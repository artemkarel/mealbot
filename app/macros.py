"""КБЖУ дня — примерная оценка по справочнику.

Значения в products — на 100 г/мл сырого продукта, поэтому считаем по сырому весу
(калорийность при готовке сохраняется). unit_g переводит штуки в граммы.
"""
from app.cooking import _resolve


def item_macros(con, name, q, unit):
    """КБЖУ одной позиции плана на одну порцию. None — блюда нет в справочнике."""
    rows = _resolve(con, name)
    if not rows:
        return None
    tot = {"kcal": 0.0, "prot": 0.0, "fat": 0.0, "carb": 0.0}
    known = False
    for d in rows:
        if not d["product"]:
            known = known or d["type"] == "напиток"   # вода/чай — честный ноль
            continue
        prod = con.execute("SELECT * FROM products WHERE id=?", (d["product"],)).fetchone()
        if not prod or prod["kcal"] is None:
            continue
        unit_g = prod["unit_g"] or 1
        if d["amount"]:                               # фиксированная раскладка, в единицах продукта
            grams = d["amount"] * (unit_g if (d["unit"] or "") == "шт" else 1)
        else:
            raw = (q or 0) / d["coef"] if d["coef"] else (q or 0)
            grams = raw * (unit_g if (unit or "") == "шт" else 1)
        tot["kcal"] += grams / 100 * (prod["kcal"] or 0)
        tot["prot"] += grams / 100 * (prod["prot"] or 0)
        tot["fat"] += grams / 100 * (prod["fat"] or 0)
        tot["carb"] += grams / 100 * (prod["carb"] or 0)
        known = True
    return tot if known else None


def day_macros(con, items):
    """Суммы по дню и по приёмам. items — позиции дня (после замен), количества на одного."""
    total = {"kcal": 0.0, "prot": 0.0, "fat": 0.0, "carb": 0.0}
    meals, unknown = {}, 0
    for it in items:
        q = it["qty_max"] if it["qty_max"] is not None else it["qty_min"]
        m = item_macros(con, it["name"], q, it["unit"])
        if m is None:
            unknown += 1
            continue
        for k in total:
            total[k] += m[k]
        meals[it["meal"]] = meals.get(it["meal"], 0) + m["kcal"]
    return {"total": {k: round(v) for k, v in total.items()},
            "meals": {k: round(v) for k, v in meals.items()},
            "unknown": unknown}
