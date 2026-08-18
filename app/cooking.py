"""Пересчёт сырого и готового веса + план готовки на 1 или 2 дня."""
from app.db import connect
from app import ref

def _resolve(con, name):
    """Название из плана -> строки справочника (у составного блюда их несколько)."""
    return ref.dishes_of(name)

def cook_plan(plan_id: int, day_index: int, days: int = 1, persons: int = 1, items=None):
    """Что и сколько готовить. days=2 — сразу на два одинаковых дня.
    items — позиции дня с учётом замен (иначе берутся из плана как есть)."""
    con = connect()
    if items is None:
        items = con.execute(
            "SELECT name, meal, COALESCE(qty_max,qty_min) q, unit FROM plan_items"
            " WHERE plan_id=? AND day_index=?", (plan_id, day_index)).fetchall()
    else:
        items = [{"name": i["name"], "meal": i["meal"], "unit": i["unit"],
                  "q": i["qty_max"] if i["qty_max"] is not None else i["qty_min"]}
                 for i in items]
    agg = {}
    for it in items:
        for d in _resolve(con, it["name"]):
            if not d["product"]: continue
            prod = ref.product(d["product"])
            if not prod: continue
            cooked = (it["q"] or 0) * days * persons
            unit = it["unit"]
            # план считает штуками, а продукт в граммах (тартин, онигири) — переводим
            if (unit or "") == "шт" and (prod["unit"] or "") != "шт":
                cooked *= prod["unit_g"] or 1
                unit = prod["unit"]
            if d["amount"]:                       # фиксированная раскладка — в единицах продукта
                raw, raw_unit, fixed = d["amount"] * days * persons, prod["unit"], True
            elif d["coef"]:                       # coef = готовый / сырой
                raw, raw_unit, fixed = cooked / d["coef"], unit, False
            else:
                raw, raw_unit, fixed = cooked, unit, False
            k = d["product"]
            a = agg.setdefault(k, {"product": prod["name"], "dish": it["name"],
                                   "raw": 0, "cooked": 0, "unit": unit,
                                   "raw_unit": raw_unit, "fixed": False, "meals": []})
            a["raw"] += raw; a["cooked"] += cooked; a["meals"].append(it["meal"])
            a["fixed"] = a["fixed"] or fixed
    for a in agg.values():
        a["raw"] = round(a["raw"] / 5) * 5 if a["raw"] >= 100 else round(a["raw"], 1)
        # у продукта из раскладки суммарный «готовый» вес не имеет смысла —
        # это вес всего блюда, а не этого ингредиента
        if a["fixed"]:
            a["cooked"] = None
        else:
            a["cooked"] = round(a["cooked"] / 5) * 5 if a["cooked"] >= 100 else round(a["cooked"], 1)
        a["meals"] = " + ".join(dict.fromkeys(a["meals"]))
        del a["fixed"]
    return sorted(agg.values(), key=lambda x: -(x["cooked"] if x["cooked"] is not None else x["raw"]))

_same_cache = {}

def same_days(plan_id: int):
    """Пары одинаковых дней -> {day_index: [индексы дублей]}"""
    if plan_id in _same_cache:
        return _same_cache[plan_id]
    con = connect()
    sig = {}
    for r in con.execute("SELECT day_index, name, qty_max, unit FROM plan_items"
                         " WHERE plan_id=? ORDER BY day_index, id", (plan_id,)):
        sig.setdefault(r["day_index"], []).append(f'{r["name"]}|{r["qty_max"]}{r["unit"]}')
    groups = {}
    for k, v in sig.items(): groups.setdefault(tuple(v), []).append(k)
    res = {d: [x for x in g if x != d] for g in groups.values() if len(g) > 1 for d in g}
    _same_cache[plan_id] = res
    return res
