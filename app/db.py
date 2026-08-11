"""Подключение к SQLite и применение схемы."""
import sqlite3, os
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/mealplan.db"))

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def _migrate(con):
    """Дотягивает старую базу до текущей схемы: колонка user_id появилась позже."""
    for table in ("plans", "purchases"):
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
        if "user_id" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN user_id INT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_plans_user ON plans(user_id, active)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id, used)")


def init():
    sql = Path(__file__).parent.parent / "schema.sql"
    con = connect()
    con.executescript(sql.read_text(encoding="utf-8"))
    _migrate(con)
    con.commit()
    return con

if __name__ == "__main__":
    init(); print(f"База готова: {DB_PATH}")
