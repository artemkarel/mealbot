"""Подключение к SQLite и применение схемы."""
import sqlite3, os, json, threading
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/mealplan.db"))
MAX_UID_OFFSET = 2_000_000_000_000   # id пользователей MAX — без пересечений с Telegram

_tls = threading.local()

def connect():
    """Одно соединение на поток. Раньше каждое обращение открывало новое и
    не закрывало его — бот за полсуток упирался в лимит открытых файлов."""
    con = getattr(_tls, "con", None)
    if con is not None:
        try:
            con.execute("SELECT 1").fetchone()
            return con
        except Exception:
            try: con.close()
            except Exception: pass
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=NORMAL")     # с WAL безопасно и заметно быстрее
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-16000")      # 16 МБ страниц в памяти
    _tls.con = con
    return con

def _migrate(con):
    """Дотягивает старую базу до текущей схемы: колонки user_id появились позже."""
    for table in ("plans", "purchases", "meal_logs"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if "user_id" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN user_id INT")
    cols = [r[1] for r in con.execute("PRAGMA table_info(user_prefs)")]
    if cols:
        for c, t in (("tz", "TEXT"), ("sex", "TEXT"), ("height", "REAL"),
                     ("weight", "REAL"), ("health", "TEXT")):
            if c not in cols:
                con.execute(f"ALTER TABLE user_prefs ADD COLUMN {c} {t}")
    cols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
    for c in ("kcal", "prot", "fat", "carb", "unit_g"):
        if cols and c not in cols:
            con.execute(f"ALTER TABLE products ADD COLUMN {c} REAL")
    cols = [r[1] for r in con.execute("PRAGMA table_info(plan_links)")]
    if cols and "product" not in cols:
        con.execute("ALTER TABLE plan_links ADD COLUMN product TEXT")
    # старый импорт вешал первую ссылку приёма на все его позиции — такие url врут
    con.execute("UPDATE plan_items SET url=NULL WHERE url IS NOT NULL AND plan_id NOT IN"
                " (SELECT DISTINCT plan_id FROM plan_links)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_plans_user ON plans(user_id, active)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id, used)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_recipes_plan ON recipes(plan_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_recipes_dish ON recipes(dish)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_urecipes_user ON user_recipes(user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_logs_plan_user ON meal_logs(plan_id, user_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_prep_plan ON prep_tasks(plan_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_rem_user ON user_reminders(user_id, enabled)")


def menu_owner(con, uid: int):
    """С кем у пользователя общее меню: чьи планы он получает (или None)."""
    r = con.execute("SELECT owner_id FROM menu_share WHERE follower_id=? LIMIT 1",
                    (uid,)).fetchone()
    return r["owner_id"] if r else None


def plan_scope(con, uid: int):
    """Чьи планы доступны пользователю: свои + владельца общего меню."""
    o = menu_owner(con, uid)
    return [uid, o] if o is not None else [uid]


def visible_plan(con, uid: int, plan_id: int):
    """План, если он доступен пользователю (свой, общий или владельца меню)."""
    scope = plan_scope(con, uid)
    q = "SELECT * FROM plans WHERE id=? AND (user_id IS NULL OR user_id IN (%s))" % (
        ",".join("?" * len(scope)))
    return con.execute(q, [plan_id] + scope).fetchone()


def current_plan(con, uid: int, _depth: int = 0):
    """Текущий план: выбранный им; иначе план владельца общего меню;
    иначе его последний; иначе общий."""
    r = con.execute("SELECT current_plan_id FROM user_prefs WHERE user_id=?", (uid,)).fetchone()
    if r and r["current_plan_id"]:
        p = visible_plan(con, uid, r["current_plan_id"])
        if p: return p
    if _depth == 0:                          # общее меню: берём выбор владельца
        o = menu_owner(con, uid)
        if o is not None:
            p = current_plan(con, o, 1)
            if p: return p
    p = con.execute("SELECT * FROM plans WHERE user_id=?"
                    " ORDER BY active DESC, id DESC LIMIT 1", (uid,)).fetchone()
    if p: return p
    return con.execute("SELECT * FROM plans WHERE user_id IS NULL"
                       " ORDER BY id DESC LIMIT 1").fetchone()


def monday_of(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def plan_for(con, uid: int, d: date, _depth: int = 0):
    """План на дату: свой выбор на неделю; иначе выбор владельца общего меню;
    иначе текущий план; для будущих недель без плана — None."""
    ws = monday_of(d)
    r = con.execute("SELECT plan_id FROM week_plans WHERE user_id=? AND week_start=?",
                    (uid, ws)).fetchone()
    if r:
        p = visible_plan(con, uid, r["plan_id"])
        if p: return p
    if _depth == 0:
        o = menu_owner(con, uid)
        if o is not None:
            p = plan_for(con, o, d, 1)
            if p: return p
    if ws <= monday_of(date.today()):
        return current_plan(con, uid)
    return None


def touch_user(con, uid: int, first_name=None, last_name=None, username=None, source=None):
    """Отмечает пользователя как живого — для списка «кто пользуется» у админа."""
    con.execute(
        "INSERT INTO users(user_id, first_name, last_name, username, last_seen, source)"
        " VALUES(?,?,?,?,datetime('now'),?)"
        " ON CONFLICT(user_id) DO UPDATE SET"
        " first_name=COALESCE(excluded.first_name, first_name),"
        " last_name=COALESCE(excluded.last_name, last_name),"
        " username=COALESCE(excluded.username, username),"
        " last_seen=excluded.last_seen, source=excluded.source",
        (uid, first_name, last_name, username, source))
    con.commit()


def day_items(con, uid: int, plan_id: int, day: int):
    """Позиции дня плана с учётом замен пользователя (meal_overrides).
    Заменённый приём подставляется целиком, у его позиций swapped=1."""
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM plan_items WHERE plan_id=? AND day_index=?"
        " ORDER BY meal_index, id", (plan_id, day))]
    ovs = {r["meal"]: json.loads(r["items_json"] or "[]") for r in con.execute(
        "SELECT meal, items_json FROM meal_overrides"
        " WHERE user_id=? AND plan_id=? AND day_index=?", (uid, plan_id, day))}
    if not ovs:
        return rows
    out, done = [], set()
    for r in rows:
        m = r["meal"]
        if m not in ovs:
            out.append(r)
        elif m not in done:
            done.add(m)
            for it in ovs[m]:
                out.append({**r, "name": it.get("name"), "qty_min": it.get("qty_min"),
                            "qty_max": it.get("qty_max"), "unit": it.get("unit"),
                            "note": None, "url": None, "swapped": 1})
    # приём, которого в плане нет, а пользователь его добавил (например второй завтрак)
    base = dict(rows[0]) if rows else {}
    for m, items in ovs.items():
        if m in done:
            continue
        for it in items:
            out.append({**base, "plan_id": plan_id, "day_index": day,
                        "day_name": base.get("day_name"), "meal": m, "meal_index": 99,
                        "optional": 0, "note": None, "url": None, "swapped": 1,
                        "name": it.get("name"), "qty_min": it.get("qty_min"),
                        "qty_max": it.get("qty_max"), "unit": it.get("unit")})
    return out


def persons_of(con, uid: int) -> int:
    r = con.execute("SELECT persons FROM user_prefs WHERE user_id=?", (uid,)).fetchone()
    return r["persons"] if r else 1


def set_current_plan(con, uid: int, plan_id: int):
    con.execute("INSERT INTO user_prefs(user_id, current_plan_id) VALUES(?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET current_plan_id=excluded.current_plan_id",
                (uid, plan_id))


def set_persons(con, uid: int, n: int):
    con.execute("INSERT INTO user_prefs(user_id, persons) VALUES(?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET persons=excluded.persons", (uid, n))


def init():
    sql = Path(__file__).parent.parent / "schema.sql"
    con = connect()
    con.executescript(sql.read_text(encoding="utf-8"))
    _migrate(con)
    con.commit()
    return con

if __name__ == "__main__":
    init(); print(f"База готова: {DB_PATH}")
