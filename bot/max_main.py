"""Бот в мессенджере MAX: те же команды, ИИ-диетолог и напоминания, общая база.

Пользователи MAX хранятся с общим сдвигом id (MAX_UID_OFFSET), поэтому их планы,
настройки и напоминания живут в тех же таблицах и работают в том же приложении.
Без MAX_BOT_TOKEN в .env процесс тихо завершается — сервис можно держать включённым.
"""
import asyncio, json, os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import aiohttp
from app.db import connect, current_plan, persons_of, touch_user, MAX_UID_OFFSET
from app.texts import (day_menu_text, build_morning, build_evening, build_meal,
                       build_menu, build_shopping_note, user_tz, _minutes)
from app.ai import ask_claude, ai_context, split_recipe, AI_SYSTEM, ANTHROPIC_KEY

TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
BASE = os.getenv("MAX_API_BASE", "https://platform-api2.max.ru").rstrip("/")
URL = os.getenv("WEBAPP_URL", "").strip()

COMMANDS = [
    {"name": "start", "description": "Что умеет бот"},
    {"name": "today", "description": "Что сегодня едим"},
    {"name": "tomorrow", "description": "Что завтра"},
    {"name": "myid", "description": "Мой ID"},
    {"name": "status", "description": "Состояние"},
]

KB_ROW = [{"type": "link", "text": "Открыть план", "url": URL}] if URL else None


