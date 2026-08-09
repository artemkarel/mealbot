# -*- coding: utf-8 -*-
"""Телеграм-бот плана питания: расписание, закупки с галочками (общие для семьи),
счётчик людей, рецепты с редактированием, обновление плана файлом plan.json,
выбор блюд -> список покупок, напоминания. Без внешних ИИ-сервисов."""
import os, io, math, time, random, asyncio, logging, subprocess
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import store

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = os.environ.get("TZ", "Europe/Samara")   # Ижевск, UTC+4
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}

bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

TRIP_HDR = {1: "🟢 ЗАКУП 1 — начало недели (вс/пн)", 2: "🟣 ЗАКУП 2 — середина недели (ср/чт)"}
RECIPE_PREFIX = [("Овощной суп на костном бульоне", "veg"), ("Уха", "uha"),
                 ("Венские вафли", "vafli"), ("Морковная запеканка", "zap")]


# ---------- helpers ----------
def B(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def KB(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fmtqty(q, u, people):
    if q is None:
        return None
    total = q * people
    if u in ("г", "мл"):
        return f"{round(total / 10) * 10} {u}"
    return f"{math.ceil(total)} {u}"


def recipe_for(dish):
    for p, rid in RECIPE_PREFIX:
        if dish.startswith(p):
            return rid
    return None


async def safe_edit(msg: Message, text, kb):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # "message is not modified" и т.п.


# ---------- текущее меню и «что сегодня» ----------
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def tznow():
    try:
        return datetime.now(ZoneInfo(TZ))
    except Exception:
        return datetime.now()


def active_plan():
    wid = store.get_setting("active_plan")
    return store.get_week(wid) if wid else None


def day_for_date(w, d):
    """Какой день плана соответствует дате. Сначала по названию дня недели,
    иначе — отсчётом от даты включения меню."""
    days = w.get("days", [])
    if not days:
        return None
    want = WEEKDAYS[d.weekday()].lower()
    for day in days:
        if day.get("name", "").strip().lower() == want:
            return day
    started = store.get_setting("active_started")
    if started:
        try:
            s = date.fromisoformat(started)
            return days[max(0, (d - s).days) % len(days)]
        except Exception:
            pass
    return days[d.weekday() % len(days)]


def meals_equal(a, b):
    if not a or not b:
        return False
    return [(m["t"], tuple(m["d"])) for m in a["meals"]] == [(m["t"], tuple(m["d"])) for m in b["meals"]]


def recipes_in(day):
    out, seen = [], set()
    for m in day["meals"]:
        for d in m["d"]:
            rid = recipe_for(d)
            if rid and rid not in seen:
                seen.add(rid)
                r = store.get_recipe(rid)
                if r:
                    out.append(r)
    return out


def day_card(w, d, head):
    """Текст с меню на дату d + кнопки рецептов."""
    day = day_for_date(w, d)
    if not day:
        return None, None
    prev = day_for_date(w, d - timedelta(days=1))
    same_as_yesterday = meals_equal(day, prev)
    lines = [f"{head} · {day['name']}, {d.strftime('%d.%m')}", f"Меню: {w['label']}", ""]
    for m in day["meals"]:
        if not m["d"]:
            continue
        lines.append(f"🍽 {m['t']}")
        lines += [f"   • {x}" for x in m["d"]]
        lines.append("")
    if same_as_yesterday:
        lines.append("♻️ Сегодня то же, что вчера — готовить заново не нужно.")
    else:
        nxt = day_for_date(w, d + timedelta(days=1))
        if meals_equal(day, nxt):
            lines.append("🍳 Новые блюда. Готовим сразу на два дня — завтра то же самое.")
        else:
            lines.append("🍳 Новые блюда на сегодня.")
    rows = [[B(f"📖 {r['name']}", f"rv:{r['id']}")] for r in recipes_in(day)]
    rows.append([B("📅 Вся неделя", f"sw:{w['id']}"), B("🛒 Закупки", f"shop:{w['id']}")])
    rows.append([B("⌂ Меню", "menu")])
    return "\n".join(lines).strip(), KB(rows)


async def send_day(chat_id, head, shift=0):
    w = active_plan()
    if not w:
        return False
    txt, kb = day_card(w, tznow().date() + timedelta(days=shift), head)
    if not txt:
        return False
    await bot.send_message(chat_id, txt, reply_markup=kb)
    return True


@dp.message(Command("today"))
async def cmd_today(m: Message):
    if not await send_day(m.chat.id, "🍽 Сегодня"):
        await m.answer("Сначала выбери текущее меню.", reply_markup=KB([[B("▶️ Выбрать меню", "actsel")]]))


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message):
    if not await send_day(m.chat.id, "🌙 Завтра", 1):
        await m.answer("Сначала выбери текущее меню.", reply_markup=KB([[B("▶️ Выбрать меню", "actsel")]]))


@dp.callback_query(F.data.in_({"today", "tomorrow"}))
async def cb_today(c: CallbackQuery):
    await c.answer()
    head, shift = ("🍽 Сегодня", 0) if c.data == "today" else ("🌙 Завтра", 1)
    if not await send_day(c.message.chat.id, head, shift):
        await c.message.answer("Сначала выбери текущее меню.",
                               reply_markup=KB([[B("▶️ Выбрать меню", "actsel")]]))


# ---------- выбор текущего меню ----------
@dp.callback_query(F.data == "actsel")
async def cb_active_select(c: CallbackQuery):
    cur = store.get_setting("active_plan")
    rows = [[B("— 📅 Из расписания", "noop")]]
    for w in store.all_weeks():
        mark = "✅ " if w["id"] == cur else ""
        rows.append([B(f"{mark}{w['label']} · {w['dates']}".strip(" ·"), f"act:{w['id']}")])
    gen = store.all_generated()
    if gen:
        rows.append([B("— 🎲 Случайные меню", "noop")])
        for g in gen:
            mark = "✅ " if g["id"] == cur else ""
            rows.append([B(f"{mark}{g['label']}", f"act:{g['id']}")])
    if cur:
        rows.append([B("✖︎ Не питаться по плану", "actoff")])
    rows.append([B("‹ Настройки", "settings"), B("⌂ Меню", "menu")])
    await safe_edit(c.message, "По какому меню сейчас питаемся?\n"
                               "От него зависят утренние напоминания «что сегодня».", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("act:"))
async def cb_active_set(c: CallbackQuery):
    wid = c.data.split(":", 1)[1]
    w = store.get_week(wid)
    if not w:
        return await c.answer("Меню не найдено", show_alert=True)
    store.set_setting("active_plan", wid)
    store.set_setting("active_started", tznow().date().isoformat())
    await c.answer("Меню выбрано")
    txt, kb = day_card(w, tznow().date(), "🍽 Сегодня")
    await safe_edit(c.message, f"✅ Питаемся по: {w['label']}\n\n" + (txt or ""), kb)


@dp.callback_query(F.data == "actoff")
async def cb_active_off(c: CallbackQuery):
    store.del_setting("active_plan")
    await c.answer("Выключено")
    await cb_settings(c)


# ---------- FSM ----------
class Edit(StatesGroup):
    recipe_text = State()
    new_name = State()
    new_text = State()


# ---------- access control ----------
@dp.update.outer_middleware()
async def auth_mw(handler, event, data):
    user = data.get("event_from_user")
    if ALLOWED and (user is None or user.id not in ALLOWED):
        return
    return await handler(event, data)


# ---------- menu ----------
def menu_kb():
    return KB([
        [B("🍽 Что сегодня", "today"), B("📅 Расписание", "schweeks")],
        [B("🛒 Закупки", "shopsrc"), B("📖 Рецепты", "recs")],
        [B("🎲 Случайное меню", "gen"), B("⚙️ Настройки", "settings")],
    ])


def menu_text():
    w = active_plan()
    cur = f"▶️ Сейчас питаемся по: {w['label']}" if w else "▶️ Текущее меню не выбрано (Настройки → Текущее меню)"
    return ("🍲 План питания\n\n"
            "• Что сегодня — меню на день и что готовить\n"
            "• Расписание — меню по дням и неделям\n"
            "• Закупки — выбери меню, а в нём собери список покупок\n"
            "• Рецепты — смотреть и редактировать\n"
            "• Случайное меню — соберу неделю из уже знакомых блюд\n\n"
            + cur)


@dp.message(CommandStart())
async def start(m: Message):
    store.add_chat(m.chat.id)
    await m.answer(menu_text(), reply_markup=menu_kb())


@dp.message(Command("menu"))
async def menu_cmd(m: Message):
    await m.answer(menu_text(), reply_markup=menu_kb())


@dp.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Твой Telegram ID: {m.from_user.id}\nВпиши его в ALLOWED_IDS, чтобы ограничить доступ семьёй.")


# ---------- обновление кода с GitHub ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_FLAG = os.path.join(BASE_DIR, ".update_notify")
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x} or ALLOWED


