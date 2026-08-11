"""FastAPI: API мини-приложения + раздача web/."""
from __future__ import annotations
import os, json
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from app.db import connect, init
from app.cooking import cook_plan, same_days
from app.shopping import build as build_shopping
from app import auth

init()
app = FastAPI(title="Meal plan")
DEV = os.getenv("DEV", "0") == "1"

def me(x_init_data: str | None):
    if DEV: return {"id": 0, "first_name": "dev"}
    user = auth.check(x_init_data or "")
    if not user: raise HTTPException(401, "нет доступа")
    return user

def active_plan(con):
    r = con.execute("SELECT * FROM plans WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
    if not r: raise HTTPException(404, "план не загружен")
    return r

@app.get("/api/today")
def today(day: int = 0, x_init_data: str = Header(None)):
    me(x_init_data)
    con = connect(); plan = active_plan(con)
    items = con.execute("SELECT * FROM plan_items WHERE plan_id=? AND day_index=?"
                        " ORDER BY meal_index, id", (plan["id"], day)).fetchall()
    recipes = {r["dish"]: {"title": r["title"],
                           "ingredients": json.loads(r["ingredients_json"] or "[]"),
                           "steps": json.loads(r["steps_json"] or "[]")}
               for r in con.execute("SELECT * FROM recipes WHERE plan_id=?", (plan["id"],))}
    meals: dict = {}
    for r in items:
        m = meals.setdefault(r["meal"], {"meal": r["meal"], "optional": r["optional"],
                                         "note": r["note"], "recipe": None, "items": []})
        m["items"].append({"name": r["name"], "qty_min": r["qty_min"],
                           "qty_max": r["qty_max"], "unit": r["unit"], "url": r["url"]})
        if r["name"] in recipes: m["recipe"] = recipes[r["name"]]
    logs = {r["meal"]: r["status"] for r in con.execute(
        "SELECT meal, status FROM meal_logs WHERE plan_id=? AND day_index=?", (plan["id"], day))}
    prep = [dict(r) for r in con.execute(
        "SELECT * FROM prep_tasks WHERE plan_id=? AND day_index=?", (plan["id"], day))]
    return {"plan": plan["title"], "persons": plan["persons"], "day": day,
            "day_name": items[0]["day_name"] if items else None,
            "meals": list(meals.values()), "logs": logs, "prep": prep,
            "same_as": same_days(plan["id"]).get(day, [])}

@app.get("/api/cook")
def cook(day: int = 0, days: int = 1, x_init_data: str = Header(None)):
    me(x_init_data)
    con = connect(); plan = active_plan(con)
    return {"days": days, "persons": plan["persons"],
            "list": cook_plan(plan["id"], day, days, plan["persons"])}

@app.get("/api/shopping")
def shopping(x_init_data: str = Header(None)):
    me(x_init_data)
    con = connect(); plan = active_plan(con)
    return build_shopping(plan["id"], persons=plan["persons"])

@app.post("/api/log")
def log(day: int, meal: str, status: str, x_init_data: str = Header(None)):
    me(x_init_data)
    con = connect(); plan = active_plan(con)
    con.execute("DELETE FROM meal_logs WHERE plan_id=? AND day_index=? AND meal=?",
                (plan["id"], day, meal))
    con.execute("INSERT INTO meal_logs(plan_id,day_index,meal,status) VALUES(?,?,?,?)",
                (plan["id"], day, meal, status))
    con.commit(); return {"ok": True}

@app.post("/api/unpack")
def unpack(payload: dict, x_init_data: str = Header(None)):
    """Разбор пакетов: записывает покупки и проставляет сроки годности."""
    me(x_init_data)
    con = connect(); saved = 0
    for it in payload.get("items", []):
        prod = con.execute("SELECT * FROM products WHERE id=?", (it.get("product"),)).fetchone()
        if not prod: continue
        frozen = int(bool(it.get("frozen")))
        shelf = 60 if frozen else prod["shelf_days"]
        con.execute(
            "INSERT INTO purchases(product,amount,unit,expires_at,frozen)"
            " VALUES(?,?,?,date('now', ?),?)",
            (prod["id"], it.get("amount"), it.get("unit") or prod["unit"],
             f"+{shelf} day", frozen))
        saved += 1
    con.commit(); return {"saved": saved}

@app.get("/api/fridge")
def fridge(x_init_data: str = Header(None)):
    """Что лежит в холодильнике/морозилке и сколько дней осталось."""
    me(x_init_data)
    con = connect()
    rows = con.execute(
        "SELECT pu.id, pu.product, pr.name, pu.amount, pu.unit, pu.frozen,"
        " pu.bought_at, pu.expires_at,"
        " CAST(julianday(pu.expires_at) - julianday(date('now')) AS INT) days_left"
        " FROM purchases pu JOIN products pr ON pr.id = pu.product"
        " WHERE pu.used=0 ORDER BY pu.frozen, days_left, pr.name").fetchall()
    return {"items": [dict(r) for r in rows]}

@app.post("/api/use")
def use(id: int, x_init_data: str = Header(None)):
    """Отметить покупку использованной (или вернуть обратно)."""
    me(x_init_data)
    con = connect()
    con.execute("UPDATE purchases SET used = 1 - used WHERE id=?", (id,))
    con.commit(); return {"ok": True}

@app.post("/api/persons")
def persons(n: int, x_init_data: str = Header(None)):
    me(x_init_data)
    con = connect(); plan = active_plan(con)
    con.execute("UPDATE plans SET persons=? WHERE id=?", (max(1, min(8, n)), plan["id"]))
    con.commit(); return {"persons": n}

app.mount("/", StaticFiles(directory="web", html=True), name="web")
