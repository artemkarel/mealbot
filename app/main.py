"""FastAPI: API мини-приложения + раздача web/. Все данные — по пользователям."""
from __future__ import annotations
import os, json
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from app.db import connect, init, current_plan, persons_of, set_current_plan, set_persons
from app.cooking import cook_plan, same_days
from app.shopping import build as build_shopping
from app import auth

init()
app = FastAPI(title="Meal plan")
DEV = os.getenv("DEV", "0") == "1"
ADMINS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x}
if DEV: ADMINS.add(0)

MEALS = ["Завтрак", "Обед", "Полдник", "Ужин", "Второй ужин"]
TIMINGS = ["до еды", "во время еды", "после еды"]
KINDS = ["meal", "shopping", "morning", "evening"]

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
    items = con.execute("SELECT * FROM plan_items WHERE plan_id=? AND day_index=?"
                        " ORDER BY meal_index, id", (plan["id"], day)).fetchall()
    recipes = {r["dish"]: {"title": r["title"],
                           "ingredients": json.loads(r["ingredients_json"] or "[]"),
                           "steps": json.loads(r["steps_json"] or "[]")}
               for r in con.execute("SELECT * FROM recipes WHERE plan_id=?", (plan["id"],))}
    # свои рецепты пользователя перекрывают рецепты из файла плана
    for r in con.execute("SELECT * FROM user_recipes WHERE user_id=?", (uid,)):
        recipes[r["dish"]] = _user_recipe(r)
    meals: dict = {}
    for r in items:
        m = meals.setdefault(r["meal"], {"meal": r["meal"], "optional": r["optional"],
                                         "note": r["note"], "items": []})
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
            "same_as": same_days(plan["id"]).get(day, [])}

@app.get("/api/cook")
def cook(day: int = 0, days: int = 1, x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect(); plan = active_plan(con, uid)
    persons = persons_of(con, uid)
    return {"days": days, "persons": persons,
            "list": cook_plan(plan["id"], day, days, persons)}

@app.get("/api/shopping")
def shopping(x_init_data: str = Header(None)):
    uid = me(x_init_data)["id"]
    con = connect(); plan = active_plan(con, uid)
    res = build_shopping(plan["id"], persons=persons_of(con, uid))
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
    for t in ("plan_items", "recipes", "prep_tasks", "meal_logs"):
        con.execute(f"DELETE FROM {t} WHERE plan_id=?", (id,))
    con.execute("DELETE FROM plans WHERE id=?", (id,))
    con.execute("UPDATE user_prefs SET current_plan_id=NULL WHERE current_plan_id=?", (id,))
    con.commit(); return {"ok": True}

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
    dishes = [r["name"] for r in con.execute(
        "SELECT DISTINCT name FROM plan_items WHERE plan_id=? ORDER BY name",
        (plan["id"],))] if plan else []
    return {"recipes": [{**_user_recipe(r), "id": r["id"], "dish": r["dish"]} for r in rows],
            "dishes": dishes}

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
