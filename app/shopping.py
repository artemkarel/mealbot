"""Список закупок: агрегация продуктов одним списком на неделю, округление до фасовки.
Скоропорт, нужный во второй половине недели, получает подсказку вместо отдельной закупки."""
from app.db import connect
from app.cooking import _resolve
from app import ref

def build(plan_id: int, day_from=0, day_to=6, persons=1, split_after=2, items=None):
    """split_after — последний день «первой половины» недели (0=Пн, 2=Ср): что нужно
    после него и быстро портится, помечаем «заморозить» / «докупить».
    items — позиции всех дней с учётом замен (иначе берутся из плана как есть)."""
    con = connect()
    if items is None:
        rows = [dict(x) for x in con.execute(
            "SELECT day_index, name, COALESCE(qty_max,qty_min) q, unit FROM plan_items"
            " WHERE plan_id=? AND day_index BETWEEN ? AND ?",
            (plan_id, day_from, day_to))]
    else:
        rows = [{"day_index": i["day_index"], "name": i["name"], "unit": i["unit"],
                 "q": i["qty_max"] if i["qty_max"] is not None else i["qty_min"]}
                for i in items if day_from <= i["day_index"] <= day_to]
    agg = {}
    for r in rows:
        for d in _resolve(con, r["name"]):
            if not d["product"] or d["type"] == "напиток": continue
            prod = ref.product(d["product"])
            if not prod: continue
            cooked = (r["q"] or 0) * persons
            # план считает штуками, а продукт в граммах (тартин, онигири) — переводим
            if (r["unit"] or "") == "шт" and (prod["unit"] or "") != "шт":
                cooked *= prod["unit_g"] or 1
            raw = d["amount"] * persons if d["amount"] else (cooked / d["coef"] if d["coef"] else cooked)
            a = agg.setdefault(prod["id"], {
                "id": prod["id"], "name": prod["name"], "category": prod["category"],
                "shelf": prod["shelf_days"], "freezable": prod["freezable"],
                "pack": prod["pack"], "unit": prod["unit"], "url": prod["url"],
                "early": 0, "late": 0, "days": []})
            (a.__setitem__("early", a["early"] + raw) if r["day_index"] <= split_after
             else a.__setitem__("late", a["late"] + raw))
            a["days"].append(r["day_index"])

    out = []
    for a in agg.values():
        total = a["early"] + a["late"]
        tag = None
        if a["late"] and a["shelf"] <= 3:          # скоропорт на вторую половину недели
            tag = "часть заморозить" if a["freezable"] else "свежее — докупить к Чт"
        out.append({**a, "qty": pack(total, a), "tag": tag})
    return {"items": sorted(out, key=lambda x: (x["category"] or "", x["name"]))}

def pack(qty, p):
    """Округляем до целой упаковки, если она известна."""
    if p["pack"]:
        n = max(1, -(-qty // p["pack"]))
        return {"value": n * p["pack"], "packs": int(n), "unit": p["unit"]}
    v = round(qty / 5) * 5 if qty >= 100 else round(qty, 1)
    return {"value": v, "packs": None, "unit": p["unit"]}