def is_admin(uid):
    return bool(ADMINS) and uid in ADMINS


def version_line():
    try:
        r = subprocess.run(["git", "-C", BASE_DIR, "log", "-1", "--pretty=%h · %s · %cd",
                            "--date=format:%d.%m.%Y %H:%M"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "версия неизвестна"
    except Exception:
        return "версия неизвестна"


@dp.message(Command("version"))
async def cmd_version(m: Message):
    await m.answer(f"🏷 Версия на сервере:\n{version_line()}")


# ---------- самоконтроль: бот должен быть на связи ----------
START_TS = time.time()
_last_ok = {"ts": time.time()}
HEALTH_EVERY = 120      # проверяем связь раз в 2 минуты
HEALTH_FAILS = 5        # 5 неудач подряд (~10 минут) — перезапуск


async def healthcheck():
    fails = 0
    while True:
        await asyncio.sleep(HEALTH_EVERY)
        try:
            await bot.get_me()
            _last_ok["ts"] = time.time()
            fails = 0
        except Exception as e:
            fails += 1
            logging.warning("нет связи с Telegram (%d/%d): %s", fails, HEALTH_FAILS, e)
            if fails >= HEALTH_FAILS:
                logging.error("Telegram недоступен ~%d мин — перезапускаюсь",
                              HEALTH_EVERY * HEALTH_FAILS // 60)
                os._exit(1)      # systemd поднимет заново


@dp.message(Command("status"))
async def cmd_status(m: Message):
    up = int(time.time() - START_TS)
    d, rest = divmod(up, 86400)
    h, rest = divmod(rest, 3600)
    mins = rest // 60
    upstr = (f"{d} д " if d else "") + (f"{h} ч " if h or d else "") + f"{mins} мин"
    ago = int(time.time() - _last_ok["ts"])
    w = active_plan()
    jobs = ", ".join(f"{j.id} → {j.next_run_time:%d.%m %H:%M}" for j in sched.get_jobs()) \
        if sched and sched.get_jobs() else "нет"
    await m.answer(
        "📊 Состояние бота\n\n"
        f"Работает без перерыва: {upstr}\n"
        f"Связь с Telegram: {ago} сек назад\n"
        f"Текущее меню: {w['label'] if w else 'не выбрано'}\n"
        f"Часовой пояс: {TZ} · сейчас {tznow():%d.%m %H:%M}\n"
        f"Напоминания: {jobs}\n"
        f"🏷 {version_line()}")


@dp.message(Command("update"))
async def cmd_update(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Команда доступна только владельцу бота (задай ADMIN_IDS в .env).")
    await m.answer("⏳ Скачиваю обновление и проверяю код…")
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["bash", os.path.join(BASE_DIR, "update.sh")],
            capture_output=True, text=True, timeout=600)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return await m.answer(f"❌ Не смог запустить обновление: {e}")

    tail = out[-1200:] if out else "(пусто)"
    if r.returncode != 0:
        return await m.answer(f"❌ Обновление отменено, работаю на прежней версии.\n\n{tail}")
    if "NOCHANGE" in out:
        return await m.answer(f"✅ {version_line()}\n\nОбновлений нет — на сервере уже последняя версия.")

    try:
        with open(UPDATE_FLAG, "w", encoding="utf-8") as f:
            f.write(str(m.chat.id))
    except Exception:
        pass
    await m.answer(f"✅ Код обновлён:\n{tail}\n\nПерезапускаюсь, вернусь через несколько секунд…")
    await asyncio.sleep(1)
    os._exit(0)          # systemd поднимет бота заново уже с новым кодом


@dp.message(Command("restart"))
async def cmd_restart(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer("Команда доступна только владельцу бота.")
    try:
        with open(UPDATE_FLAG, "w", encoding="utf-8") as f:
            f.write(str(m.chat.id))
    except Exception:
        pass
    await m.answer("🔄 Перезапускаюсь…")
    await asyncio.sleep(1)
    os._exit(0)


@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await safe_edit(c.message, menu_text(), menu_kb())
    await c.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ---------- settings ----------
MORNING_DEFAULT = "07:30"
EVENING_DEFAULT = "21:00"
TIME_CHOICES = ["06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]


def rem_on(key, default="1"):
    return store.get_setting(key, default) == "1"


def settings_kb():
    p = store.get_people()
    w = active_plan()
    cur = w["label"] if w else "не выбрано"
    mt = store.get_setting("morning_time", MORNING_DEFAULT)
    rows = [
        [B("➖", "p:-"), B(f"👥 {p} чел.", "noop"), B("➕", "p:+")],
        [B(f"▶️ Текущее меню: {cur}"[:60], "actsel")],
        [B(f"🌅 Утром «что сегодня»: {mt if rem_on('morning_on') else 'выкл'}", "remmorn")],
        [B(f"🌙 Вечером «что готовим завтра»: {'вкл' if rem_on('evening_on') else 'выкл'}", "remeve")],
        [B(f"🛒 Напоминать про закупы: {'вкл' if rem_on('shop_on') else 'выкл'}", "remshop")],
        [B("⌂ Меню", "menu")],
    ]
    return KB(rows)


SETTINGS_TEXT = ("⚙️ Настройки\n\n"
                 "Количество едоков пересчитывает закупки.\n"
                 "Текущее меню — то, по которому приходят утренние напоминания.\n"
                 f"Часовой пояс: {TZ}")


@dp.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery):
    await safe_edit(c.message, SETTINGS_TEXT, settings_kb())
    await c.answer()


@dp.callback_query(F.data.in_({"p:+", "p:-"}))
async def cb_people(c: CallbackQuery):
    store.set_people(store.get_people() + (1 if c.data.endswith("+") else -1))
    if (c.message.text or "").startswith("⚙️ Настройки"):
        await safe_edit(c.message, SETTINGS_TEXT, settings_kb())
    else:
        await render_shop_here(c)
    await c.answer("Готово")


@dp.callback_query(F.data == "remmorn")
async def cb_rem_morning(c: CallbackQuery):
    mt = store.get_setting("morning_time", MORNING_DEFAULT)
    rows = [[B(("✅ " if t == mt and rem_on("morning_on") else "") + t, f"mt:{t}")]
            for t in TIME_CHOICES]
    rows.append([B("🔕 Выключить утренние", "mt:off")])
    rows.append([B("‹ Настройки", "settings")])
    await safe_edit(c.message, "🌅 Во сколько присылать меню на день?\n"
                               f"Время по {TZ} (Ижевск).", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("mt:"))
async def cb_set_time(c: CallbackQuery):
    v = c.data.split(":", 1)[1]
    if v == "off":
        store.set_setting("morning_on", "0")
        await c.answer("Утренние выключены")
    else:
        store.set_setting("morning_time", v)
        store.set_setting("morning_on", "1")
        await c.answer(f"Буду присылать в {v}")
    reschedule()
    await safe_edit(c.message, SETTINGS_TEXT, settings_kb())


@dp.callback_query(F.data.in_({"remeve", "remshop"}))
async def cb_toggle_rem(c: CallbackQuery):
    key = "evening_on" if c.data == "remeve" else "shop_on"
    store.set_setting(key, "0" if rem_on(key) else "1")
    reschedule()
    await safe_edit(c.message, SETTINGS_TEXT, settings_kb())
    await c.answer("Готово")


# ---------- schedule ----------
@dp.callback_query(F.data == "schweeks")
async def cb_schweeks(c: CallbackQuery):
    rows = [[B(f"{w['label']} · {w['dates']}".strip(" ·"), f"sw:{w['id']}")] for w in store.all_weeks()]
    gen = store.all_generated()
    if gen:
        rows.append([B("— 🎲 Случайные меню", "noop")])
        rows += [[B(g["label"], f"sw:{g['id']}")] for g in gen]
    rows.append([B("⌂ Меню", "menu")])
    await safe_edit(c.message, "Выбери меню:", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("sw:"))
async def cb_sch_week(c: CallbackQuery):
    wid = c.data.split(":", 1)[1]
    w = store.get_week(wid)
    if not w:
        return await c.answer("Неделя не найдена", show_alert=True)
    rows = [[B(day["name"], f"sd:{wid}:{i}")] for i, day in enumerate(w["days"])]
    rows.append([B("‹ Недели", "schweeks"), B("⌂ Меню", "menu")])
    note = ("\n" + w["note"]) if w.get("note") else ""
    await safe_edit(c.message, f"{w['label']} · {w.get('dates','')}{note}\n\nВыбери день:", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("sd:"))
async def cb_sch_day(c: CallbackQuery):
    _, wid, di = c.data.split(":")
    w = store.get_week(wid)
    day = w["days"][int(di)]
    lines = [f"📅 {w['label']} · {day['name']}", ""]
    rec_btns, seen = [], set()
    for m in day["meals"]:
        if not m["d"]:
            continue
        lines.append(f"• {m['t']}:")
        for d in m["d"]:
            lines.append(f"   – {d}")
            rid = recipe_for(d)
            if rid and rid not in seen:
                seen.add(rid)
                r = store.get_recipe(rid)
                if r:
                    rec_btns.append([B(f"📖 {r['name']}", f"rv:{rid}")])
        lines.append("")
    rows = rec_btns + [[B("‹ Дни", f"sw:{wid}"), B("⌂ Меню", "menu")]]
    await safe_edit(c.message, "\n".join(lines).strip(), KB(rows))
    await c.answer()


# ---------- shopping ----------
STORE_DEFAULT = {
    "Крупы и лапша": "Супермаркет",
    "Хлеб и выпечка": "ВкусВилл",
    "Молочное / замена, сыр": "ВкусВилл",
    "Козье молочное, яйца": "ВкусВилл",
    "Рыба, мясо, морепродукты": "Супермаркет",
    "Рыба, мясо, яйца": "Супермаркет",
    "Овощи, фрукты, зелень": "Рынок / супермаркет",
    "Готовое, напитки, бакалея": "Супермаркет",
}
STORE_ORDER = ["ВкусВилл", "Эко-маркет", "Рынок / супермаркет", "Супермаркет", "idietum", "Ozon/WB"]


def store_of(badge, cat):
    return badge or STORE_DEFAULT.get(cat, "Супермаркет")


# режим группировки списка на пользователя: False — по категориям, True — по магазинам
_by_store = {}
# последняя открытая закупка на пользователя (для кнопок «людей»)
_last_shop = {}


@dp.callback_query(F.data == "shopsrc")
async def cb_shop_source(c: CallbackQuery):
    rows = [[B("— 📅 Из расписания", "noop")]]
    rows += [[B(f"{w['label']} · {w['dates']}".strip(" ·"), f"plan:{w['id']}")] for w in store.all_weeks()]
    gen = store.all_generated()
    if gen:
        rows.append([B("— 🎲 Случайные меню", "noop")])
        rows += [[B(g["label"], f"plan:{g['id']}")] for g in gen]
    else:
        rows.append([B("🎲 Собрать случайное меню", "gen")])
    rows.append([B("⌂ Меню", "menu")])
    await safe_edit(c.message, "Закупки. Сначала выбери меню:", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan_screen(c: CallbackQuery):
    wid = c.data.split(":", 1)[1]
    w = store.get_week(wid)
    if not w:
        return await c.answer("Меню не найдено", show_alert=True)
    shop = w.get("shop", [])
    checks = store.checked_set(wid + ":")
    stores = sorted({store_of(it[2], it[0]) for it in shop},
                    key=lambda s: STORE_ORDER.index(s) if s in STORE_ORDER else 99)
    lines = [f"📋 {w['label']} · {w.get('dates','')}".strip(" ·"), ""]
    if w.get("note"):
        lines += [w["note"], ""]
    lines += [f"Дней в меню: {len(w.get('days', []))}",
              f"Позиций в закупке: {len(shop)} · куплено {len(checks)}",
              "Магазины: " + (", ".join(stores) if stores else "—")]
    rows = [[B("🛒 Список покупок", f"shop:{wid}")],
            [B("📅 Посмотреть меню", f"sw:{wid}")]]
    if wid.startswith("g"):
        rows.append([B("🗑 Удалить это меню", f"gdel:{wid}")])
    rows.append([B("‹ Выбор меню", "shopsrc"), B("⌂ Меню", "menu")])
    await safe_edit(c.message, "\n".join(lines), KB(rows))
    await c.answer()


def shop_button(mark, name, q, st, show_store):
    label = f"{mark} {name}"
    tail = (f" · {q}" if q else "") + (f" · {st}" if show_store else "")
    room = 60 - len(tail) - 2
    if len(label) > room:
        label = label[:room].rstrip() + "…"
    return label + tail


def render_shop(wid, uid):
    w = store.get_week(wid)
    shop = w.get("shop", [])
    people = store.get_people()
    checks = store.checked_set(wid + ":")
    by_store = _by_store.get(uid, False)
    text = (f"🛒 {w['label']} · {w.get('dates','')}".strip(" ·") +
            f"\nКуплено {len(checks)} из {len(shop)} · на {people} чел." +
            ("\nГруппировка: по магазинам" if by_store else "\nГруппировка: по разделам"))
    rows = []
    for trip in (1, 2):
        items = [(i, it) for i, it in enumerate(shop) if (it[6] if len(it) > 6 else 1) == trip]
        if not items:
            continue
        rows.append([B(TRIP_HDR[trip], "noop")])
        if by_store:
            items.sort(key=lambda p: (STORE_ORDER.index(store_of(p[1][2], p[1][0]))
                                      if store_of(p[1][2], p[1][0]) in STORE_ORDER else 99, p[1][1]))
            keyf = lambda it: "🏬 " + store_of(it[2], it[0])
        else:
            keyf = lambda it: "— " + it[0]
        last = None
        for i, it in items:
            head = keyf(it)
            if head != last:
                rows.append([B(head, "noop")])
                last = head
            iid = f"{wid}:{i}"
            mark = "✅" if iid in checks else "⬜"
            q = fmtqty(it[4], it[3], people)
            st = store_of(it[2], it[0])
            rows.append([B(shop_button(mark, it[1], q, st, not by_store), f"t:{iid}")])
    rows.append([B("🏬 По магазинам" if not by_store else "📦 По разделам", f"grp:{wid}")])
    rows.append([B("➖", "p:-"), B(f"{people} чел.", "noop"), B("➕", "p:+")])
    rows.append([B("↩︎ Сбросить отметки", f"rs:{wid}")])
    rows.append([B("‹ К меню плана", f"plan:{wid}"), B("⌂ Меню", "menu")])
    return text, KB(rows)


async def render_shop_here(c: CallbackQuery):
    wid = _last_shop.get(c.from_user.id)
    if not wid:
        return
    text, kb = render_shop(wid, c.from_user.id)
    await safe_edit(c.message, text, kb)


@dp.callback_query(F.data.startswith("shop:"))
async def cb_shop(c: CallbackQuery):
    wid = c.data.split(":", 1)[1]
    if not store.get_week(wid):
        return await c.answer("Меню не найдено", show_alert=True)
    _last_shop[c.from_user.id] = wid
    text, kb = render_shop(wid, c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("grp:"))
async def cb_group(c: CallbackQuery):
    wid = c.data.split(":", 1)[1]
    _by_store[c.from_user.id] = not _by_store.get(c.from_user.id, False)
    _last_shop[c.from_user.id] = wid
    text, kb = render_shop(wid, c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("t:"))
async def cb_toggle(c: CallbackQuery):
    iid = c.data.split(":", 1)[1]
    store.toggle_check(iid)
    wid = iid.split(":")[0]
    _last_shop[c.from_user.id] = wid
    text, kb = render_shop(wid, c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer()


@dp.callback_query(F.data.startswith("rs:"))
async def cb_reset(c: CallbackQuery):
    wid = c.data.split(":", 1)[1]
    store.reset_week(wid)
    text, kb = render_shop(wid, c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer("Отметки сброшены")


# ---------- recipes ----------
@dp.callback_query(F.data == "recs")
async def cb_recs(c: CallbackQuery):
    rows = [[B(r["name"], f"rv:{r['id']}")] for r in store.all_recipes()]
    rows.append([B("➕ Добавить рецепт", "radd")])
    rows.append([B("⌂ Меню", "menu")])
    await safe_edit(c.message, "Рецепты:", KB(rows))
    await c.answer()


def recipe_text(r):
    if r.get("text"):
        return f"📖 {r['name']}\n\n{r['text']}"
    lines = [f"📖 {r['name']}", f"Выход: {r.get('out','')}", "", "Ингредиенты:"]
    lines += [f"• {x}" for x in r.get("ing", [])]
    lines += ["", "Приготовление:"]
    lines += [f"{i}. {x}" for i, x in enumerate(r.get("steps", []), 1)]
    if r.get("tips"):
        lines += ["", "Советы:"] + [f"• {x}" for x in r["tips"]]
    return "\n".join(lines)


@dp.callback_query(F.data.startswith("rv:"))
async def cb_recipe_view(c: CallbackQuery):
    rid = c.data.split(":", 1)[1]
    r = store.get_recipe(rid)
    if not r:
        return await c.answer("Рецепт не найден", show_alert=True)
    rows = [[B("✏️ Редактировать", f"re:{rid}")], [B("‹ Рецепты", "recs"), B("⌂ Меню", "menu")]]
    await safe_edit(c.message, recipe_text(r), KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("re:"))
async def cb_recipe_edit(c: CallbackQuery, state: FSMContext):
    rid = c.data.split(":", 1)[1]
    await state.update_data(rid=rid)
    await state.set_state(Edit.recipe_text)
    await c.message.answer("Пришли новый текст рецепта одним сообщением — я сохраню его как есть.")
    await c.answer()


@dp.message(Edit.recipe_text)
async def on_recipe_text(m: Message, state: FSMContext):
    data = await state.get_data()
    r = store.get_recipe(data["rid"])
    if r:
        r["text"] = m.text or ""
        store.save_recipe(r)
        await m.answer("✅ Рецепт обновлён.", reply_markup=KB([[B("Открыть", f"rv:{r['id']}")], [B("⌂ Меню", "menu")]]))
    await state.clear()


@dp.callback_query(F.data == "radd")
async def cb_recipe_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.new_name)
    await c.message.answer("Название нового рецепта?")
    await c.answer()


@dp.message(Edit.new_name)
async def on_new_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text or "Без названия")
    await state.set_state(Edit.new_text)
    await m.answer("Теперь пришли текст рецепта одним сообщением.")


@dp.message(Edit.new_text)
async def on_new_text(m: Message, state: FSMContext):
    data = await state.get_data()
    import re
    rid = "u" + re.sub(r"\W+", "", (data["name"] or "r"))[:12].lower() + str(abs(hash(data["name"])) % 1000)
    r = {"id": rid, "name": data["name"], "out": "", "ing": [], "steps": [], "tips": [], "text": m.text or ""}
    store.save_recipe(r)
    await m.answer("✅ Рецепт добавлен.", reply_markup=KB([[B("Открыть", f"rv:{rid}")], [B("⌂ Меню", "menu")]]))
    await state.clear()


# ---------- случайное меню на неделю ----------
SLOTS = ["Завтрак", "Обед", "Полдник", "Ужин", "2-й ужин"]
# блоки по два дня — как в планах диетолога: готовим один раз, едим два дня
BLOCKS = [("Понедельник", "Вторник"), ("Среда", "Четверг"),
          ("Пятница", "Суббота"), ("Воскресенье",)]
PANTRY_CATS = {"Крупы и лапша", "Готовое, напитки, бакалея"}


def meal_pool():
    """Все варианты приёмов пищи из готовых планов: слот -> список наборов блюд."""
    pool = {s: [] for s in SLOTS}
    seen = {s: set() for s in SLOTS}
    for w in store.all_weeks():
        for day in w.get("days", []):
            for m in day.get("meals", []):
                t = m.get("t")
                if t not in pool:
                    continue
                dishes = tuple(d for d in m.get("d", []) if d.strip())
                if not dishes or any("ресторан" in d.lower() for d in dishes):
                    continue          # ресторанные обеды не готовим и не закупаем
                if dishes in seen[t]:
                    continue
                seen[t].add(dishes)
                pool[t].append(dishes)
    return pool


def is_pantry(cat, name):
    if cat in PANTRY_CATS:
        return True
    if cat == "Хлеб и выпечка":
        return "Тартин" not in name   # тартин берём свежим, остальное хранится
    return False


def shop_for_days(days):
    """Список покупок для набора дней: складывает продукты и делит на два закупа."""
    table = store.dish_ingredients()
    agg, unknown = {}, []
    for di, day in enumerate(days):
        half = 1 if di < 4 else 2      # пн–чт / пт–вс
        for m in day["meals"]:
            for d in m["d"]:
                ing = table.get(d)
                if ing is None:
                    if d not in unknown:
                        unknown.append(d)
                    continue
                for cat, name, badge, unit, qty, note in ing:
                    trip = 1 if is_pantry(cat, name) else half
                    key = (trip, name, unit)
                    if key not in agg:
                        agg[key] = [cat, name, badge, unit, qty, note, trip]
                    else:
                        cur = agg[key][4]
                        agg[key][4] = None if (cur is None or qty is None) else cur + qty
    order = ["Крупы и лапша", "Хлеб и выпечка", "Молочное / замена, сыр",
             "Рыба, мясо, морепродукты", "Овощи, фрукты, зелень", "Готовое, напитки, бакалея"]
    items = sorted(agg.values(),
                   key=lambda x: (x[6], order.index(x[0]) if x[0] in order else 99, x[1]))
    return items, unknown


def generate_week():
    pool = meal_pool()
    used = {s: set() for s in SLOTS}
    days = []
    for block in BLOCKS:
        meals = []
        for s in SLOTS:
            if not pool[s]:
                continue
            fresh = [o for o in pool[s] if o not in used[s]] or pool[s]
            choice = random.choice(fresh)
            used[s].add(choice)
            meals.append({"t": s, "d": list(choice)})
        for name in block:
            days.append({"name": name, "meals": [{"t": m["t"], "d": list(m["d"])} for m in meals]})
    shop, unknown = shop_for_days(days)
    return {"id": "draft", "label": "Случайное меню",
            "dates": datetime.now().strftime("%d.%m.%Y"),
            "note": "Блюда повторяются по два дня — готовим один раз на два приёма."
                    + (f"\n⚠️ Без состава: {', '.join(unknown[:3])}" if unknown else ""),
            "days": days, "shop": shop}


_draft = {}   # черновик сгенерированного меню на пользователя


def draft_text(w):
    lines = [f"🎲 {w['label']} · {w['dates']}", w["note"], ""]
    for day in w["days"]:
        lines.append(f"📅 {day['name']}")
        for m in day["meals"]:
            lines.append(f"   {m['t']}: " + "; ".join(m["d"]))
        lines.append("")
    lines.append(f"Позиций в закупке: {len(w['shop'])}")
    return "\n".join(lines)


def draft_kb():
    return KB([[B("🔄 Другой вариант", "gen")],
               [B("💾 Сохранить меню", "gsave")],
               [B("⌂ Меню", "menu")]])


@dp.callback_query(F.data == "gen")
async def cb_generate(c: CallbackQuery):
    w = generate_week()
    _draft[c.from_user.id] = w
    txt = draft_text(w)
    if len(txt) > 3900:
        txt = txt[:3900] + "…"
    await safe_edit(c.message, txt, draft_kb())
    await c.answer("Собрал меню")


@dp.callback_query(F.data == "gsave")
async def cb_generate_save(c: CallbackQuery):
    w = _draft.get(c.from_user.id)
    if not w:
        return await c.answer("Сначала собери меню", show_alert=True)
    n = len(store.all_generated()) + 1
    w = dict(w, label=f"Случайное меню №{n}")
    gid = store.add_generated(w)
    _draft.pop(c.from_user.id, None)
    await safe_edit(c.message, f"✅ Сохранено: {w['label']} · {w['dates']}\n\n"
                               "Теперь оно есть в Расписании и в Закупках.",
                    KB([[B("🛒 Список покупок", f"plan:{gid}")],
                        [B("📅 Посмотреть меню", f"sw:{gid}")],
                        [B("⌂ Меню", "menu")]]))
    await c.answer()


@dp.callback_query(F.data.startswith("gdel:"))
async def cb_generate_delete(c: CallbackQuery):
    gid = c.data.split(":", 1)[1]
    store.delete_generated(gid)
    await c.answer("Меню удалено")
    await cb_shop_source(c)


# ---------- обновление плана: присылаем готовый plan.json ----------
@dp.message(F.document)
async def on_document(m: Message):
    fn = (m.document.file_name or "").lower()
    if not fn.endswith(".json"):
        return await m.answer("Обновить план можно файлом plan.json.",
                              reply_markup=KB([[B("⌂ Меню", "menu")]]))
    await m.answer("📥 Читаю план…")
    try:
        buf = io.BytesIO()
        await bot.download(m.document, destination=buf)
        weeks, recipes = await asyncio.to_thread(store.replace_plan, buf.getvalue())
        await m.answer(
            f"✅ План обновлён.\nНедель: {weeks} · рецептов: {recipes}\n\n"
            "Старый файл сохранён как plan.json.bak. Отметки «куплено» не тронуты.",
            reply_markup=KB([[B("📅 Расписание", "schweeks"), B("🛒 Закупки", "shopsrc")],
                             [B("⌂ Меню", "menu")]]),
        )
    except Exception as e:
        logging.exception("replace plan")
        await m.answer(f"Не получилось обновить план: {e}")


# ---------- напоминания ----------
async def remind_morning():
    w = active_plan()
    if not w:
        return
    d = tznow().date()
    txt, kb = day_card(w, d, "🌅 Доброе утро! Сегодня")
    if not txt:
        return
    for cid in store.all_chats():
        try:
            await bot.send_message(cid, txt, reply_markup=kb)
        except Exception:
            logging.exception("morning to %s", cid)


async def remind_evening():
    """Вечером — только если завтра начинается новый блок или что-то надо готовить."""
    w = active_plan()
    if not w:
        return
    today = tznow().date()
    tom = today + timedelta(days=1)
    d_today, d_tom = day_for_date(w, today), day_for_date(w, tom)
    if not d_tom:
        return
    new_block = not meals_equal(d_tom, d_today)
    if not new_block:
        return                      # завтра доедаем сегодняшнее — готовить нечего
    recs = recipes_in(d_tom)
    lines = [f"🌙 Завтра — {d_tom['name']}", "", "Начинается новый блок: блюда меняются, готовим заново."]
    if recs:
        lines.append("Готовим сами: " + ", ".join(r["name"] for r in recs))
        lines.append("Загляни в рецепт — что-то может понадобиться разморозить или замочить заранее.")
    lines.append("")
    for m in d_tom["meals"]:
        if m["d"]:
            lines.append(f"• {m['t']}: " + "; ".join(m["d"]))
    rows = [[B(f"📖 {r['name']}", f"rv:{r['id']}")] for r in recs]
    rows.append([B("🛒 Закупки", f"shop:{w['id']}"), B("⌂ Меню", "menu")])
    for cid in store.all_chats():
        try:
            await bot.send_message(cid, "\n".join(lines), reply_markup=KB(rows))
        except Exception:
            logging.exception("evening to %s", cid)


async def remind_shop(kind):
    w = active_plan()
    base = {"t1": "🛒 Пора сделать Закуп 1 (начало недели): бакалея + свежее на первую половину.",
            "t2": "🛒 Пора сделать Закуп 2 (середина недели): свежее на вторую половину. "
                  "И свежий хлеб к выходным!"}[kind]
    kb = KB([[B("⌂ Меню", "menu")]])
    if w:
        trip = 1 if kind == "t1" else 2
        shop = w.get("shop", [])
        checks = store.checked_set(w["id"] + ":")
        left = [i for i, it in enumerate(shop)
                if (it[6] if len(it) > 6 else 1) == trip and f"{w['id']}:{i}" not in checks]
        if not left:
            return                      # всё уже куплено — не тревожим
        base += f"\n\nМеню: {w['label']}\nОсталось купить: {len(left)} позиций."
        kb = KB([[B("🛒 Открыть список", f"shop:{w['id']}")], [B("⌂ Меню", "menu")]])
    for cid in store.all_chats():
        try:
            await bot.send_message(cid, base, reply_markup=kb)
        except Exception:
            logging.exception("shop reminder to %s", cid)


sched = None


def reschedule():
    """Пересобрать задания по текущим настройкам."""
    if sched is None:
        return
    for jid in ("morning", "evening", "trip1", "trip2"):
        try:
            sched.remove_job(jid)
        except Exception:
            pass
    if rem_on("morning_on"):
        hh, mm = (store.get_setting("morning_time", MORNING_DEFAULT) + ":0").split(":")[:2]
        sched.add_job(remind_morning, "cron", hour=int(hh), minute=int(mm), id="morning",
                      misfire_grace_time=3600, coalesce=True)
    if rem_on("evening_on"):
        hh, mm = EVENING_DEFAULT.split(":")
        sched.add_job(remind_evening, "cron", hour=int(hh), minute=int(mm), id="evening",
                      misfire_grace_time=3600, coalesce=True)
    if rem_on("shop_on"):
        sched.add_job(remind_shop, "cron", day_of_week="sun", hour=10, minute=0,
                      args=["t1"], id="trip1", misfire_grace_time=7200, coalesce=True)
        sched.add_job(remind_shop, "cron", day_of_week="wed", hour=18, minute=0,
                      args=["t2"], id="trip2", misfire_grace_time=7200, coalesce=True)


async def main():
    global sched
    store.init()
    if os.path.exists(UPDATE_FLAG):
        try:
            with open(UPDATE_FLAG, encoding="utf-8") as f:
                cid = int(f.read().strip())
            await bot.send_message(cid, f"✅ Бот снова на связи.\n🏷 {version_line()}",
                                   reply_markup=menu_kb())
        except Exception:
            logging.exception("update notify")
        finally:
            try:
                os.remove(UPDATE_FLAG)
            except OSError:
                pass
    sched = AsyncIOScheduler(timezone=TZ)
    sched.start()
    reschedule()
    asyncio.create_task(healthcheck())
    logging.info("Bot started, TZ=%s, jobs=%s", TZ, [j.id for j in sched.get_jobs()])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
