"""Кэш справочников в памяти: блюда и продукты меняются только при деплое (seed.py).

Раньше каждая позиция плана била в базу отдельным SELECT — на закупках это
сотни запросов. Теперь справочник читается один раз и живёт в памяти.
"""
import time
from app.db import connect, DB_PATH

_TTL = 300
_cache = {"t": 0, "dishes": None, "products": None}


def _mtime():
    try:
        return DB_PATH.stat().st_mtime
    except OSError:
        return 0


def _load():
    now = time.time()
    fresh = _cache["dishes"] is not None and now - _cache["t"] < _TTL
    if fresh and _cache.get("m") == _mtime():      # seed.py переписал базу — перечитаем
        return
    con = connect()
    dishes = {}
    for r in con.execute("SELECT * FROM dishes"):
        dishes.setdefault(r["dish"], []).append(dict(r))
    _cache["dishes"] = dishes
    _cache["products"] = {r["id"]: dict(r) for r in con.execute("SELECT * FROM products")}
    _cache["t"] = now
    _cache["m"] = _mtime()


def dishes_of(name):
    _load()
    return _cache["dishes"].get(name, [])


def product(pid):
    _load()
    return _cache["products"].get(pid)


def products():
    _load()
    return _cache["products"]


def invalidate():
    _cache["dishes"] = _cache["products"] = None