async def api(method, path, params=None, body=None, timeout=60):
    async with aiohttp.ClientSession() as s:
        async with s.request(method, BASE + path, params=params, json=body,
                             headers={"Authorization": TOKEN},
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            if r.status >= 400:
                raise RuntimeError(f"{method} {path}: HTTP {r.status} {str(data)[:200]}")
            return data


async def send(text, user_id=None, chat_id=None, with_kb=True):
    params = {"chat_id": chat_id} if chat_id is not None else {"user_id": user_id}
    body = {"text": text[:3900]}
    if with_kb and KB_ROW:
        body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": [KB_ROW]}}]
    return await api("POST", "/messages", params=params, body=body)


async def try_delete(mid):
    for attempt in ({"params": {"message_id": mid}}, {"params": {"mid": mid}}):
        try:
            await api("DELETE", "/messages", **attempt)
            return
        except Exception:
            continue


# ---------- чат «в одном окне» (как в Telegram-боте) ----------

def _clean_ids(chat_id):
    con = connect()
    r = con.execute("SELECT value FROM settings WHERE key=?",
                    ("cleanmax:%s" % chat_id,)).fetchone()
    try:
        return json.loads(r["value"]) if r else []
    except Exception:
        return []


def _save_clean_ids(chat_id, ids):
    con = connect()
    con.execute("INSERT INTO settings(key,value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("cleanmax:%s" % chat_id, json.dumps(ids[-20:])))
    con.commit()


async def reply_clean(chat_id, incoming_mid, text, with_kb=True):
    for mid in _clean_ids(chat_id):
        await try_delete(mid)
    data = await send(text, chat_id=chat_id, with_kb=with_kb)
    new_mid = ((data.get("message") or {}).get("body") or {}).get("mid")
    _save_clean_ids(chat_id, [m for m in (incoming_mid, new_mid) if m])


# ---------- обработка сообщений ----------

AI_HISTORY = {}

START_TEXT = ("Привет! Я помогаю питаться по плану диетолога.\n\n"
              "Команды: /today — меню на сегодня, /tomorrow — на завтра.\n"
              "А ещё можно просто написать вопрос о питании — отвечу как "
              "ИИ-диетолог, который знает твой план.\n\n"
              "Планы, настройки и напоминания — в приложении по кнопке ниже.")


async def handle_message(msg):
    sender = msg.get("sender") or {}
    if sender.get("is_bot"):
        return
    max_id = sender.get("user_id")
    chat_id = (msg.get("recipient") or {}).get("chat_id")
    body = msg.get("body") or {}
    text = (body.get("text") or "").strip()
    mid = body.get("mid")
    if not max_id or chat_id is None:
        return
    uid = MAX_UID_OFFSET + int(max_id)
    try:
        name = sender.get("first_name") or sender.get("name")
        touch_user(connect(), uid, name, sender.get("last_name"),
                   sender.get("username"), "max")
    except Exception:
        pass

    cmd = text.split()[0].lower() if text.startswith("/") else ""
    if cmd in ("/start", "/help"):
        return await reply_clean(chat_id, mid, START_TEXT)
    if cmd == "/today":
        return await reply_clean(chat_id, mid, day_menu_text(0, uid))
    if cmd == "/tomorrow":
        return await reply_clean(chat_id, mid, day_menu_text(1, uid))
    if cmd == "/myid":
        return await reply_clean(chat_id, mid, f"Твой MAX ID: {max_id}")
    if cmd == "/status":
        con = connect()
        plan = current_plan(con, uid)
        head = (f"План: {plan['title']}" if plan else "План не загружен.")
        rems = con.execute("SELECT COUNT(*) c FROM user_reminders"
                           " WHERE user_id=? AND enabled=1", (uid,)).fetchone()["c"]
        return await reply_clean(chat_id, mid,
            f"{head}\nЧеловек: {persons_of(con, uid)}, напоминаний: {rems}\n"
            "Настройки — в приложении (кнопка ниже).")
    if cmd:
        return await reply_clean(chat_id, mid,
            "Такой команды нет. Просто напиши вопрос текстом — отвечу как диетолог.")
    if not text:
        return await reply_clean(chat_id, mid,
            "Пока понимаю только текст. Файлы плана можно добавить в приложении "
            "(кнопка ниже) — файлом, фото или текстом.")
    if not ANTHROPIC_KEY:
        return await reply_clean(chat_id, mid,
            "ИИ-помощник ещё не подключён на сервере.")
    hist = AI_HISTORY.setdefault(uid, [])
    hist.append({"role": "user", "content": text[:2000]})
    del hist[:-8]
    try:
        answer = await ask_claude(AI_SYSTEM + "\n\n" + ai_context(uid), list(hist))
    except Exception as e:
        hist.pop()
        return await reply_clean(chat_id, mid, f"Не получилось спросить помощника: {e}")
    if not answer:
        hist.pop()
        return await reply_clean(chat_id, mid, "Помощник промолчал — попробуй переформулировать.")
    hist.append({"role": "assistant", "content": answer})
    clean, _ = split_recipe(answer)
    await reply_clean(chat_id, mid, clean[:3900])


async def handle_update(u):
    t = u.get("update_type")
    if t == "message_created":
        await handle_message(u.get("message") or {})
    elif t == "bot_started":
        chat_id = u.get("chat_id")
        user = u.get("user") or {}
        if chat_id is not None:
            uid = MAX_UID_OFFSET + int(user.get("user_id") or 0)
            try:
                touch_user(connect(), uid, user.get("first_name") or user.get("name"),
                           user.get("last_name"), user.get("username"), "max")
            except Exception:
                pass
            await send(START_TEXT, chat_id=chat_id)


# ---------- напоминания (только пользователи MAX) ----------

REMINDER_FALLBACK = {
    "evening": "🌙 На завтра ничего готовить с вечера не нужно — отдыхай.",
    "morning": "🎒 Сегодня собирать с собой ничего не нужно.",
}


async def fire_reminder(r):
    uid = r["user_id"]
    builders = {"morning": lambda: build_morning(uid),
                "evening": lambda: build_evening(uid),
                "shopping": lambda: build_shopping_note(uid),
                "menu": lambda: build_menu(uid),
                "meal": lambda: build_meal(uid, r["meal"])}
    text = builders.get(r["kind"], lambda: "")() or REMINDER_FALLBACK.get(r["kind"], "")
    if text:
        await send(text, user_id=uid - MAX_UID_OFFSET)


async def reminder_loop():
    sent = set()
    while True:
        now_utc = datetime.now(timezone.utc)
        try:
            con = connect()
            tz_cache = {}
            for r in con.execute("SELECT * FROM user_reminders WHERE enabled=1"
                                 " AND user_id >= ?", (MAX_UID_OFFSET,)):
                uid = r["user_id"]
                if uid not in tz_cache:
                    tz_cache[uid] = user_tz(con, uid)
                local = now_utc.astimezone(tz_cache[uid]) if tz_cache[uid] else datetime.now()
                cur = local.hour * 60 + local.minute
                key = (local.date(), r["id"])
                try:
                    t = _minutes(r["time"])
                except Exception:
                    continue
                if t <= cur < t + 30 and key not in sent:
                    sent.add(key)
                    try:
                        await fire_reminder(r)
                    except Exception as e:
                        print(f"Ошибка напоминания MAX #{r['id']}: {e}")
        except Exception as e:
            print(f"Ошибка цикла напоминаний MAX: {e}")
        if len(sent) > 500:
            today_utc = now_utc.date()
            sent = {k for k in sent if (k[0] - today_utc).days >= -1}
        await asyncio.sleep(60)


# ---------- long polling ----------

def _marker_load():
    r = connect().execute("SELECT value FROM settings WHERE key='max:marker'").fetchone()
    try:
        return int(r["value"]) if r else None
    except Exception:
        return None


def _marker_save(m):
    con = connect()
    con.execute("INSERT INTO settings(key,value) VALUES('max:marker',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(m),))
    con.commit()


async def set_commands():
    for attempt in (("PATCH", "/me", {"commands": COMMANDS}),
                    ("PATCH", "/me/commands", {"commands": COMMANDS})):
        try:
            await api(attempt[0], attempt[1], body=attempt[2])
            return
        except Exception as e:
            err = e
    print(f"Не удалось задать команды: {err}")


async def main():
    if not TOKEN:
        print("MAX_BOT_TOKEN не задан — бот MAX выключен.")
        return
    me = await api("GET", "/me")
    print("MAX-бот запущен:", me.get("username") or me.get("name") or me)
    await set_commands()
    asyncio.create_task(reminder_loop())
    marker = _marker_load()
    while True:
        try:
            params = {"timeout": 30, "limit": 50}
            if marker is not None:
                params["marker"] = marker
            data = await api("GET", "/updates", params=params, timeout=45)
            for u in data.get("updates") or []:
                try:
                    await handle_update(u)
                except Exception as e:
                    print(f"Ошибка обработки апдейта: {e}")
            if data.get("marker") is not None:
                marker = data["marker"]
                _marker_save(marker)
        except Exception as e:
            print(f"Ошибка long polling: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
