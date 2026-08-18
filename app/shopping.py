"""Список закупок: агрегация продуктов, деление по срокам хранения, округление до фасовки."""
from app.db import connect
from app.cooking import _resolve
from app import ref

def build(plan_id: int, day_from=0, day_to=6, persons=1, split_after=2, items=None):
    """split_after — последний день первой закупки (0=Пн, 2=Ср).
    items — позиции всех дней с учётом замен (иначе берутся из плана как есть)."""
    con = connect()
    if items is None:
        rows = con.execute(
            "SELECT day_index, name, COALESCE(qty_max,qty_min) q, unit FROM plan_items"
            " WHERE plan_id=? AND day_index BETWEEN ? AND ?",
            (plan_id, day_from, day_to)).fetchall()
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

    part1, part2 = [], []
    for a in agg.values():
        total = a["early"] + a["late"]
        if a["shelf"] >= 14:                       # долгое хранение — берём сразу всё
            part1.append({**a, "qty": pack(total, a), "tag": "на всю неделю"})
            continue
        if a["early"]:
            part1.append({**a, "qty": pack(a["early"], a), "tag": "на первые дни"})
        if a["late"]:
            if a["freezable"] and a["shelf"] <= 3:
                part1.append({**a, "qty": pack(a["late"], a), "tag": "заморозить сразу"})
            else:
                part2.append({**a, "qty": pack(a["late"], a), "tag": "на вторую половину"})
    key = lambda x: (x["category"] or "", x["name"])
    return {"part1": sorted(part1, key=key), "part2": sorted(part2, key=key)}

def pack(qty, p):
    """Округляем до целой упаковки, если она известна."""
    if p["pack"]:
        n = max(1, -(-qty // p["pack"]))
        return {"value": n * p["pack"], "packs": int(n), "unit": p["unit"]}
    v = round(qty / 5) * 5 if qty >= 100 else round(qty, 1)
    return {"value": v, "packs": None, "unit": p["unit"]}
