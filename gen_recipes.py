"""Базовые рецепты для блюд из планов — генерирует Claude по раскладкам справочника.

Кладёт их в recipes с plan_id NULL: этот слой виден во всех планах и
перекрывается рецептами из файла диетолога и своими рецептами пользователя.
Запускать на сервере (нужен ANTHROPIC_API_KEY). Повторный запуск дополняет
только недостающие блюда.
"""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from app.db import connect
from app.ai import ask_claude, ANTHROPIC_KEY

SYSTEM = (
    "Ты — терпеливый повар-наставник для человека, который почти не умеет готовить. "
    "Для каждого блюда из списка составь рецепт на одну порцию. "
    "Верни ТОЛЬКО JSON-массив без markdown:\n"
    '[{"dish": "точное имя блюда из списка", "title": "название рецепта", '
    '"ingredients": ["Гречка — 60 г", "Соль — по вкусу"], "steps": ["шаг 1", "шаг 2"]}]\n'
    "Ингредиенты бери из указанного состава — граммовки уже даны на одну порцию; "
    "можно добавить воду, соль, специи. Если состав не указан — подбери сам, скромно.\n"
    "Шаги пиши максимально просто и подробно, как для новичка: что достать, в какую "
    "посуду положить, сколько воды или молока, какой огонь, сколько минут, как понять, "
    "что готово. 4–8 коротких шагов, каждый — одно действие. По-русски. Кухня простая, "
    "диетическая, без глютена и без жарки в масле (варка, пар, духовка, гриль)."
)


def targets():
    """Блюда из планов, требующие готовки и ещё не имеющие рецепта."""
    con = connect()
    names = {r["name"] for r in con.execute("SELECT DISTINCT name FROM plan_items")}
    have = {r["dish"] for r in con.execute("SELECT DISTINCT dish FROM recipes")}
    pname = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM products")}
    out = []
    for n in sorted(names - have):
        rows = con.execute("SELECT * FROM dishes WHERE dish=?", (n,)).fetchall()
        if not rows or all(r["type"] != "блюдо" for r in rows):
            continue                     # товары и напитки не готовим
        comp = []
        for r in rows:
            if not r["product"]:
                continue
            p = pname.get(r["product"], r["product"])
            if r["amount"]:
                comp.append("{} — {:g} {}".format(p, r["amount"], r["unit"] or "г"))
            else:
                comp.append(p)
        out.append({"dish": n, "comp": comp})
    return out


async def main():
    if not ANTHROPIC_KEY:
        print("Нет ANTHROPIC_API_KEY — запускать на сервере."); return
    ts = targets()
    print("блюд без рецепта:", len(ts))
    con = connect()
    added = 0
    for i in range(0, len(ts), 8):
        batch = ts[i:i + 8]
        wanted = {t["dish"] for t in batch}
        msg = "\n".join("- {}: состав на порцию: {}".format(
            t["dish"], ", ".join(t["comp"]) or "не расписан") for t in batch)
        ans = await ask_claude(SYSTEM, [{"role": "user", "content": msg}],
                               max_tokens=8000, timeout=180)
        arr = json.loads(ans[ans.find("["):ans.rfind("]") + 1])
        for r in arr:
            dish = str(r.get("dish") or "").strip()
            if dish not in wanted:
                continue
            ings = [{"name": str(s).strip()[:200], "qty": None, "unit": None}
                    for s in (r.get("ingredients") or []) if str(s).strip()]
            steps = [str(s).strip()[:500] for s in (r.get("steps") or []) if str(s).strip()]
            if not steps:
                continue
            con.execute("INSERT INTO recipes(plan_id,dish,title,ingredients_json,steps_json)"
                        " VALUES(NULL,?,?,?,?)",
                        (dish, str(r.get("title") or dish).strip()[:120],
                         json.dumps(ings, ensure_ascii=False),
                         json.dumps(steps, ensure_ascii=False)))
            added += 1
        con.commit()
        print("партия {}: готово {} из {}".format(i // 8 + 1, added, len(ts)))
    print("добавлено рецептов:", added)


if __name__ == "__main__":
    asyncio.run(main())
