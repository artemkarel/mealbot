"""Подключение к SQLite и применение схемы."""
import sqlite3, os, json
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/mealplan.db"))

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def _migrate(con):
    """Дотягивает старую базу до текущей схемы: колонки user_id появились позже."""
    for table in ("plans", "purchases", "meal_logs"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if "user_id" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN user_id INT")
    cols = [r[1] for r in con.execute("PRAGMA table_info(user_prefs)")]
    if cols and "tz" not in cols:
        con.execute("ALTER TABLE user_prefs ADD COLUMN tz TEXT")
    cols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
    for c in ("kcal", "prot", "fat", "carb", "unit_g"):
        if cols and c not in cols:
            con.execute(f"ALTER TABLE products ADD COLUMN {c} REAL")
    con.execute("CREATE INDEX IF NOT EXISTS idx_plans_user ON plans(user_id, active)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id, used)")


def current_plan(con, uid: int):
    """Текущий план пользователя: выбранный им; иначе его последний; иначе общий."""
    r = con.execute("SELECT current_plan_id FROM user_prefs WHERE user_id=?", (uid,)).fetchone()
    if r and r["current_plan_id"]:
        p = con.execute("SELECT * FROM plans WHERE id=? AND (user_id IS NULL OR user_id=?)",
                        (r["current_plan_id"], uid)).fetchone()
        if p: return p
    p = con.execute("SELECT * FROM plans WHERE user_id=?"
                    " ORDER BY active DESC, id DESC LIMIT 1", (uid,)).fetchone()
    if p: return p
    return con.execute("SELECT * FROM plans WHERE user_id IS NULL"
                       " ORDER BY id DESC LIMIT 1").fetchone()


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
