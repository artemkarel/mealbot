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
    "INSERT INTO products(id,name,shelf_days,freezable,category,pack,unit,url,"
    "kcal,prot,fat,carb,unit_g,search) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    [(r["id"].strip(), r["name"].strip(), int(num(r["shelf_days"]) or 14),
      int(num(r["freezable"]) or 0), r["category"], num(r["pack"]),
      (r["unit"] or "г").strip(), (r["url"] or "").strip() or None,
      num(r.get("kcal")), num(r.get("prot")), num(r.get("fat")), num(r.get("carb")),
      num(r.get("unit_g")) or 1, (r.get("search") or "").strip() or None)
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

con.execute("DELETE FROM store_links")
if Path("data/store_links.csv").exists():
    rows = [r for r in read_csv("data/store_links.csv") if (r.get("product") or "").strip() and (r.get("url") or "").strip()]
    known = {r[0] for r in con.execute("SELECT id FROM products")}
    rows = [r for r in rows if r["product"].strip() in known]
    con.executemany("INSERT OR REPLACE INTO store_links(product,store,url,name) VALUES(?,?,?,?)",
                    [(r["product"].strip(), r["store"].strip(), r["url"].strip(), (r.get("name") or "").strip() or None) for r in rows])
    print(f"карточек в магазинах: {len(rows)}")
con.execute("DELETE FROM vv_items")
if Path("data/vv_items.csv").exists():
    rows = [r for r in read_csv("data/vv_items.csv") if (r.get("product") or "").strip() and num(r.get("xml_id"))]
    known = {r[0] for r in con.execute("SELECT id FROM products")}
    rows = [r for r in rows if r["product"].strip() in known]
    con.executemany("INSERT OR REPLACE INTO vv_items(product,xml_id,unit,weight_kg,price,name,pcs) VALUES(?,?,?,?,?,?,?)",
                    [(r["product"].strip(), int(num(r["xml_id"])), (r.get("unit") or "").strip() or None,
                      num(r.get("weight_kg")), num(r.get("price")), (r.get("name") or "").strip() or None,
                      num(r.get("pcs"))) for r in rows])
    print(f"карточек ВкусВилл с единицами: {len(rows)}")
con.commit()
need = con.execute("SELECT COUNT(*) c FROM dishes WHERE note LIKE 'ПРОВЕРЬ%'").fetchone()["c"]
print(f"строк с пометкой ПРОВЕРЬ: {need}")
