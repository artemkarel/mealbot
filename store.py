# -*- coding: utf-8 -*-
"""Хранилище: план из plan.json + общие данные семьи в SQLite (data.db)."""
import os, json, shutil, sqlite3
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
PLAN_BASE = os.path.join(BASE, "plan.json")          # версия из репозитория (обновляется через git)
PLAN_LOCAL = os.path.join(BASE, "plan.local.json")   # план, присланный в бота (переживает обновления)
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE, "data.db"))


def plan_path():
    return PLAN_LOCAL if os.path.exists(PLAN_LOCAL) else PLAN_BASE


PLAN = {}


def reload_plan():
    """Перечитать план с диска."""
    global PLAN
    with open(plan_path(), encoding="utf-8") as f:
        PLAN = json.load(f)
    return PLAN


reload_plan()


def replace_plan(raw_bytes):
    """Заменить план новым файлом (с бэкапом старого). Возвращает (недель, рецептов)."""
    data = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(data, dict) or "weeks" not in data:
        raise ValueError("в файле нет ключа 'weeks' — это не план питания")
    if not isinstance(data["weeks"], list) or not data["weeks"]:
        raise ValueError("список недель пуст")
    for w in data["weeks"]:
        for k in ("id", "label", "days", "shop"):
            if k not in w:
                raise ValueError(f"в неделе нет поля '{k}'")
    data.setdefault("recipes", [])
    data.setdefault("dish_ingredients", {})
    if os.path.exists(PLAN_LOCAL):
        shutil.copyfile(PLAN_LOCAL, PLAN_LOCAL + ".bak")
    tmp = PLAN_LOCAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PLAN_LOCAL)
    reload_plan()
    return len(data["weeks"]), len(data["recipes"])


