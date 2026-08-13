"""FastAPI: API мини-приложения + раздача web/. Все данные — по пользователям."""
from __future__ import annotations
import os, json, random
from datetime import date
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from app.db import (connect, init, current_plan, persons_of, set_current_plan,
                    set_persons, day_items)
from app.cooking import cook_plan, same_days
from app.macros import day_macros
from app.shopping import build as build_shopping
from app import ai
from app import auth

init()
app = FastAPI(title="Meal plan")
DEV = os.getenv("DEV", "0") == "1"
ADMINS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x}
if DEV: ADMINS.add(0)

MEALS = ["Завтрак", "Обед", "Полдник", "Ужин", "Второй ужин"]
TIMINGS = ["до еды", "во время еды", "после еды"]
KINDS = ["meal", "shopping", "morning", "evening", "menu"]

def _clean_note(note):
    """Обрезанные заголовки рецептов из файла («Овсяная каша без глютена:») не показываем."""
    note = (note or "").strip()
    return note if note and not note.endswith(":") else None

def me(x_init_data: str | None):
    if DEV: return {"id": 0, "first_name": "dev"}
    user = auth.check(x_init_data or "")
    if not user: raise HTTPException(401, "нет доступа")
    return user

def active_plan(con, uid: int):
    r = current_plan(con, uid)
    if not r: raise HTTPException(404, "План не загружен — пришли боту файл от диетолога")
    return r

