# -*- coding: utf-8 -*-
"""HTTP-часть бота для Mini App.

Работает внутри того же процесса, что и бот, поэтому использует ту же базу:
что отметил в приложении — видит бот, и наоборот.

Telegram передаёт приложению строку initData, подписанную ботовым токеном.
Проверяем подпись — так узнаём, кто именно открыл приложение, без паролей.
"""
import os, json, hmac, hashlib, logging, time
from urllib.parse import parse_qsl

from aiohttp import web

import store

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAX_AGE = 24 * 3600          # initData старше суток не принимаем


def check_init_data(init_data: str):
    """Проверить подпись Telegram. Возвращает id пользователя или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", None)
        if not got:
            return None
        check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > MAX_AGE:
            return None
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        logging.warning("initData не прошла проверку")
        return None


async def body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def user_state(uid):
    """Всё, что нужно приложению для отрисовки."""
    weeks = store.all_weeks() + store.all_generated()
    return {
        "plan": {
            "weeks": weeks,
            "recipes": store.all_recipes(),
            "dish_ingredients": store.dish_ingredients(),
            "cafe": store.PLAN.get("cafe", []),      # места из плана диетолога
        },
        "user": {
            "uid": uid,
            "people": store.get_people(uid),
            "active": store.get_user_setting(uid, "active_plan"),
            "checks": sorted(store.checked_set(uid, "")),
            "extras": [dict(e, wid=w["id"])
                       for w in weeks for e in store.all_extras(uid, w["id"])],
            "supps": store.all_supps(uid),
            "cafe": store.all_cafe(uid),
        },
    }


async def h_state(request):
    uid = check_init_data((await body(request)).get("initData", ""))
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    return web.json_response(user_state(uid))


async def h_act(request):
    data = await body(request)
    uid = check_init_data(data.get("initData", ""))
    if not uid:
        return web.json_response({"error": "auth"}, status=401)
    act = data.get("action")
    try:
        if act == "toggle":
            store.toggle_check(uid, data["iid"])
        elif act == "people":
            store.set_people(uid, data["value"])
        elif act == "active":
            store.set_user_setting(uid, "active_plan", data["wid"])
            store.set_user_setting(uid, "active_started", data["today"])
        elif act == "reset":
            ids = data.get("ids") or []
            store.uncheck_many(uid, ids)
        elif act == "extra_add":
            for name in data.get("names", [])[:20]:
                store.add_extra(uid, data["wid"], data.get("trip", 1), name[:80])
        elif act == "extra_del":
            store.del_extra(uid, data["wid"], data["eid"])
        elif act == "supp_add":
            store.add_supp(uid, data["name"][:60], data.get("dose", "")[:40], "Завтрак")
        elif act == "supp_slots":
            store.set_supp_slots(uid, data["sid"], data.get("slots", ""))
        elif act == "supp_timing":
            store.set_supp_timing(uid, data["sid"], data.get("timing", ""))
        elif act == "cafe_add":
            store.add_cafe(uid, data["name"][:80], data.get("place", "")[:40],
                           data.get("slot", "Обед"))
        elif act == "cafe_del":
            store.del_cafe(uid, data["cid"])
        elif act == "supp_del":
            store.del_supp(uid, data["sid"])
        elif act == "gen_save":
            store.add_generated(data["week"])
        elif act == "recipe_save":
            r = store.get_recipe(data["rid"])
            if r:
                r["text"] = data.get("text", "")
                store.save_recipe(r)
        else:
            return web.json_response({"error": "unknown action"}, status=400)
    except KeyError as e:
        return web.json_response({"error": f"нет поля {e}"}, status=400)
    return web.json_response(user_state(uid))


def make_app():
    app = web.Application()
    app.router.add_post("/api/state", h_state)
    app.router.add_post("/api/act", h_act)
    return app


async def start(host="127.0.0.1", port=8081):
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logging.info("Mini App API слушает %s:%s", host, port)
    return runner