def dish_ingredients():
    return PLAN.get("dish_ingredients", {})


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS checks(item_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS uchecks(
                uid INTEGER, item_id TEXT, PRIMARY KEY(uid, item_id));
            CREATE TABLE IF NOT EXISTS recipes(rid TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS weeks(wid TEXT PRIMARY KEY, ord INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS genweeks(gid TEXT PRIMARY KEY, ord INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS picks(dish TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS extras(
                eid INTEGER PRIMARY KEY AUTOINCREMENT,
                wid TEXT, trip INTEGER, name TEXT);
            CREATE TABLE IF NOT EXISTS supplements(
                sid INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER DEFAULT 0,
                name TEXT, dose TEXT, slots TEXT);
            CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY);
            """
        )
        for tbl in ("supplements", "extras"):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
            if "uid" not in cols:                   # база из прошлой версии
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN uid INTEGER DEFAULT 0")


# ---- settings (kv) ----
def get_setting(k, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def set_setting(k, v):
    with conn() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, str(v)))


def del_setting(k):
    with conn() as c:
        c.execute("DELETE FROM kv WHERE k=?", (k,))


# ---- личные настройки (у каждого свои) ----
def get_user_setting(uid, k, default=None):
    return get_setting(f"u{uid}:{k}", default)


def set_user_setting(uid, k, v):
    set_setting(f"u{uid}:{k}", v)


def del_user_setting(uid, k):
    del_setting(f"u{uid}:{k}")


def migrate_personal(uid):
    """Разовый перенос: то, что раньше было общим, становится личным для владельца."""
    if not uid or get_setting("migrated_personal"):
        return
    for k in ("active_plan", "active_started", "next_plan", "auto_next",
              "morning_time", "morning_on", "evening_on", "shop_on"):
        v = get_setting(k)
        if v is not None and get_user_setting(uid, k) is None:
            set_user_setting(uid, k, v)
    v = get_setting("people")
    if v is not None and get_user_setting(uid, "people") is None:
        set_user_setting(uid, "people", v)
    with conn() as c:
        c.execute("UPDATE supplements SET uid=? WHERE uid IS NULL OR uid=0", (int(uid),))
        c.execute("UPDATE extras SET uid=? WHERE uid IS NULL OR uid=0", (int(uid),))
        c.execute("INSERT OR IGNORE INTO uchecks(uid,item_id) SELECT ?, item_id FROM checks",
                  (int(uid),))
    set_setting("migrated_personal", "1")


# ---- people ----
def get_people(uid):
    v = get_user_setting(uid, "people")
    return int(v) if v else 1


def set_people(uid, n):
    set_user_setting(uid, "people", max(1, min(12, int(n))))


# ---- checkmarks (shared) ----
def is_checked(uid, item_id):
    with conn() as c:
        return c.execute("SELECT 1 FROM uchecks WHERE uid=? AND item_id=?",
                         (int(uid), item_id)).fetchone() is not None


def toggle_check(uid, item_id):
    with conn() as c:
        if c.execute("SELECT 1 FROM uchecks WHERE uid=? AND item_id=?",
                     (int(uid), item_id)).fetchone():
            c.execute("DELETE FROM uchecks WHERE uid=? AND item_id=?", (int(uid), item_id))
        else:
            c.execute("INSERT INTO uchecks(uid,item_id) VALUES(?,?)", (int(uid), item_id))


def checked_set(uid, prefix):
    with conn() as c:
        rows = c.execute("SELECT item_id FROM uchecks WHERE uid=? AND item_id LIKE ?",
                         (int(uid), prefix + "%")).fetchall()
        return {r["item_id"] for r in rows}


def reset_week(uid, wid):
    with conn() as c:
        c.execute("DELETE FROM uchecks WHERE uid=? AND item_id LIKE ?", (int(uid), wid + ":%"))


def uncheck_many(uid, ids):
    if not ids:
        return
    with conn() as c:
        c.executemany("DELETE FROM uchecks WHERE uid=? AND item_id=?",
                      [(int(uid), i) for i in ids])


# ---- свои пункты списка ----
def all_extras(uid, wid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT eid,trip,name FROM extras WHERE uid=? AND wid=? ORDER BY eid",
            (int(uid), wid)).fetchall()]


def add_extra(uid, wid, trip, name):
    with conn() as c:
        cur = c.execute("INSERT INTO extras(uid,wid,trip,name) VALUES(?,?,?,?)",
                        (int(uid), wid, int(trip), name))
        return cur.lastrowid


def del_extra(uid, wid, eid):
    with conn() as c:
        c.execute("DELETE FROM extras WHERE uid=? AND wid=? AND eid=?", (int(uid), wid, int(eid)))
        c.execute("DELETE FROM uchecks WHERE uid=? AND item_id=?", (int(uid), f"{wid}:x{eid}"))


# ---- биодобавки ----
def all_supps(uid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT sid,name,dose,slots FROM supplements WHERE uid=? ORDER BY sid",
            (int(uid),)).fetchall()]


def get_supp(uid, sid):
    with conn() as c:
        r = c.execute("SELECT sid,name,dose,slots FROM supplements WHERE sid=? AND uid=?",
                      (int(sid), int(uid))).fetchone()
        return dict(r) if r else None


def add_supp(uid, name, dose, slots):
    with conn() as c:
        cur = c.execute("INSERT INTO supplements(uid,name,dose,slots) VALUES(?,?,?,?)",
                        (int(uid), name, dose, slots))
        return cur.lastrowid


def set_supp_slots(uid, sid, slots):
    with conn() as c:
        c.execute("UPDATE supplements SET slots=? WHERE sid=? AND uid=?",
                  (slots, int(sid), int(uid)))


def del_supp(uid, sid):
    with conn() as c:
        c.execute("DELETE FROM supplements WHERE sid=? AND uid=?", (int(sid), int(uid)))


# ---- weeks (из plan.json + добавленные) ----
def all_weeks():
    weeks = [dict(w) for w in PLAN["weeks"]]
    with conn() as c:
        for r in c.execute("SELECT data FROM weeks ORDER BY ord").fetchall():
            weeks.append(json.loads(r["data"]))
    return weeks


def get_week(wid):
    for w in all_weeks() + all_generated():
        if w["id"] == wid:
            return w
    return None


def add_week(week):
    with conn() as c:
        n = c.execute("SELECT COALESCE(MAX(ord),0)+1 AS n FROM weeks").fetchone()["n"]
        wid = "x%d" % n
        week["id"] = wid
        week.setdefault("accent", "#33624F")
        c.execute("INSERT INTO weeks(wid,ord,data) VALUES(?,?,?)",
                  (wid, n, json.dumps(week, ensure_ascii=False)))
    return wid


# ---- generated (random) menus ----
def all_generated():
    with conn() as c:
        return [json.loads(r["data"])
                for r in c.execute("SELECT data FROM genweeks ORDER BY ord").fetchall()]


def add_generated(week):
    with conn() as c:
        n = c.execute("SELECT COALESCE(MAX(ord),0)+1 AS n FROM genweeks").fetchone()["n"]
        gid = "g%d" % n
        week["id"] = gid
        c.execute("INSERT INTO genweeks(gid,ord,data) VALUES(?,?,?)",
                  (gid, n, json.dumps(week, ensure_ascii=False)))
    return gid


def delete_generated(gid):
    with conn() as c:
        c.execute("DELETE FROM genweeks WHERE gid=?", (gid,))
        c.execute("DELETE FROM checks WHERE item_id LIKE ?", (gid + ":%",))


# ---- recipes (встроенные + отредактированные) ----
def all_recipes():
    base = {r["id"]: dict(r) for r in PLAN["recipes"]}
    with conn() as c:
        for row in c.execute("SELECT rid,data FROM recipes").fetchall():
            d = json.loads(row["data"])
            base[d["id"]] = d
    return list(base.values())


def get_recipe(rid):
    for r in all_recipes():
        if r["id"] == rid:
            return r
    return None


def save_recipe(r):
    with conn() as c:
        c.execute("INSERT INTO recipes(rid,data) VALUES(?,?) ON CONFLICT(rid) DO UPDATE SET data=excluded.data",
                  (r["id"], json.dumps(r, ensure_ascii=False)))


# ---- chats (for reminders) ----
def add_chat(cid):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (cid,))


def all_chats():
    with conn() as c:
        return [r["chat_id"] for r in c.execute("SELECT chat_id FROM chats").fetchall()]
