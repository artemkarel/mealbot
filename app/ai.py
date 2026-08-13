"""ИИ-диетолог: общий движок для бота и мини-приложения.

Контекст — актуальный план пользователя, состав блюд и КБЖУ; сам вызов —
Claude API (ключ ANTHROPIC_API_KEY в .env, без него функция выключена).
"""
import os
from datetime import date
from app.db import connect, current_plan, persons_of, day_items
from app.cooking import _resolve
from app.macros import day_macros

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5")

DAY_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

AI_SYSTEM = (
    "Ты — онлайн-диетолог в приложении «Питаемся правильно». Пользователь следует "
    "индивидуальному безглютеновому плану питания от диетолога; ниже — его актуальный "
    "план и справочник состава блюд.\n"
    "Отвечай на любые вопросы о питании, продуктах, пищевых привычках и их влиянии "
    "на здоровье, самочувствие, сон и энергию — по-русски, дружелюбно, коротко и по "
    "делу, без markdown-разметки, опираясь на научный консенсус. Если вопрос касается "
    "плана — используй данные ниже; замены предлагай в духе плана: та же структура "
    "приёма, похожий состав, объём и КБЖУ, без глютена.\n"
    "Ты не врач: не ставь диагнозы и не назначай лечение или дозировки лекарств. При "
    "симптомах болезни, тревожных признаках или вопросах о серьёзных состояниях "
    "советуй обратиться к врачу или своему диетологу. Если данных не хватает — "
    "честно скажи об этом."
)


def ai_context(uid):
    """План пользователя одним текстом — контекст для Claude."""
    con = connect()
    plan = current_plan(con, uid)
    if not plan:
        return "У пользователя пока нет загруженного плана питания."
    parts = ["План: {}, человек: {}.".format(plan["title"], persons_of(con, uid))]
    week = {}
    for d in range(7):
        week[d] = day_items(con, uid, plan["id"], d)
    wd = date.today().weekday()
    for off, label in ((0, "Сегодня"), (1, "Завтра")):
        d = (wd + off) % 7
        meals, order = {}, []
        for r in week[d]:
            if r["meal"] not in meals:
                order.append(r["meal"])
            q = r["qty_max"] if r["qty_max"] is not None else r["qty_min"]
            meals.setdefault(r["meal"], []).append(
                r["name"] + (" {:g} {}".format(q, r["unit"] or "") if q else ""))
        if meals:
            parts.append("{} ({}): ".format(label, DAY_FULL[d]) + " | ".join(
                "{}: {}".format(m, ", ".join(meals[m])) for m in order))
    mac = day_macros(con, week[wd])["total"]
    if mac["kcal"]:
        parts.append("КБЖУ сегодня (на человека, примерно): {} ккал, "
                     "белки {} г, жиры {} г, углеводы {} г.".format(
                         mac["kcal"], mac["prot"], mac["fat"], mac["carb"]))
    days_short = []
    for d in range(7):
        names = list(dict.fromkeys(r["name"] for r in week[d]))
        if names:
            days_short.append("{}: {}".format(DAY_FULL[d][:2], ", ".join(names)))
    if days_short:
        parts.append("Неделя целиком:\n" + "\n".join(days_short))
    dishes = sorted({r["name"] for d in range(7) for r in week[d]})
    pname = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM products")}
    comp = []
    for n in dishes:
        det = []
        for x in _resolve(con, n):
            if not x["product"]:
                continue
            p = pname.get(x["product"], x["product"])
            if x["amount"]:
                det.append("{} {:g} {}".format(p, x["amount"], x["unit"] or "г"))
            elif x["coef"]:
                det.append("{} (готовый/сырой = {:g})".format(p, x["coef"]))
            else:
                det.append(p)
        if det:
            comp.append("{} = {}".format(n, ", ".join(det)))
    if comp:
        parts.append("Состав блюд по справочнику:\n" + "\n".join(comp))
    sups = con.execute("SELECT * FROM supplements WHERE user_id=?", (uid,)).fetchall()
    if sups:
        parts.append("БАДы: " + "; ".join(
            "{} ({}, {})".format(s["name"], s["meal"], s["timing"]) for s in sups))
    return "\n\n".join(parts)[:8000]


async def ask_claude(system, messages):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": 900,
                      "system": system, "messages": messages},
                timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", "HTTP %s" % r.status))
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
