"""ИИ-диетолог: общий движок для бота и мини-приложения.

Контекст — актуальный план пользователя, состав блюд и КБЖУ; сам вызов —
Claude API (ключ ANTHROPIC_API_KEY в .env, без него функция выключена).
"""
import os, re, json
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
    "Если указаны пол, рост, вес и особенности здоровья — обязательно учитывай их "
    "в расчётах (нормы калорий, БЖУ, вода) и советах. При аллергиях и "
    "непереносимостях никогда не предлагай такие продукты и предупреждай о них.\n"
    "Ты не врач: не ставь диагнозы и не назначай лечение или дозировки лекарств. При "
    "симптомах болезни, тревожных признаках или вопросах о серьёзных состояниях "
    "советуй обратиться к врачу или своему диетологу. Если данных не хватает — "
    "честно скажи об этом.\n"
    "Если пользователь просит найти или придумать рецепт — дай один конкретный рецепт "
    "(ингредиенты с количествами и шаги) в духе его плана. В самом конце такого ответа "
    "добавь отдельной строкой служебный блок строго одной строкой:\n"
    'RECIPE_JSON: {"title": "Название", "ingredients": ["Мука рисовая — 100 г"], '
    '"steps": ["Смешать", "Выпекать 20 минут"]}\n'
    "Блок добавляй только когда в ответе есть конкретный рецепт, не упоминай его в тексте."
)

RECIPE_RE = re.compile(r"RECIPE_JSON:\s*(\{.*\})\s*$", re.S)


def split_recipe(answer):
    """Отделяет служебный RECIPE_JSON от текста ответа. -> (текст, рецепт|None)"""
    m = RECIPE_RE.search(answer or "")
    if not m:
        return answer, None
    text = (answer[:m.start()]).rstrip()
    try:
        r = json.loads(m.group(1))
        title = str(r.get("title") or "").strip()[:120]
        ings = [str(s).strip()[:200] for s in (r.get("ingredients") or []) if str(s).strip()]
        steps = [str(s).strip()[:500] for s in (r.get("steps") or []) if str(s).strip()]
        if title and (ings or steps):
            return text, {"title": title, "ingredients": ings, "steps": steps}
    except Exception:
        pass
    return text, None


def _about_user(con, uid):
    """Пол/рост/вес и особенности здоровья — если пользователь их указал."""
    r = con.execute("SELECT sex, height, weight, health FROM user_prefs WHERE user_id=?",
                    (uid,)).fetchone()
    if not r:
        return None
    bits = []
    if r["sex"]: bits.append("пол: " + ("мужской" if r["sex"] == "м" else "женский"))
    if r["height"]: bits.append("рост: {:g} см".format(r["height"]))
    if r["weight"]: bits.append("вес: {:g} кг".format(r["weight"]))
    parts = []
    if bits:
        parts.append("О пользователе: " + ", ".join(bits) + ".")
    if r["health"]:
        parts.append("Особенности здоровья, болезни и аллергии (учитывай обязательно): "
                     + r["health"])
    return "\n".join(parts) or None


def ai_context(uid):
    """План пользователя одним текстом — контекст для Claude."""
    con = connect()
    about = _about_user(con, uid)
    plan = current_plan(con, uid)
    if not plan:
        return about or "У пользователя пока нет загруженного плана питания."
    parts = ["План: {}, человек: {}.".format(plan["title"], persons_of(con, uid))]
    if about:
        parts.append(about)
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


async def ask_claude(system, messages, max_tokens=1400, timeout=60):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": max_tokens,
                      "system": system, "messages": messages},
                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", "HTTP %s" % r.status))
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