@app.get("/api/today")
def today(day: int = 0, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect(); plan = active_plan(con, uid)
    items = day_items(con, uid, plan["id"], day)   # с учётом замен приёмов
    # базовые рецепты (plan_id IS NULL) перекрываются рецептами конкретного плана
    recipes = {r["dish"]: {"title": r["title"],
                           "ingredients": json.loads(r["ingredients_json"] or "[]"),
                           "steps": json.loads(r["steps_json"] or "[]")}
               for r in con.execute(
                   "SELECT * FROM recipes WHERE plan_id IS NULL OR plan_id=?"
                   " ORDER BY plan_id IS NOT NULL", (plan["id"],))}
    # свои рецепты пользователя перекрывают рецепты из файла плана
    for r in con.execute("SELECT * FROM user_recipes WHERE user_id=?", (uid,)):
        recipes[r["dish"]] = _user_recipe(r)
    meals: dict = {}
    for r in items:
        m = meals.setdefault(r["meal"], {"meal": r["meal"], "optional": r["optional"],
                                         "note": _clean_note(r["note"]), "swapped": 0, "items": []})
        m["swapped"] = m["swapped"] or (1 if r.get("swapped") else 0)
        m["items"].append({"name": r["name"], "qty_min": r["qty_min"],
                           "qty_max": r["qty_max"], "unit": r["unit"], "url": r["url"],
                           "recipe": recipes.get(r["name"])})
    logs = {r["meal"]: r["status"] for r in con.execute(
        "SELECT meal, status FROM meal_logs WHERE plan_id=? AND day_index=? AND user_id=?",
        (plan["id"], day, uid))}
    prep = [dict(r) for r in con.execute(
        "SELECT * FROM prep_tasks WHERE plan_id=? AND day_index=?", (plan["id"], day))]
    return {"plan": plan["title"], "persons": persons_of(con, uid), "day": day,
            "day_name": items[0]["day_name"] if items else None,
            "meals": list(meals.values()), "logs": logs, "prep": prep,
            "macros": day_macros(con, items),
            "same_as": same_days(plan["id"]).get(day, [])}

@app.get("/api/cook")
def cook(day: int = 0, days: int = 1, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect(); plan = active_plan(con, uid)
    persons = persons_of(con, uid)
    return {"days": days, "persons": persons,
            "list": cook_plan(plan["id"], day, days, persons,
                              items=day_items(con, uid, plan["id"], day))}

@app.get("/api/shopping")
def shopping(x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect(); plan = active_plan(con, uid)
    week = [it for d in range(7) for it in day_items(con, uid, plan["id"], d)]
    res = build_shopping(plan["id"], persons=persons_of(con, uid), items=week)
    # место покупки: правка пользователя поверх ссылки из файла диетолога
    places = {r["product"]: r["place"] for r in con.execute(
        "SELECT product, place FROM user_places WHERE user_id=?", (uid,))}
    for part in (res["part1"], res["part2"]):
        for it in part:
            it["place"] = places.get(it["id"]) or it["url"]
    return res

@app.post("/api/place")
def set_place(product: str, place: str = "", x_init_data: str = Header(None)):
    """Задать/поправить место покупки товара. Пустая строка — вернуть значение из справочника."""
    uid = me(x_init_data)["id"]
    con = connect()
    if not con.execute("SELECT 1 FROM products WHERE id=?", (product,)).fetchone():
        raise HTTPException(404, "нет такого товара")
    place = place.strip()[:300]
    if place:
        con.execute("INSERT INTO user_places(user_id,product,place) VALUES(?,?,?)"
                    " ON CONFLICT(user_id,product) DO UPDATE SET place=excluded.place",
                    (uid, product, place))
    else:
        con.execute("DELETE FROM user_places WHERE user_id=? AND product=?", (uid, product))
    con.commit(); return {"ok": True}

@app.post("/api/log")
def log(day: int, meal: str, status: str, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect(); plan = active_plan(con, uid)
    con.execute("DELETE FROM meal_logs WHERE plan_id=? AND day_index=? AND meal=? AND user_id=?",
                (plan["id"], day, meal, uid))
    con.execute("INSERT INTO meal_logs(plan_id,day_index,meal,status,user_id) VALUES(?,?,?,?,?)",
                (plan["id"], day, meal, status, uid))
    con.commit(); return {"ok": True}

@app.post("/api/persons")
def persons(n: int, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    set_persons(con, uid, max(1, min(8, n)))
    con.commit(); return {"persons": n}

@app.post("/api/tz")
def set_tz(tz: str = "", x_init_data: str = Header(None)):
    """Часовой пояс телефона — приложение шлёт его при каждом открытии.
    По нему бот считает время напоминаний."""
    uid = me(x_init_data)["id"]
    tz = "".join(c for c in tz.strip() if c.isalnum() or c in "/_+-:")[:64]
    con = connect()
    con.execute("INSERT INTO user_prefs(user_id, tz) VALUES(?,?)"
                " ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz", (uid, tz or None))
    con.commit(); return {"tz": tz}

# ---------- мои и общие планы ----------
# user_id IS NULL — общий план: виден всем, «Открыть» делает его текущим для тебя.
# Отметки хранятся по пользователям, поэтому общим планом можно пользоваться всем сразу.

@app.get("/api/plans")
def plans_list(x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    cur = current_plan(con, uid)
    cur_id = cur["id"] if cur else None
    rows = con.execute(
        "SELECT id, title, created_at, user_id,"
        " (SELECT COUNT(DISTINCT day_index) FROM plan_items WHERE plan_id=plans.id) days"
        " FROM plans WHERE user_id=? OR user_id IS NULL"
        " ORDER BY user_id IS NULL, id DESC", (uid,)).fetchall()
    plans = []
    for r in rows:
        d = dict(r)
        d["shared"] = r["user_id"] is None
        d["active"] = 1 if r["id"] == cur_id else 0
        d["can_delete"] = not d["shared"] or uid in ADMINS
        del d["user_id"]
        plans.append(d)
    return {"plans": plans}

@app.post("/api/plans/activate")
def plan_activate(id: int, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    r = con.execute("SELECT id FROM plans WHERE id=? AND (user_id=? OR user_id IS NULL)",
                    (id, uid)).fetchone()
    if not r: raise HTTPException(404, "это не твой план")
    set_current_plan(con, uid, id)
    con.commit(); return {"ok": True, "plan_id": id}

@app.post("/api/plans/delete")
def plan_delete(id: int, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    r = con.execute("SELECT user_id FROM plans WHERE id=?", (id,)).fetchone()
    if not r or (r["user_id"] is not None and r["user_id"] != uid):
        raise HTTPException(404, "это не твой план")
    if r["user_id"] is None and uid not in ADMINS:
        raise HTTPException(403, "общий план может удалить только админ")
    for t in ("plan_items", "recipes", "prep_tasks", "meal_logs", "meal_overrides"):
        con.execute(f"DELETE FROM {t} WHERE plan_id=?", (id,))
    con.execute("DELETE FROM plans WHERE id=?", (id,))
    con.execute("UPDATE user_prefs SET current_plan_id=NULL WHERE current_plan_id=?", (id,))
    con.commit(); return {"ok": True}

# ---------- генерация случайного меню ----------

DAY_NAMES_FULL = ["Понедельник", "Вторник", "Среда", "Четверг",
                  "Пятница", "Суббота", "Воскресенье"]

@app.post("/api/plans/generate")
def plan_generate(x_init_data: str = Header(None)):
    """Случайная неделя из готовых приёмов пищи всех доступных планов.
    Принцип планов диетолога сохранён: приёмы берутся целиком (состав и граммовки
    не перемешиваются), дни спарены Пн=Вт, Ср=Чт, Пт=Сб + отдельное воскресенье."""
    uid = me(x_init_data)["id"]
    con = connect()
    plan_ids = [r["id"] for r in con.execute(
        "SELECT id FROM plans WHERE user_id=? OR user_id IS NULL", (uid,))]
    pools: dict = {m: {} for m in MEALS}
    for pid in plan_ids:
        groups: dict = {}
        for r in con.execute(
                "SELECT day_index, meal, name, qty_min, qty_max, unit FROM plan_items"
                " WHERE plan_id=? ORDER BY day_index, meal_index, id", (pid,)):
            groups.setdefault((r["day_index"], r["meal"]), []).append(r)
        for (_, meal), items in groups.items():
            if meal not in pools: continue
            sig = tuple(i["name"] for i in items)   # дедуп по составу, граммовки не важны
            pools[meal].setdefault(sig, [
                {"name": i["name"], "qty_min": i["qty_min"],
                 "qty_max": i["qty_max"], "unit": i["unit"]} for i in items])
    if not any(pools.values()):
        raise HTTPException(404, "Нет планов, из которых можно собрать меню")
    # 4 уникальных дневных меню: Пн(=Вт), Ср(=Чт), Пт(=Сб), Вс
    slots = [[] for _ in range(4)]
    for meal in MEALS:
        variants = list(pools[meal].values())
        if not variants: continue
        random.shuffle(variants)
        picks = (variants * (4 // len(variants) + 1))[:4] if len(variants) < 4 else variants[:4]
        for i in range(4):
            slots[i].append({"meal": meal, "optional": int(meal == "Второй ужин"),
                             "items": picks[i]})
    slot_of_day = [0, 0, 1, 1, 2, 2, 3]
    days = [{"day": DAY_NAMES_FULL[d], "meals": slots[slot_of_day[d]]} for d in range(7)]
    return {"days": days}

@app.post("/api/plans/save_generated")
def plan_save_generated(payload: dict, x_init_data: str = Header(None)):
    """Сохранить сгенерированное меню как свой план (в «Мои планы»)."""
    uid = me(x_init_data)["id"]
    days = payload.get("days") or []
    if not isinstance(days, list) or not (1 <= len(days) <= 7):
        raise HTTPException(400, "нечего сохранять")
    con = connect()
    title = f"Случайное меню от {date.today().strftime('%d.%m')}"
    cur = con.execute("INSERT INTO plans(title,source_file,raw_json,active,user_id)"
                      " VALUES(?,?,?,0,?)",
                      (title, "generated", json.dumps(payload, ensure_ascii=False)[:200000], uid))
    pid = cur.lastrowid
    for di, d in enumerate(days[:7]):
        day_name = str(d.get("day") or DAY_NAMES_FULL[di])[:20]
        for mi, m in enumerate((d.get("meals") or [])[:6]):
            meal = str(m.get("meal") or "")[:30]
            if meal not in MEALS: continue
            for it in (m.get("items") or [])[:15]:
                name = str(it.get("name") or "").strip()[:120]
                if not name: continue
                def num(v):
                    try: return float(v)
                    except (TypeError, ValueError): return None
                con.execute(
                    "INSERT INTO plan_items(plan_id,day_index,day_name,meal,meal_index,"
                    "optional,name,qty_min,qty_max,unit) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (pid, di, day_name, meal, mi, int(bool(m.get("optional"))),
                     name, num(it.get("qty_min")), num(it.get("qty_max")),
                     str(it.get("unit") or "")[:10] or None))
    con.commit()
    return {"ok": True, "plan_id": pid, "title": title}

# ---------- импорт плана через ИИ ----------

@app.post("/api/plans/import")
async def plan_import(payload: dict, x_init_data: str = Header(None)):
    """План из файла (PDF/Word/Excel/текст, base64) или вставленного текста.
    Текст извлекаем сами, структуру дней и приёмов разбирает Claude."""
    import base64
    from app.import_ai import extract_text, parse_plan, parse_plan_image, image_media_type
    uid = me(x_init_data)["id"]
    if not ai.ANTHROPIC_KEY:
        raise HTTPException(503, "распознавание не подключено (нет ANTHROPIC_API_KEY)")
    text = str(payload.get("text") or "").strip()
    fname = str(payload.get("filename") or "")[:120]
    media = image_media_type(fname) if payload.get("data_b64") else None
    if not text and not media and payload.get("data_b64"):
        try:
            raw = base64.b64decode(payload["data_b64"])
        except Exception:
            raise HTTPException(400, "файл не дошёл целиком — попробуй ещё раз")
        if len(raw) > 15_000_000:
            raise HTTPException(400, "файл слишком большой (до 15 МБ)")
        try:
            text = extract_text(fname, raw).strip()
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"не смог прочитать файл: {e}")
    if not media and len(text) < 40:
        raise HTTPException(400, "в документе не нашлось текста плана")
    try:
        if media:
            if len(payload["data_b64"]) > 6_500_000:
                raise ValueError("фото слишком большое — пришли поменьше")
            parsed = await parse_plan_image(payload["data_b64"], media)
        else:
            parsed = await parse_plan(text)
    except Exception as e:
        raise HTTPException(422, f"ИИ не смог разобрать план: {e}")
    days = parsed.get("days") or []
    title = str(parsed.get("title") or "").strip()[:80] \
        or (fname.rsplit(".", 1)[0] if fname else f"План от {date.today().strftime('%d.%m')}")
    def num(v):
        try: return float(v)
        except (TypeError, ValueError): return None
    con = connect()
    cur = con.execute("INSERT INTO plans(title,source_file,raw_json,active,user_id)"
                      " VALUES(?,?,?,0,?)",
                      (title, fname or "text", json.dumps(parsed, ensure_ascii=False)[:200000], uid))
    pid = cur.lastrowid
    saved = 0
    for di, d in enumerate(days[:7]):
        day_name = str(d.get("day") or DAY_NAMES_FULL[di])[:20]
        for mi, m in enumerate((d.get("meals") or [])[:6]):
            meal = str(m.get("meal") or "")[:30]
            if meal not in MEALS: continue
            note = str(m.get("note") or "").strip()[:300] or None
            for it in (m.get("items") or [])[:15]:
                name = str(it.get("name") or "").strip()[:120]
                if not name: continue
                con.execute(
                    "INSERT INTO plan_items(plan_id,day_index,day_name,meal,meal_index,"
                    "optional,name,qty_min,qty_max,unit,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, di, day_name, meal, mi, int(bool(m.get("optional"))),
                     name, num(it.get("qty_min")), num(it.get("qty_max")),
                     str(it.get("unit") or "")[:10] or None, note))
                saved += 1
                note = None          # примечание достаточно хранить у первой позиции
    if not saved:
        con.execute("DELETE FROM plans WHERE id=?", (pid,))
        con.commit()
        raise HTTPException(422, "ИИ не нашёл в документе ни одного блюда")
    set_current_plan(con, uid, pid)
    con.commit()
    return {"ok": True, "plan_id": pid, "title": title, "days": min(len(days), 7)}

# ---------- «не хочу это» — замена приёма пищи ----------
# Приём заменяется целиком на такой же приём из другого дня/плана,
# чтобы состав и граммовки диетолога не перемешивались.

def _meal_pool(con, uid: int, meal: str):
    """Все варианты приёма из доступных планов, дедуп по составу."""
    pool: dict = {}
    for p in con.execute("SELECT id FROM plans WHERE user_id=? OR user_id IS NULL", (uid,)):
        groups: dict = {}
        for r in con.execute(
                "SELECT day_index, name, qty_min, qty_max, unit FROM plan_items"
                " WHERE plan_id=? AND meal=? ORDER BY day_index, meal_index, id",
                (p["id"], meal)):
            groups.setdefault(r["day_index"], []).append(r)
        for items in groups.values():
            sig = tuple(i["name"] for i in items)
            pool.setdefault(sig, [{"name": i["name"], "qty_min": i["qty_min"],
                                   "qty_max": i["qty_max"], "unit": i["unit"]}
                                  for i in items])
    return pool

@app.get("/api/swap/options")
def swap_options(day: int, meal: str, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    if meal not in MEALS: raise HTTPException(400, "неизвестный приём пищи")
    con = connect(); plan = active_plan(con, uid)
    cur_sig = tuple(r["name"] for r in day_items(con, uid, plan["id"], day)
                    if r["meal"] == meal)
    variants = [{"items": v} for s, v in _meal_pool(con, uid, meal).items() if s != cur_sig]
    random.shuffle(variants)
    swapped = bool(con.execute(
        "SELECT 1 FROM meal_overrides WHERE user_id=? AND plan_id=? AND day_index=? AND meal=?",
        (uid, plan["id"], day, meal)).fetchone())
    return {"variants": variants[:3], "swapped": swapped}

@app.post("/api/swap")
def swap_apply(payload: dict, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    day = int(payload.get("day", -1)); meal = payload.get("meal")
    if not (0 <= day <= 6): raise HTTPException(400, "неверный день")
    if meal not in MEALS: raise HTTPException(400, "неизвестный приём пищи")
    def num(v):
        try: return float(v)
        except (TypeError, ValueError): return None
    items = [{"name": str(it.get("name") or "").strip()[:120],
              "qty_min": num(it.get("qty_min")), "qty_max": num(it.get("qty_max")),
              "unit": (str(it.get("unit") or "").strip()[:10] or None)}
             for it in (payload.get("items") or [])[:15]]
    items = [it for it in items if it["name"]]
    if not items: raise HTTPException(400, "пустая замена")
    con = connect(); plan = active_plan(con, uid)
    con.execute("INSERT INTO meal_overrides(user_id,plan_id,day_index,meal,items_json)"
                " VALUES(?,?,?,?,?)"
                " ON CONFLICT(user_id,plan_id,day_index,meal)"
                " DO UPDATE SET items_json=excluded.items_json,"
                " created_at=datetime('now')",
                (uid, plan["id"], day, meal, json.dumps(items, ensure_ascii=False)))
    con.commit(); return {"ok": True}

@app.post("/api/swap/revert")
def swap_revert(day: int, meal: str, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect(); plan = active_plan(con, uid)
    con.execute("DELETE FROM meal_overrides"
                " WHERE user_id=? AND plan_id=? AND day_index=? AND meal=?",
                (uid, plan["id"], day, meal))
    con.commit(); return {"ok": True}

# ---------- ИИ-диетолог ----------

@app.post("/api/ai")
async def ai_chat(payload: dict, x_init_data: str = Header(None)):
    """Чат с онлайн-диетологом. История хранится у клиента и приходит с запросом."""
    uid = me(x_init_data)["id"]
    msg = str(payload.get("message") or "").strip()[:2000]
    if not msg: raise HTTPException(400, "напиши вопрос")
    if not ai.ANTHROPIC_KEY:
        raise HTTPException(503, "ИИ-помощник не подключён (нет ANTHROPIC_API_KEY)")
    # история: только валидные реплики, строго чередующиеся, начиная с user
    clean = []
    for h in (payload.get("history") or [])[-8:]:
        role, content = h.get("role"), str(h.get("content") or "").strip()[:2000]
        if role not in ("user", "assistant") or not content: continue
        if clean and clean[-1]["role"] == role:
            clean[-1] = {"role": role, "content": content}
        else:
            clean.append({"role": role, "content": content})
    while clean and clean[0]["role"] != "user": clean.pop(0)
    if clean and clean[-1]["role"] == "user": clean.pop()
    clean.append({"role": "user", "content": msg})
    try:
        answer = await ai.ask_claude(ai.AI_SYSTEM + "\n\n" + ai.ai_context(uid), clean)
    except Exception as e:
        raise HTTPException(502, f"не получилось спросить: {e}")
    if not answer:
        raise HTTPException(502, "помощник промолчал — попробуй переформулировать")
    text, recipe = ai.split_recipe(answer)
    return {"answer": text, "recipe": recipe}

# ---------- напоминания ----------

@app.get("/api/reminders")
def reminders_list(x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    rows = con.execute("SELECT id, kind, meal, time FROM user_reminders"
                       " WHERE user_id=? ORDER BY time", (uid,)).fetchall()
    return {"reminders": [dict(r) for r in rows]}

@app.post("/api/reminders/add")
def reminder_add(kind: str, time: str, meal: str = None, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    if kind not in KINDS: raise HTTPException(400, "неизвестный тип напоминания")
    if kind == "meal" and meal not in MEALS: raise HTTPException(400, "выбери приём пищи")
    try:
        h, m = time.split(":"); assert 0 <= int(h) < 24 and 0 <= int(m) < 60
    except Exception:
        raise HTTPException(400, "время в формате ЧЧ:ММ")
    con = connect()
    con.execute("INSERT INTO user_reminders(user_id,kind,meal,time) VALUES(?,?,?,?)",
                (uid, kind, meal if kind == "meal" else None, f"{int(h):02d}:{int(m):02d}"))
    con.commit(); return {"ok": True}

@app.post("/api/reminders/delete")
def reminder_delete(id: int, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    con.execute("DELETE FROM user_reminders WHERE id=? AND user_id=?", (id, uid))
    con.commit(); return {"ok": True}

# ---------- свои рецепты ----------

def _user_recipe(r) -> dict:
    ings = json.loads(r["ingredients_json"] or "[]")
    return {"title": r["title"],
            "ingredients": [{"name": i, "qty": None, "unit": None} if isinstance(i, str) else i
                            for i in ings],
            "steps": json.loads(r["steps_json"] or "[]")}

@app.get("/api/recipes")
def recipes_list(x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    rows = con.execute("SELECT * FROM user_recipes WHERE user_id=? ORDER BY id DESC",
                       (uid,)).fetchall()
    plan = current_plan(con, uid)
    dishes, plan_recipes, seen = [], [], set()
    if plan:
        dishes = [r["name"] for r in con.execute(
            "SELECT DISTINCT name FROM plan_items WHERE plan_id=? ORDER BY name",
            (plan["id"],))]
        # рецепты из файла диетолога + базовые (plan_id IS NULL); дубли схлопываем
        for r in con.execute("SELECT * FROM recipes WHERE plan_id=? OR plan_id IS NULL"
                             " ORDER BY plan_id IS NULL", (plan["id"],)):
            key = (r["dish"], r["title"])
            if key in seen: continue
            seen.add(key)
            plan_recipes.append({"dish": r["dish"], "title": r["title"],
                                 "ingredients": json.loads(r["ingredients_json"] or "[]"),
                                 "steps": json.loads(r["steps_json"] or "[]")})
    return {"recipes": [{**_user_recipe(r), "id": r["id"], "dish": r["dish"]} for r in rows],
            "plan_recipes": plan_recipes, "dishes": dishes}

@app.post("/api/recipes/add")
def recipe_add(payload: dict, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    title = (payload.get("title") or "").strip()[:120]
    dish = (payload.get("dish") or "").strip()[:120]
    if not title: raise HTTPException(400, "впиши название рецепта")
    if not dish: raise HTTPException(400, "укажи блюдо, к которому прикрепить")
    ings = [str(s).strip()[:200] for s in payload.get("ingredients", []) if str(s).strip()]
    steps = [str(s).strip()[:500] for s in payload.get("steps", []) if str(s).strip()]
    con = connect()
    con.execute("INSERT INTO user_recipes(user_id,dish,title,ingredients_json,steps_json)"
                " VALUES(?,?,?,?,?)",
                (uid, dish, title, json.dumps(ings, ensure_ascii=False),
                 json.dumps(steps, ensure_ascii=False)))
    con.commit(); return {"ok": True}

@app.post("/api/recipes/delete")
def recipe_delete(id: int, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    con.execute("DELETE FROM user_recipes WHERE id=? AND user_id=?", (id, uid))
    con.commit(); return {"ok": True}

# ---------- БАДы ----------

@app.get("/api/supplements")
def supplements_list(x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    rows = con.execute("SELECT id, name, dose, meal, timing FROM supplements"
                       " WHERE user_id=? ORDER BY meal, timing, name", (uid,)).fetchall()
    return {"supplements": [dict(r) for r in rows]}

@app.post("/api/supplements/add")
def supplement_add(name: str, meal: str, timing: str, dose: str = "",
                   x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    name = name.strip()[:100]
    if not name: raise HTTPException(400, "впиши название")
    if meal not in MEALS: raise HTTPException(400, "выбери приём пищи")
    if timing not in TIMINGS: raise HTTPException(400, "когда принимать: до/во время/после еды")
    con = connect()
    con.execute("INSERT INTO supplements(user_id,name,dose,meal,timing) VALUES(?,?,?,?,?)",
                (uid, name, dose.strip()[:100] or None, meal, timing))
    con.commit(); return {"ok": True}

@app.post("/api/supplements/delete")
def supplement_delete(id: int, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect()
    con.execute("DELETE FROM supplements WHERE id=? AND user_id=?", (id, uid))
    con.commit(); return {"ok": True}

# ---------- холодильник ----------

@app.post("/api/unpack")
def unpack(payload: dict, x_init_data: str = Header(None)):
    """Разбор пакетов: записывает покупки и проставляет сроки годности."""
    uid = me(x_init_data)["id"]
    con = connect(); saved = 0
    for it in payload.get("items", []):
        prod = con.execute("SELECT * FROM products WHERE id=?", (it.get("product"),)).fetchone()
        if not prod: continue
        frozen = int(bool(it.get("frozen")))
        shelf = 60 if frozen else prod["shelf_days"]
        con.execute(
            "INSERT INTO purchases(product,amount,unit,expires_at,frozen,user_id)"
            " VALUES(?,?,?,date('now', ?),?,?)",
            (prod["id"], it.get("amount"), it.get("unit") or prod["unit"],
             f"+{shelf} day", frozen, uid))
        saved += 1
    con.commit(); return {"saved": saved}

@app.get("/api/fridge")
def fridge(x_init_data: str = Header(None)):
    """Что лежит в холодильнике/морозилке и сколько дней осталось."""
    uid = me(x_init_data)["id"]
    con = connect()
    rows = con.execute(
        "SELECT pu.id, pu.product, pr.name, pu.amount, pu.unit, pu.frozen,"
        " pu.bought_at, pu.expires_at,"
        " CAST(julianday(pu.expires_at) - julianday(date('now')) AS INT) days_left"
        " FROM purchases pu JOIN products pr ON pr.id = pu.product"
        " WHERE pu.used=0 AND pu.user_id=? ORDER BY pu.frozen, days_left, pr.name",
        (uid,)).fetchall()
    return {"items": [dict(r) for r in rows]}

@app.post("/api/use")
def use(id: int, x_init_data: str = Header(None)):
    """Отметить покупку использованной (или вернуть обратно)."""
    uid = me(x_init_data)["id"]
    con = connect()
    con.execute("UPDATE purchases SET used = 1 - used WHERE id=? AND user_id=?", (id, uid))
    con.commit(); return {"ok": True}

app.mount("/", StaticFiles(directory="web", html=True), name="web")
