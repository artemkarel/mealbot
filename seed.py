"""Заливает справочники из CSV в базу. Запускать после каждой правки таблиц.

Разделитель определяется сам — можно сохранять хоть с точкой с запятой,
хоть с запятой, из Excel, Numbers или Google Таблиц.
"""
import csv
from pathlib import Path
from app.db import init


def read_csv(path):
    text = Path(path).read_text(encoding="utf-8-sig")   # -sig убирает метку Excel
    head = text.split("\n", 1)[0]
    delim = ";" if head.count(";") >= head.count(",") else ","
    return list(csv.DictReader(text.splitlines(), delimiter=delim))


def num(v):
    v = (v or "").strip().replace(",", ".").replace(" ", "")
    try:
        return float(v)
    except ValueError:
        return None


con = init()
con.execute("DELETE FROM dishes")     # сначала блюда — они ссылаются на товары
con.execute("DELETE FROM products")

rows = read_csv("data/products.csv")
con.executemany(
    "INSERT INTO products(id,name,shelf_days,freezable,category,pack,unit,url)"
    " VALUES(?,?,?,?,?,?,?,?)",
    [(r["id"].strip(), r["name"].strip(), int(num(r["shelf_days"]) or 14),
      int(num(r["freezable"]) or 0), r["category"], num(r["pack"]),
      (r["unit"] or "г").strip(), (r["url"] or "").strip() or None)
     for r in rows if (r.get("id") or "").strip()])
print(f"товаров: {len(rows)}")

rows = read_csv("data/dishes.csv")
con.executemany(
    "INSERT INTO dishes(dish,type,product,amount,unit,coef,note) VALUES(?,?,?,?,?,?,?)",
    [(r["dish"].strip(), r["type"], (r["product"] or "").strip() or None,
      num(r["amount"]), (r["unit"] or "").strip() or None, num(r["coef"]),
      (r["note"] or "").strip() or None)
     for r in rows if (r.get("dish") or "").strip()])
print(f"блюд: {len(rows)}")

con.commit()
need = con.execute("SELECT COUNT(*) c FROM dishes WHERE note LIKE 'ПРОВЕРЬ%'").fetchone()["c"]
print(f"строк с пометкой ПРОВЕРЬ: {need}")
