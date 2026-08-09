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
            CREATE TABLE IF NOT EXISTS recipes(rid TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS weeks(wid TEXT PRIMARY KEY, ord INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS genweeks(gid TEXT PRIMARY KEY, ord INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS picks(dish TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS extras(
                eid INTEGER PRIMARY KEY AUTOINCREMENT,
                wid TEXT, trip INTEGER, name TEXT);
            CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY);
            """
        )


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


# ---- people ----
def get_people():
    with conn() as c:
        r = c.execute("SELECT v FROM kv WHERE k='people'").fetchone()
        return int(r["v"]) if r else 1


def set_people(n):
    n = max(1, min(12, int(n)))
    with conn() as c:
        c.execute(
            "INSERT INTO kv(k,v) VALUES('people',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(n),),
        )


# ---- checkmarks (shared) ----
def is_checked(item_id):
    with conn() as c:
        return c.execute("SELECT 1 FROM checks WHERE item_id=?", (item_id,)).fetchone() is not None


def toggle_check(item_id):
    with conn() as c:
        if c.execute("SELECT 1 FROM checks WHERE item_id=?", (item_id,)).fetchone():
            c.execute("DELETE FROM checks WHERE item_id=?", (item_id,))
        else:
            c.execute("INSERT INTO checks(item_id) VALUES(?)", (item_id,))


def checked_set(prefix):
    with conn() as c:
        rows = c.execute("SELECT item_id FROM checks WHERE item_id LIKE ?", (prefix + "%",)).fetchall()
        return {r["item_id"] for r in rows}


def reset_week(wid):
    with conn() as c:
        c.execute("DELETE FROM checks WHERE item_id LIKE ?", (wid + ":%",))


def uncheck_many(ids):
    if not ids:
        return
    with conn() as c:
        c.executemany("DELETE FROM checks WHERE item_id=?", [(i,) for i in ids])


# ---- свои пункты списка ----
def all_extras(wid):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT eid,trip,name FROM extras WHERE wid=? ORDER BY eid", (wid,)).fetchall()]


def add_extra(wid, trip, name):
    with conn() as c:
        cur = c.execute("INSERT INTO extras(wid,trip,name) VALUES(?,?,?)", (wid, int(trip), name))
        return cur.lastrowid


def del_extra(wid, eid):
    with conn() as c:
        c.execute("DELETE FROM extras WHERE wid=? AND eid=?", (wid, int(eid)))
        c.execute("DELETE FROM checks WHERE item_id=?", (f"{wid}:x{eid}",))


# ---- weeks (built-in from plan.json + added from parsed files) ----
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


def add_week(week):
    with conn() as c:
        n = c.execute("SELECT COALESCE(MAX(ord),0)+1 AS n FROM weeks").fetchone()["n"]
        wid = "x%d" % n
        week["id"] = wid
        week.setdefault("accent", "#33624F")
        c.execute(
            "INSERT INTO weeks(wid,ord,data) VALUES(?,?,?)",
            (wid, n, json.dumps(week, ensure_ascii=False)),
        )
    return wid


# ---- recipes (built-in + edited overrides) ----
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
        c.execute(
            "INSERT INTO recipes(rid,data) VALUES(?,?) ON CONFLICT(rid) DO UPDATE SET data=excluded.data",
            (r["id"], json.dumps(r, ensure_ascii=False)),
        )


# ---- chats (for reminders) ----
def add_chat(cid):
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO chats(chat_id) VALUES(?)", (cid,))


def all_chats():
    with conn() as c:
        return [r["chat_id"] for r in c.execute("SELECT chat_id FROM chats").fetchall()]
