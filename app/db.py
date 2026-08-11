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

def init():
    sql = Path(__file__).parent.parent / "schema.sql"
    con = connect()
    con.executescript(sql.read_text(encoding="utf-8"))
    con.commit()
    return con

if __name__ == "__main__":
    init(); print(f"База готова: {DB_PATH}")
