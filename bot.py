# -*- coding: utf-8 -*-
"""Телеграм-бот плана питания. У каждого своё: меню, закупки с галочками,
число едоков, биодобавки и напоминания. Рецепты и планы — общие,
обновляются файлом plan.json. Без внешних ИИ-сервисов."""
import os, io, re, math, time, random, asyncio, logging, subprocess
from html import escape as esc
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           BotCommand, BotCommandScopeDefault, BotCommandScopeChat, ErrorEvent,
                           WebAppInfo, MenuButtonWebApp, MenuButtonCommands)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import store

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
TZ = os.environ.get("TZ", "Europe/Samara")   # Ижевск, UTC+4
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").strip()   # адрес Mini App, если он поднят
def id_set(name):
    """Список Telegram ID из переменной окружения. Мусор пропускаем, а не падаем."""
    out = set()
    for part in os.environ.get(name, "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            logging.warning("%s: значение %r — не число, пропускаю", name, part)
    return out


ALLOWED = id_set("ALLOWED_IDS")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

RECIPE_PREFIX = [("Овощной суп на костном бульоне", "veg"), ("Уха", "uha"),
                 ("Венские вафли", "vafli"), ("Морковная запеканка", "zap")]


# ---------- helpers ----------
def B(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def KB(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)




def bar(done, total, width=10):
    """Полоса прогресса: ▰▰▰▱▱▱▱▱▱▱ 30%"""
    if not total:
        return ""
    filled = round(width * done / total)
    return "▰" * filled + "▱" * (width - filled) + f"  {round(100 * done / total)}%"


def clip(text, limit=3900):
    """Безопасно укоротить: режем по строкам, чтобы не разорвать HTML-тег."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut[:cut.rfind("\n")]
    return cut + "\n…"


def chunks(lines, limit=3900):
    """Разбить длинный список строк на сообщения по границам строк."""
    out, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ("\n" if cur else "") + ln
    if cur:
        out.append(cur)
    return out


def dish_line(d, marker="•"):
    """Блюдо: название обычным, количество — приглушённо."""
    if " — " in d:
        name, amt = d.split(" — ", 1)
        return f"{marker} {esc(name)}  <i>{esc(amt)}</i>"
    return f"{marker} {esc(d)}"


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


async def del_msg(chat_id, mid):
    """Тихо удалить сообщение (Telegram позволяет это в течение 48 часов)."""
    try:
        await bot.delete_message(chat_id, int(mid))
    except Exception:
        pass


def ui_key(chat_id):
    return f"ui:{chat_id}"


async def clear_export(chat_id):
    """Убрать присланный ранее список текстом (он мог быть из нескольких сообщений)."""
    ids = store.get_setting(f"exp:{chat_id}")
    if not ids:
        return
    store.del_setting(f"exp:{chat_id}")
    for mid in ids.split(","):
        if mid.strip():
            await del_msg(chat_id, mid.strip())


async def adopt_window(msg):
    """Сделать это сообщение единственным «окном приложения», старое убрать."""
    await clear_export(msg.chat.id)
    prev = store.get_setting(ui_key(msg.chat.id))
    if prev and int(prev) != msg.message_id:
        await del_msg(msg.chat.id, prev)
    store.set_setting(ui_key(msg.chat.id), msg.message_id)


async def show(chat_id, text, kb=None):
    """Показать экран в одном и том же окне: правим сообщение, а не плодим новые."""
    await clear_export(chat_id)
    mid = store.get_setting(ui_key(chat_id))
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=int(mid), reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        except Exception:
            pass
        await del_msg(chat_id, mid)
    m = await bot.send_message(chat_id, text, reply_markup=kb)
    store.set_setting(ui_key(chat_id), m.message_id)


async def notify(chat_id, kind, text, kb=None):
    """Уведомление (напоминание): предыдущее такого же вида убираем."""
    k = f"rem:{kind}:{chat_id}"
    prev = store.get_setting(k)
    cur_ui = store.get_setting(ui_key(chat_id))
    if prev and prev != cur_ui:
        await del_msg(chat_id, prev)
    m = await bot.send_message(chat_id, text, reply_markup=kb)
    store.set_setting(k, m.message_id)


async def safe_edit(msg: Message, text, kb):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # "message is not modified" и т.п.
    await adopt_window(msg)


# ---------- текущее меню и «что сегодня» ----------
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def tznow():
    try:
        return datetime.now(ZoneInfo(TZ))
    except Exception:
        return datetime.now()


def user_allergens(uid):
    v = store.get_user_setting(uid, "allergens", "") or ""
    return {x for x in v.split(",") if x}


def conflicts(dish, allerg):
    """Какие из выбранных аллергенов есть в блюде."""
    return sorted(set(store.dish_tags().get(dish, [])) & allerg)


def plan_order():
    """Порядок меню: недели из расписания, затем сохранённые случайные."""
    return [w["id"] for w in store.all_weeks()] + [g["id"] for g in store.all_generated()]


def next_plan_id(uid, cur):
    """Какое меню будет следующим: выбранное вручную или следующее по порядку."""
    chosen = store.get_user_setting(uid, "next_plan")
    if chosen and chosen != cur and store.get_week(chosen):
        return chosen
    ids = plan_order()
    if not ids:
        return None
    if cur in ids:
        return ids[(ids.index(cur) + 1) % len(ids)]
    return ids[0]


def week_monday(d):
    return d - timedelta(days=d.weekday())


def maybe_rollover(uid):
    """Началась новая календарная неделя — переходим на следующее меню.
    Возвращает новое меню, если переключились."""
    if store.get_user_setting(uid, "auto_next", "1") != "1":
        return None
    wid = store.get_user_setting(uid, "active_plan")
    started = store.get_user_setting(uid, "active_started")
    if not wid or not started:
        return None
    try:
        s = date.fromisoformat(started)
    except ValueError:
        return None
    today = tznow().date()
    if week_monday(today) <= week_monday(s):
        return None                      # неделя ещё не закончилась
    nxt = next_plan_id(uid, wid)
    if not nxt or nxt == wid:
        return None
    store.set_user_setting(uid, "active_plan", nxt)
    store.set_user_setting(uid, "active_started", today.isoformat())
    store.del_user_setting(uid, "next_plan")     # разовый выбор использован
    logging.info("Новая неделя у %s: перешли с %s на %s", uid, wid, nxt)
    return store.get_week(nxt)


def active_plan(uid):
    maybe_rollover(uid)
    wid = store.get_user_setting(uid, "active_plan")
    return store.get_week(wid) if wid else None


def day_for_date(w, d, uid=None):
    """Какой день плана соответствует дате. Сначала по названию дня недели,
    иначе — отсчётом от даты включения меню."""
    days = w.get("days", [])
    if not days:
        return None
    want = WEEKDAYS[d.weekday()].lower()
    for day in days:
        if day.get("name", "").strip().lower() == want:
            return day
    started = store.get_user_setting(uid, "active_started") if uid else None
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


AMOUNT_RE = re.compile(r"—\s*(\d+)(?:\s*[–-]\s*\d+)?\s*(г|мл|шт)(?![а-я])")
SKIP_COOK = re.compile(r"^(Вода|Кофе|Зелёный чай|Ромашковый чай)")


def cook_plan(w, day, people, uid, force_days=None):
    """Что и сколько готовить: порции × едоки × дни × повторы за день.
    force_days — готовим не на весь блок, а на указанное число дней."""
    days_list = w.get("days", [])
    i = next((k for k, d in enumerate(days_list) if d["name"] == day["name"]), 0)
    repeat = meals_equal(day, days_list[i - 1]) if i > 0 else False
    def span_of(dish):
        """Сколько дней подряд, начиная с сегодня, встречается это блюдо."""
        n = 0
        while i + n < len(days_list) and any(dish in m["d"] for m in days_list[i + n]["meals"]):
            n += 1
        return max(1, n)

    block = 1                       # сколько дней подряд день целиком повторяется
    while i + block < len(days_list) and meals_equal(day, days_list[i + block]):
        block += 1
    cap = max(block, 2)             # впрок готовим максимум на два дня
    multi = any(min(span_of(x), cap) > 1 for m in day["meals"] for x in m["d"])

    table = store.dish_ingredients()
    agg = {}
    for m in day["meals"]:
        for dish in m["d"]:
            if SKIP_COOK.match(dish):          # заваривается по чашке
                continue
            ing = table.get(dish) or []
            # штуками считаем, только если блюдо — это ровно один штучный продукт
            single = len(ing) == 1 and ing[0][3] == "шт"
            dry = raw = pieces = 0
            for cat, name, badge, unit, qty, note in ing:
                if qty is None:
                    continue
                if unit == "шт":
                    if single:
                        pieces += qty
                elif unit == "г":
                    if cat == "Крупы и лапша":
                        dry += qty                       # варим из сухого
                    elif "сырой вес" in (note or ""):
                        raw += qty                       # усохнет при готовке
            am = AMOUNT_RE.search(dish)
            if not am and not dry and not raw and not pieces:
                continue
            key = dish.split(" — ")[0]
            span = min(span_of(dish), force_days or cap)
            a = agg.setdefault(key, {"rid": recipe_for(dish), "am": am, "dry": 0, "raw": 0,
                                     "pieces": 0, "n": 0, "slots": [], "span": span})
            a["span"] = span
            a["n"] += 1
            a["dry"] += dry
            a["raw"] += raw
            a["pieces"] += pieces
            if m["t"] not in a["slots"]:
                a["slots"].append(m["t"])

    items = []
    for name, a in agg.items():
        days = a["span"]                       # у каждого блюда свой горизонт
        k = people * days * a["n"]
        by_piece = a["pieces"] > 0 and (not a["am"] or a["am"].group(2) != "шт")
        if by_piece:
            total, unit = math.ceil(a["pieces"] * people * days), "шт"
        elif a["am"]:
            total, unit = int(a["am"].group(1)) * k, a["am"].group(2)
        else:
            total, unit = 0, ""
        items.append({"name": name, "total": total, "unit": unit, "rid": a["rid"],
                      "slots": a["slots"], "portions": k,
                      "base": int(a["am"].group(1)) if a["am"] else 0,
                      "base_unit": a["am"].group(2) if a["am"] else "",   # единица из меню
                      "dry": round(a["dry"] * people * days / 5) * 5,
                      "raw": round(a["raw"] * people * days / 10) * 10})
    return {"days": min(block, force_days) if force_days else block,
            "block": block, "multi": multi, "repeat": repeat, "items": items}


def day_card(w, d, head, uid, cook_days=None):
    """Текст с меню на дату d + кнопки рецептов."""
    day = day_for_date(w, d, uid)
    if not day:
        return None, None
    prev = day_for_date(w, d - timedelta(days=1), uid)
    same_as_yesterday = meals_equal(day, prev)
    allerg = user_allergens(uid)
    lines = [f"<b>{esc(head)}</b>",
             f"<i>{esc(day['name'])}, {d:%d.%m} · {esc(w['label'])}</i>", ""]
    sb = supps_by_slot(uid)
    for m in day["meals"]:
        if not m["d"] and m["t"] not in sb:
            continue
        lines.append(f"<b>{esc(m['t'])}</b>")
        for x in m["d"]:
            bad = conflicts(x, allerg)
            lines.append(f"  {dish_line(x)}" + (f"  ⚠️ <i>{esc(', '.join(bad))}</i>" if bad else ""))
        lines += [f"  💊 <i>{esc(supp_text(sp))}</i>" for sp in sb.get(m["t"], [])]
        lines.append("")
    people = store.get_people(uid)
    c = cook_plan(w, day, people, uid, cook_days)
    shift = (d - tznow().date()).days
    cook_rows = []
    if same_as_yesterday and cook_days is None:
        lines.append("♻️ <i>Сегодня то же, что вчера — готовить заново не нужно.</i>")
        cook_rows = [[B("🍳 Приготовить на сегодня", f"cook:1:{shift}")]]
    else:
        if cook_days == 1:
            when = " — только на сегодня"
        elif c["block"] > 1:
            when = f" — сразу на {c['block']} дня"
        else:
            when = " — как в плане"
        head_cook = f"🍳 <b>Что приготовить</b>{when}\n<i>на {people} чел.</i>"
        lines.append(head_cook)
        for it in c["items"]:
            row = f"  • {esc(it['name'])}"
            if it["total"]:
                row += f" — <b>{it['total']} {it['unit']}</b>"
            hints = [", ".join(it["slots"]).lower()]
            if it["base"] and it["portions"] > 1:
                hints.append(f"{it['base']} {it['base_unit']} × {it['portions']}")
            if it["dry"]:
                hints.append(f"сварить {it['dry']} г сухой")
            if it["raw"]:
                hints.append(f"взять {it['raw']} г сырого")
            if it["rid"]:
                hints.append("по рецепту")
            row += f"\n     <i>{esc(' · '.join(hints))}</i>"
            lines.append(row)
        if c["multi"]:                         # что-то готовится не на один день
            other = f"На {c['block']} дня" if c["block"] > 1 else "Как в плане"
            cook_rows = [[B(("✓ " if cook_days == 1 else "") + "На сегодня", f"cook:1:{shift}"),
                          B(("✓ " if cook_days != 1 else "") + other, f"cook:0:{shift}")]]
    rows = cook_rows + [[B(f"📖 {r['name']}", f"rv:{r['id']}")] for r in recipes_in(day)]
    rows.append([B("📅 Вся неделя", f"sw:{w['id']}"), B("🛒 Закупки", f"shop:{w['id']}")])
    rows.append([B("⌂ Меню", "menu")])
    return "\n".join(lines).strip(), KB(rows)


async def send_day(chat_id, head, shift=0, uid=None):
    uid = uid or chat_id
    w = active_plan(uid)
    if not w:
        return False
    txt, kb = day_card(w, tznow().date() + timedelta(days=shift), head, uid)
    if not txt:
        return False
    await show(chat_id, txt, kb)
    return True


@dp.message(Command("today"))
async def cmd_today(m: Message):
    await del_msg(m.chat.id, m.message_id)
    if not await send_day(m.chat.id, "🍽 Сегодня", 0, m.from_user.id):
        await show(m.chat.id, "Сначала выбери текущее меню.", KB([[B("▶️ Выбрать меню", "actsel")]]))


@dp.message(Command("tomorrow"))
async def cmd_tomorrow(m: Message):
    await del_msg(m.chat.id, m.message_id)
    if not await send_day(m.chat.id, "🌙 Завтра", 1, m.from_user.id):
        await show(m.chat.id, "Сначала выбери текущее меню.", KB([[B("▶️ Выбрать меню", "actsel")]]))


@dp.callback_query(F.data.startswith("cook:"))
async def cb_cook(c: CallbackQuery):
    _, days, shift = c.data.split(":")
    uid = c.from_user.id
    w = active_plan(uid)
    if not w:
        return await c.answer("Меню не выбрано", show_alert=True)
    d = tznow().date() + timedelta(days=int(shift))
    head = "🍽 Сегодня" if shift == "0" else ("🌙 Завтра" if shift == "1" else "📅 День")
    txt, kb = day_card(w, d, head, uid, int(days) or None)
    if txt:
        await safe_edit(c.message, txt, kb)
    await c.answer()


@dp.callback_query(F.data.in_({"today", "tomorrow"}))
async def cb_today(c: CallbackQuery):
    await c.answer()
    head, shift = ("🍽 Сегодня", 0) if c.data == "today" else ("🌙 Завтра", 1)
    await adopt_window(c.message)
    if not await send_day(c.message.chat.id, head, shift, c.from_user.id):
        await show(c.message.chat.id, "Сначала выбери текущее меню.",
                   KB([[B("▶️ Выбрать меню", "actsel")]]))


# ---------- выбор текущего меню ----------
@dp.callback_query(F.data == "actsel")
async def cb_active_select(c: CallbackQuery):
    uid = c.from_user.id
    cur = store.get_user_setting(uid, "active_plan")
    auto = store.get_user_setting(uid, "auto_next", "1") == "1"
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
    rows.append([B(f"🔄 Менять автоматически: {'вкл' if auto else 'выкл'}", "autonext")])
    if cur:
        nxt = store.get_week(next_plan_id(uid, cur))
        rows.append([B(f"⏭ Следующее: {nxt['label'] if nxt else '—'}"[:60], "nxtsel")])
        rows.append([B("✖︎ Не питаться по плану", "actoff")])
    rows.append([B("‹ Настройки", "settings"), B("⌂ Меню", "menu")])
    txt = ("По какому меню сейчас питаемся?\n"
           "<i>Выбор личный — у других он не изменится.</i>\n\n")
    txt += ("🔄 В понедельник бот сам перейдёт на следующее меню."
            if auto else "🔄 Автопереключение выключено — меняешь вручную.")
    await safe_edit(c.message, txt, KB(rows))
    await c.answer()


@dp.callback_query(F.data == "autonext")
async def cb_auto_next(c: CallbackQuery):
    now = store.get_user_setting(c.from_user.id, "auto_next", "1") == "1"
    store.set_user_setting(c.from_user.id, "auto_next", "0" if now else "1")
    await c.answer("Автопереключение " + ("выключено" if now else "включено"))
    await cb_active_select(c)


@dp.callback_query(F.data == "nxtsel")
async def cb_next_select(c: CallbackQuery):
    uid = c.from_user.id
    cur = store.get_user_setting(uid, "active_plan")
    chosen = store.get_user_setting(uid, "next_plan")
    rows = [[B(("✅ " if not chosen else "") + "По порядку (следующее в списке)", "nxt:auto")]]
    for w in store.all_weeks() + store.all_generated():
        if w["id"] == cur:
            continue
        mark = "✅ " if w["id"] == chosen else ""
        rows.append([B(f"{mark}{w['label']}"[:60], f"nxt:{w['id']}")])
    rows.append([B("⏭ Перейти прямо сейчас", "nxtnow")])
    rows.append([B("‹ Назад", "actsel"), B("⌂ Меню", "menu")])
    await safe_edit(c.message,
                    "Какое меню включить, когда закончится текущая неделя?\n"
                    "Переключение произойдёт в понедельник.", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("nxt:"))
async def cb_next_set(c: CallbackQuery):
    v = c.data.split(":", 1)[1]
    if v == "auto":
        store.del_user_setting(c.from_user.id, "next_plan")
        await c.answer("Буду брать следующее по порядку")
    else:
        store.set_user_setting(c.from_user.id, "next_plan", v)
        w = store.get_week(v)
        await c.answer(f"Следующее: {w['label'] if w else v}")
    await cb_next_select(c)


@dp.callback_query(F.data == "nxtnow")
async def cb_next_now(c: CallbackQuery):
    uid = c.from_user.id
    cur = store.get_user_setting(uid, "active_plan")
    nxt = next_plan_id(uid, cur)
    w = store.get_week(nxt) if nxt else None
    if not w:
        return await c.answer("Некуда переключаться", show_alert=True)
    store.set_user_setting(uid, "active_plan", nxt)
    store.set_user_setting(uid, "active_started", tznow().date().isoformat())
    store.del_user_setting(uid, "next_plan")
    await c.answer("Переключил")
    txt, kb = day_card(w, tznow().date(), "🍽 Сегодня", uid)
    await safe_edit(c.message, f"✅ Теперь питаемся по: {w['label']}\n\n" + (txt or ""), kb)


@dp.callback_query(F.data.startswith("act:"))
async def cb_active_set(c: CallbackQuery):
    wid = c.data.split(":", 1)[1]
    w = store.get_week(wid)
    if not w:
        return await c.answer("Меню не найдено", show_alert=True)
    uid = c.from_user.id
    store.set_user_setting(uid, "active_plan", wid)
    store.set_user_setting(uid, "active_started", tznow().date().isoformat())
    reschedule()
    await c.answer("Меню выбрано")
    txt, kb = day_card(w, tznow().date(), "🍽 Сегодня", uid)
    await safe_edit(c.message, f"✅ Питаемся по: {w['label']}\n\n" + (txt or ""), kb)


@dp.callback_query(F.data == "actoff")
async def cb_active_off(c: CallbackQuery):
    store.del_user_setting(c.from_user.id, "active_plan")
    await c.answer("Выключено")
    await cb_settings(c)


# ---------- FSM ----------
class Edit(StatesGroup):
    recipe_text = State()
    new_name = State()
    new_text = State()
    extra_name = State()
    supp_name = State()


# ---------- биодобавки ----------
TIMING = {"before": "до еды", "with": "во время еды", "after": "после еды"}
TIMING_ORDER = ["before", "with", "after", ""]


def supp_text(sp):
    s = sp["name"]
    if sp["dose"]:
        s += f" — {sp['dose']}"
    t = TIMING.get(sp.get("timing") or "")
    if t:
        s += f" · {t}"
    return s


def supps_by_slot(uid):
    out = {}
    for sp in store.all_supps(uid):
        for sl in (sp["slots"] or "").split(","):
            sl = sl.strip()
            if sl:
                out.setdefault(sl, []).append(sp)
    for lst in out.values():
        lst.sort(key=lambda x: TIMING_ORDER.index(x.get("timing") or ""))
    return out


def supps_screen(uid):
    supps = store.all_supps(uid)
    rows = []
    for sp in supps:
        sl = [x for x in (sp["slots"] or "").split(",") if x]
        tag = "—" if not sl else (sl[0] if len(sl) == 1 else f"{sl[0]} +{len(sl)-1}")
        t = {"before": " ↑", "with": " •", "after": " ↓"}.get(sp.get("timing") or "", "")
        nm = sp["name"] if len(sp["name"]) <= 16 else sp["name"][:15] + "…"
        rows.append([B(f"💊 {nm} · {tag}{t}", f"supp:{sp['sid']}")])
    rows.append([B("➕ Добавить добавку", "sadd")])
    rows.append([B("‹ Настройки", "settings"), B("⌂ Меню", "menu")])
    txt = ("💊 <b>Биодобавки</b>\n" + "\n"
           "<i>Показываются в меню дня рядом с приёмом пищи, к которому привязаны.</i>")
    if not supps:
        txt += "\n\n<i>Пока пусто.</i>"
    return txt, KB(rows)


@dp.callback_query(F.data == "supps")
async def cb_supps(c: CallbackQuery):
    txt, kb = supps_screen(c.from_user.id)
    await safe_edit(c.message, txt, kb)
    await c.answer()


async def open_supp(c: CallbackQuery, sid):
    sp = store.get_supp(c.from_user.id, sid)
    if not sp:
        return await c.answer("Не найдено", show_alert=True)
    chosen = {s.strip() for s in (sp["slots"] or "").split(",") if s.strip()}
    rows = [[B(("✅ " if s in chosen else "⬜ ") + s, f"sslot:{sid}:{i}")]
            for i, s in enumerate(SLOTS)]
    cur_t = sp.get("timing") or ""
    labels = {"before": "До еды", "with": "Во время", "after": "После еды"}
    rows.append([B(("✓ " if cur_t == k else "") + labels[k], f"stime:{sid}:{k}")
                 for k in TIMING])
    rows.append([B("🗑 Удалить", f"sdel:{sid}")])
    rows.append([B("‹ Добавки", "supps"), B("⌂ Меню", "menu")])
    await safe_edit(c.message,
                    f"💊 <b>{esc(sp['name'])}</b>"
                    + (f"\n<i>{esc(sp['dose'])}</i>" if sp["dose"] else "")
                    + "\nК каким приёмам пищи привязать:"
                    + "\n<i>Ниже — как принимать: до, во время или после еды.</i>", KB(rows))


@dp.callback_query(F.data.startswith("supp:"))
async def cb_supp_view(c: CallbackQuery):
    await open_supp(c, c.data.split(":", 1)[1])
    await c.answer()


@dp.callback_query(F.data.startswith("sslot:"))
async def cb_supp_slot(c: CallbackQuery):
    _, sid, i = c.data.split(":")
    sp = store.get_supp(c.from_user.id, sid)
    if not sp:
        return await c.answer("Не найдено", show_alert=True)
    slot = SLOTS[int(i)]
    chosen = [s.strip() for s in (sp["slots"] or "").split(",") if s.strip()]
    if slot in chosen:
        chosen.remove(slot)
    else:
        chosen.append(slot)
    chosen = [s for s in SLOTS if s in chosen]      # держим порядок приёмов
    store.set_supp_slots(c.from_user.id, sid, ",".join(chosen))
    await open_supp(c, sid)
    await c.answer()


@dp.callback_query(F.data.startswith("stime:"))
async def cb_supp_timing(c: CallbackQuery):
    _, sid, key = c.data.split(":")
    sp = store.get_supp(c.from_user.id, sid)
    if not sp:
        return await c.answer("Не найдено", show_alert=True)
    new = "" if (sp.get("timing") or "") == key else key      # повторное нажатие снимает
    store.set_supp_timing(c.from_user.id, sid, new)
    await open_supp(c, sid)
    await c.answer(TIMING.get(new, "Без уточнения"))


@dp.callback_query(F.data.startswith("sdel:"))
async def cb_supp_del(c: CallbackQuery):
    store.del_supp(c.from_user.id, c.data.split(":", 1)[1])
    await c.answer("Удалено")
    txt, kb = supps_screen(c.from_user.id)
    await safe_edit(c.message, txt, kb)


@dp.callback_query(F.data == "sadd")
async def cb_supp_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.supp_name)
    await safe_edit(c.message,
                    "➕ Пришли название и дозировку через тире, например:\n"
                    "Омега-3 — 2 капсулы\n"
                    "Магний — 1 таблетка\n\n"
                    "Можно несколько сразу — каждая с новой строки.",
                    KB([[B("✖︎ Отмена", "supps")]]))
    await c.answer()


@dp.message(Edit.supp_name)
async def on_supp_name(m: Message, state: FSMContext):
    await state.clear()
    await del_msg(m.chat.id, m.message_id)
    added = 0
    for line in (m.text or "").split("\n"):
        line = line.strip(" -•\t")
        if not line:
            continue
        for sep in (" — ", " – ", " - "):
            if sep in line:
                name, dose = line.split(sep, 1)
                break
        else:
            name, dose = line, ""
        store.add_supp(m.from_user.id, name.strip()[:60], dose.strip()[:40], "Завтрак")
        added += 1
        if added >= 15:
            break
    txt, kb = supps_screen(m.from_user.id)
    await show(m.chat.id,
               f"✅ Добавил: {added}. Пока все — к завтраку.\n"
               "Нажми на добавку, чтобы выбрать другие приёмы.\n\n" + txt, kb)


# ---------- access control ----------
@dp.update.outer_middleware()
async def auth_mw(handler, event, data):
    user = data.get("event_from_user")
    if ALLOWED and (user is None or user.id not in ALLOWED):
        return
    return await handler(event, data)


# ---------- menu ----------
def menu_kb(uid=None, chat_id=None):
    rows = []
    private = chat_id is None or chat_id > 0        # у групп id отрицательный
    if WEBAPP_URL and private:
        rows.append([InlineKeyboardButton(text="📱 Открыть приложение",
                                          web_app=WebAppInfo(url=WEBAPP_URL))])
    if uid is not None and not active_plan(uid):
        rows.append([B("▶️ Выбрать меню", "actsel")])
    return KB(rows + [
        [B("🍽 Что сегодня", "today"), B("📅 Расписание", "schweeks")],
        [B("🛒 Закупки", "shopsrc"), B("📖 Рецепты", "recs")],
        [B("⚙️ Настройки", "settings")],
    ])


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def menu_text(uid):
    d = tznow()
    head = (f"🍲 <b>{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}</b>\n")
    w = active_plan(uid)
    if not w:
        return head + "\n▶️ <i>Меню пока не выбрано</i>"

    lines = [f"▶️ {esc(w['label'])}"]

    # готовим сегодня или доедаем вчерашнее
    day = day_for_date(w, d.date(), uid)
    if day:
        prev = day_for_date(w, d.date() - timedelta(days=1), uid)
        nxt = day_for_date(w, d.date() + timedelta(days=1), uid)
        if meals_equal(day, prev):
            lines.append("♻️ Сегодня то же, что вчера — готовить не нужно")
        elif meals_equal(day, nxt):
            lines.append("🍳 Готовим сегодня — сразу на два дня")
        else:
            lines.append("🍳 Готовим сегодня")

    # добавки: к какому приёму что принять
    sb = supps_by_slot(uid)
    shown = 0
    for slot in SLOTS:
        if slot not in sb:
            continue
        if shown == 3:                       # не раздуваем экран
            lines.append("💊 …")
            break
        names = ", ".join(sp["name"] for sp in sb[slot])
        lines.append(f"💊 {esc(slot)}: {esc(names)}"[:70])
        shown += 1

    # ближайший незакрытый заход
    _, items = shop_items(uid, w["id"])
    checks = store.checked_set(uid, w["id"] + ":")
    for trip in (1, 2):
        sel = [it for it in items if in_view(it, trip)]
        left = [it for it in sel if it["iid"] not in checks]
        if sel and left:
            name = TRIP_SHORT[trip].split(" ", 1)[1]      # без цветного кружка
            lines.append(f"🛒 {name}: осталось {len(left)} из {len(sel)}")
            break
    else:
        if items:
            lines.append("🛒 Всё куплено")

    # что включится со следующей недели
    if store.get_user_setting(uid, "auto_next", "1") == "1":
        nxt_w = store.get_week(next_plan_id(uid, w["id"]))
        if nxt_w:
            lines.append(f"⏭ С понедельника — {esc(nxt_w['label'])}")

    return head + "\n" + "\n".join(lines)


BOT_USERNAME = ""          # заполняется при запуске


async def group_hint(m: Message):
    """В группе бот не работает: у каждого своё меню и свои отметки."""
    kb = None
    if BOT_USERNAME:
        kb = KB([[InlineKeyboardButton(text="Открыть бота",
                                       url=f"https://t.me/{BOT_USERNAME}")]])
    await m.answer("Здесь я не работаю — у каждого своё меню, свои закупки и напоминания.\n"
                   "Напиши мне в личку.", reply_markup=kb)


@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    if m.chat.id < 0:
        return await group_hint(m)
    uid = m.from_user.id
    known = m.chat.id in store.all_chats()
    store.add_chat(m.chat.id)
    await del_msg(m.chat.id, m.message_id)
    if not known:
        reschedule()                       # у нового человека — свои напоминания
    await show(m.chat.id, menu_text(uid), menu_kb(uid, m.chat.id))


@dp.message(Command("menu"))
async def menu_cmd(m: Message, state: FSMContext):
    await state.clear()
    if m.chat.id < 0:
        return await group_hint(m)
    await del_msg(m.chat.id, m.message_id)
    await show(m.chat.id, menu_text(m.from_user.id), menu_kb(m.from_user.id, m.chat.id))


@dp.message(Command("myid"))
async def myid(m: Message):
    await del_msg(m.chat.id, m.message_id)
    await show(m.chat.id, f"Твой Telegram ID: {m.from_user.id}\n\n"
               "Впиши его в ALLOWED_IDS (доступ к боту) и ADMIN_IDS (обновления).",
               KB([[B("⌂ Меню", "menu")]]))


# ---------- обновление кода с GitHub ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPDATE_FLAG = os.path.join(BASE_DIR, ".update_notify")
ADMINS = id_set("ADMIN_IDS") or ALLOWED


def is_admin(uid):
    return bool(ADMINS) and uid in ADMINS


async def admin_only(m: Message):
    """Для не-админа команды как будто не существует: молча убираем сообщение."""
    if is_admin(m.from_user.id):
        return True
    await del_msg(m.chat.id, m.message_id)
    return False


async def notify_admin(text):
    """Техническое сообщение — только владельцу."""
    for uid in ADMINS:
        try:
            await bot.send_message(uid, text[:3500])
        except Exception:
            logging.warning("не смог написать админу %s", uid)


PUBLIC_CMDS = [
    BotCommand(command="start", description="Открыть меню"),
    BotCommand(command="today", description="Что сегодня едим"),
    BotCommand(command="tomorrow", description="Что завтра"),
    BotCommand(command="myid", description="Мой Telegram ID"),
]
ADMIN_CMDS = PUBLIC_CMDS + [
    BotCommand(command="status", description="Состояние бота"),
    BotCommand(command="version", description="Версия кода"),
    BotCommand(command="update", description="Обновить с GitHub"),
    BotCommand(command="restart", description="Перезапустить бота"),
]


async def setup_commands():
    """Обычные видят короткий список команд, владелец — полный."""
    global BOT_USERNAME
    try:
        BOT_USERNAME = (await bot.me()).username or ""
    except Exception:
        logging.warning("не смог узнать имя бота")
    try:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=WEBAPP_URL))
            if WEBAPP_URL else MenuButtonCommands())
    except Exception:
        logging.warning("не смог задать кнопку меню")
    try:
        await bot.set_my_commands(PUBLIC_CMDS, scope=BotCommandScopeDefault())
    except Exception:
        logging.warning("не смог задать список команд")
    for uid in ADMINS:
        try:
            await bot.set_my_commands(ADMIN_CMDS, scope=BotCommandScopeChat(chat_id=uid))
        except Exception:
            logging.warning("не смог задать команды владельца %s", uid)


@dp.errors()
async def on_error(event: ErrorEvent):
    """Любая ошибка: пользователю — нейтрально, владельцу — подробности."""
    logging.exception("ошибка обработчика", exc_info=event.exception)
    upd = event.update
    chat_id = None
    if getattr(upd, "message", None):
        chat_id = upd.message.chat.id
    elif getattr(upd, "callback_query", None) and upd.callback_query.message:
        chat_id = upd.callback_query.message.chat.id
    if chat_id and not is_admin(chat_id):
        try:
            await bot.send_message(chat_id, "Что-то пошло не так. Я уже сообщил владельцу — скоро починим.")
        except Exception:
            pass
    await notify_admin(f"⚠️ Ошибка в боте\n{type(event.exception).__name__}: {event.exception}")
    return True


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
    if not await admin_only(m):
        return
    await show(m.chat.id, f"🏷 Версия на сервере:\n{version_line()}", KB([[B("⌂ Меню", "menu")]]))


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
    await del_msg(m.chat.id, m.message_id)
    await show(m.chat.id,
        "📊 <b>Состояние бота</b>\n" + "\n"
        f"Работает без перерыва: {upstr}\n"
        f"Связь с Telegram: {ago} сек назад\n"
        f"Текущее меню: {w['label'] if w else 'не выбрано'}\n"
        f"Часовой пояс: {TZ} · сейчас {tznow():%d.%m %H:%M}\n"
        f"Напоминания: {jobs}\n"
        f"🏷 {version_line()}", KB([[B("⌂ Меню", "menu")]]))


@dp.message(Command("update"))
async def cmd_update(m: Message):
    if not await admin_only(m):
        return
    await del_msg(m.chat.id, m.message_id)
    await show(m.chat.id, "⏳ Скачиваю обновление и проверяю код…")
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["bash", os.path.join(BASE_DIR, "update.sh")],
            capture_output=True, text=True, timeout=600)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return await show(m.chat.id, f"❌ Не смог запустить обновление: {e}", KB([[B("⌂ Меню", "menu")]]))

    tail = out[-1200:] if out else "(пусто)"
    if r.returncode != 0:
        return await show(m.chat.id, f"❌ Обновление отменено, работаю на прежней версии.\n\n{tail}",
                          KB([[B("⌂ Меню", "menu")]]))
    if "NOCHANGE" in out:
        return await show(m.chat.id, f"✅ {version_line()}\n\nОбновлений нет — на сервере уже последняя версия.",
                          KB([[B("⌂ Меню", "menu")]]))

    try:
        with open(UPDATE_FLAG, "w", encoding="utf-8") as f:
            f.write(str(m.chat.id))
    except Exception:
        pass
    await show(m.chat.id, f"✅ Код обновлён:\n{tail}\n\nПерезапускаюсь, вернусь через несколько секунд…")
    await asyncio.sleep(1)
    os._exit(0)          # systemd поднимет бота заново уже с новым кодом


@dp.message(Command("restart"))
async def cmd_restart(m: Message):
    if not await admin_only(m):
        return
    await del_msg(m.chat.id, m.message_id)
    try:
        with open(UPDATE_FLAG, "w", encoding="utf-8") as f:
            f.write(str(m.chat.id))
    except Exception:
        pass
    await show(m.chat.id, "🔄 Перезапускаюсь…")
    await asyncio.sleep(1)
    os._exit(0)


@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await safe_edit(c.message, menu_text(c.from_user.id), menu_kb(c.from_user.id, c.message.chat.id))
    await c.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ---------- settings ----------
MEAL_DEFAULTS = {"Завтрак": "08:00", "Обед": "13:00", "Полдник": "16:00",
                 "Ужин": "19:00", "2-й ужин": "21:30"}


def meal_time(uid, slot):
    return store.get_user_setting(uid, "mt:" + slot, MEAL_DEFAULTS[slot])


def meal_on(uid, slot):
    """Нужно ли напоминать про этот приём — у каждого свой набор."""
    return store.get_user_setting(uid, "mon:" + slot, "1") == "1"


def shift_time(hhmm, minutes):
    """Сдвинуть время на N минут в пределах суток."""
    try:
        h, m = [int(x) for x in hhmm.split(":")]
    except Exception:
        h, m = 8, 0
    total = max(0, min(23 * 60 + 45, h * 60 + m + minutes))
    return f"{total // 60:02d}:{total % 60:02d}"


MORNING_DEFAULT = "07:30"
EVENING_DEFAULT = "21:00"
TIME_CHOICES = ["06:30", "07:00", "07:30", "08:00", "08:30", "09:00"]


def rem_on(uid, key, default="1"):
    return store.get_user_setting(uid, key, default) == "1"


def settings_kb(uid):
    p = store.get_people(uid)
    w = active_plan(uid)
    cur = w["label"] if w else "не выбрано"
    mt = store.get_user_setting(uid, "morning_time", MORNING_DEFAULT)
    rows = [
        [B("➖", "p:-"), B(f"👥 {p} чел.", "noop"), B("➕", "p:+")],
        [B(f"▶️ Текущее меню: {cur}"[:60], "actsel")],
        [B(("⏭ Дальше: " + (store.get_week(next_plan_id(uid, w["id"])) or {}).get("label", "—")
            if w and store.get_user_setting(uid, "auto_next", "1") == "1"
            else "⏭ Автопереход выключен")[:60], "nxtsel")],
        [B(f"💊 Добавки: {len(store.all_supps(uid))}", "supps")],
        [B("🍽 Напоминания о еде: " + (f"{sum(1 for s in SLOTS if meal_on(uid, s))} из {len(SLOTS)}"
           if rem_on(uid, "meals_on", "0") else "выкл"), "meals")],
        [B(f"🌅 Утром меню дня: {mt if rem_on(uid, 'morning_on') else 'выкл'}", "remmorn")],
        [B(f"🌙 Вечером о готовке: {'вкл' if rem_on(uid, 'evening_on') else 'выкл'}", "remeve")],
        [B(f"🛒 Напоминать о закупах: {'вкл' if rem_on(uid, 'shop_on') else 'выкл'}", "remshop")],
        [B("❓ Как это работает", "help")],
        [B("⌂ Меню", "menu")],
    ]
    return KB(rows)


# ---------- справка ----------
HELP_TOPICS = {
    "today": ("🍽 Что сегодня", [
        "<b>Меню на день</b>", "",
        "Показывает, что есть сегодня по выбранному плану, и главное — <b>надо ли готовить</b>.", "",
        "Блюда в планах идут парами по два дня: приготовил в понедельник — во вторник ешь то же "
        "самое. Бот сам это отслеживает и пишет «Готовим на два дня» или «Готовить не нужно».", "",
        "У блюд, которые готовятся дома, есть кнопка с рецептом.", "",
        "<i>Меню выбирается в Настройках → Текущее меню. С понедельника бот сам перейдёт "
        "на следующую неделю.</i>",
    ]),
    "shop": ("🛒 Закупки", [
        "<b>Список покупок</b>", "",
        "Сначала выбираешь меню, потом — какой заход закупаешь:", "",
        "<b>Закуп 1</b> — в начале недели. Бакалея на всю неделю и свежее на первую половину.",
        "<b>Закуп 2</b> — в середине. Свежее на вторую половину. Свежий хлеб сюда же, "
        "чтобы не черствел.",
        "<b>Заказать заранее</b> — рыба и мясо у поставщика, доставки с Ozon. "
        "Это не купить по дороге.", "",
        "В списке: галочки, количество на твоё число едоков и магазин у каждой позиции.", "",
        "<b>🏬 Магазины</b> — перестроить список по торговым точкам.",
        "<b>🙈 Скрыть ✅</b> — список тает по мере покупок.",
        "<b>➕ Добавить</b> — дописать своё: корм коту, бытовая химия.",
        "<b>📋 Списком</b> — прислать текстом, чтобы переслать или открыть в магазине.", "",
        "<i>Вес в меню — готового блюда, в закупке — сырого продукта. При готовке уходит "
        "до трети веса, это уже учтено.</i>",
    ]),
    "gen": ("🎲 Своё меню", [
        "<b>Случайное меню на неделю</b>", "",
        "Бот соберёт неделю из блюд, которые уже есть в твоих планах — ничего нового "
        "и незнакомого.", "",
        "Блюда так же идут парами по два дня, чтобы готовить один раз на два приёма.", "",
        "Не понравился вариант — «Другой вариант». Понравился — «Сохранить», и меню появится "
        "в Расписании и Закупках наравне с остальными.", "",
        "<i>Кнопка «Собрать своё меню» — внизу раздела 📅 Расписание.</i>",
    ]),
    "supp": ("💊 Добавки", [
        "<b>Витамины и добавки</b>", "",
        "Добавляешь список один раз и привязываешь каждую к приёмам пищи — завтрак, ужин, любые.", "",
        "Дальше они сами появляются в меню дня рядом с нужным приёмом и в утреннем "
        "напоминании. Забыть сложнее.", "",
        "Вводить можно пачкой, каждую с новой строки:",
        "<i>Омега-3 — 2 капсулы</i>",
        "<i>Магний — 1 таблетка</i>",
    ]),
    "rem": ("🔔 Напоминания", [
        "<b>Что и когда приходит</b>", "",
        "<b>7:30</b> — меню на день. Время можно изменить или выключить.",
        "<b>21:00</b> — что готовим завтра. Приходит, только если завтра новые блюда; "
        "если доедаешь вчерашнее — бот молчит.",
        "<b>Воскресенье 10:00 и среда 18:00</b> — про закупы. Если всё уже отмечено, "
        "напоминание не придёт.", "",
        f"<i>Время по Ижевску. Всё настраивается в ⚙️ Настройках.</i>",
    ]),
}


def help_kb():
    keys = list(HELP_TOPICS)
    rows = [[B(HELP_TOPICS[k][0], f"help:{k}") for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([B("‹ Настройки", "settings"), B("⌂ Меню", "menu")])
    return KB(rows)


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await safe_edit(c.message,
                    "❓ <b>Как это работает</b>\n\n"
                    "Бот ведёт твой план питания: показывает, что есть сегодня, "
                    "и собирает список покупок.", help_kb())
    await c.answer()


@dp.callback_query(F.data.startswith("help:"))
async def cb_help_topic(c: CallbackQuery):
    key = c.data.split(":", 1)[1]
    topic = HELP_TOPICS.get(key)
    if not topic:
        return await c.answer("Раздел не найден", show_alert=True)
    await safe_edit(c.message, "\n".join(topic[1]),
                    KB([[B("‹ Все разделы", "help")], [B("⌂ Меню", "menu")]]))
    await c.answer()


SETTINGS_TEXT = ("⚙️ <b>Настройки</b>\n\n"
                 "<i>Все настройки личные: меню, закупки, добавки и напоминания "
                 "у каждого свои.</i>\n\n"
                 f"🕒 Часовой пояс: <code>{TZ}</code>")


@dp.callback_query(F.data == "settings")
async def cb_settings(c: CallbackQuery):
    await safe_edit(c.message, SETTINGS_TEXT, settings_kb(c.from_user.id))
    await c.answer()


@dp.callback_query(F.data.in_({"p:+", "p:-"}))
async def cb_people(c: CallbackQuery):
    uid = c.from_user.id
    store.set_people(uid, store.get_people(uid) + (1 if c.data.endswith("+") else -1))
    if (c.message.text or "").startswith("⚙️ Настройки"):
        await safe_edit(c.message, SETTINGS_TEXT, settings_kb(c.from_user.id))
    else:
        await render_shop_here(c)
    await c.answer("Готово")


@dp.callback_query(F.data == "gen")
async def cb_gen_setup(c: CallbackQuery):
    """Перед сборкой меню — что исключить."""
    uid = c.from_user.id
    cur = user_allergens(uid)
    lst = store.allergen_list()
    rows = [[B(("✅ " if a in cur else "⬜ ") + a, f"alrgt:{i}")] for i, a in enumerate(lst)]
    rows.append([B("🎲 Собрать меню", "genrun")])
    rows.append([B("‹ Расписание", "schweeks"), B("⌂ Меню", "menu")])
    n = sum(1 for d in store.dish_tags().values() if not (set(d) & cur))
    await safe_edit(c.message,
                    "🎲 <b>Своё меню</b>\n\n"
                    "<i>Отметь, чего в меню быть не должно. Блюда с этим бот не возьмёт "
                    "и будет помечать ⚠️ в готовых неделях.</i>\n\n"
                    f"Подходит блюд: <b>{n}</b> из {len(store.dish_tags())}", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("alrgt:"))
async def cb_allergen_toggle(c: CallbackQuery):
    uid = c.from_user.id
    lst = store.allergen_list()
    a = lst[int(c.data.split(":", 1)[1])]
    cur = user_allergens(uid)
    cur.symmetric_difference_update({a})
    store.set_user_setting(uid, "allergens", ",".join(sorted(cur)))
    await cb_gen_setup(c)


@dp.callback_query(F.data == "meals")
async def cb_meals(c: CallbackQuery):
    uid = c.from_user.id
    on = rem_on(uid, "meals_on", "0")
    rows = [[B(("🔔 Напоминания включены" if on else "🔕 Напоминания выключены"), "mealson")]]
    if on:
        for i, slot in enumerate(SLOTS):
            mark = "✅" if meal_on(uid, slot) else "⬜"
            rows.append([B(f"{mark} {slot} · {meal_time(uid, slot)}", f"mtog:{i}"),
                         B("−15", f"mtm:{i}"), B("+15", f"mtp:{i}")])
    rows.append([B("‹ Настройки", "settings"), B("⌂ Меню", "menu")])
    await safe_edit(c.message,
                    "🍽 <b>Напоминания о приёмах пищи</b>\n\n"
                    "<i>Нажми на название, чтобы включить или выключить приём. "
                    "Время сдвигается кнопками по 15 минут.</i>", KB(rows))
    await c.answer()


@dp.callback_query(F.data == "mealson")
async def cb_meals_toggle(c: CallbackQuery):
    uid = c.from_user.id
    store.set_user_setting(uid, "meals_on", "0" if rem_on(uid, "meals_on", "0") else "1")
    reschedule()
    await cb_meals(c)


@dp.callback_query(F.data.startswith("mtog:"))
async def cb_meal_toggle(c: CallbackQuery):
    uid = c.from_user.id
    slot = SLOTS[int(c.data.split(":", 1)[1])]
    store.set_user_setting(uid, "mon:" + slot, "0" if meal_on(uid, slot) else "1")
    reschedule()
    await cb_meals(c)


@dp.callback_query(F.data.startswith(("mtm:", "mtp:")))
async def cb_meal_time(c: CallbackQuery):
    uid = c.from_user.id
    kind, i = c.data.split(":")
    slot = SLOTS[int(i)]
    store.set_user_setting(uid, "mt:" + slot,
                           shift_time(meal_time(uid, slot), -15 if kind == "mtm" else 15))
    reschedule()
    await cb_meals(c)


@dp.callback_query(F.data == "remmorn")
async def cb_rem_morning(c: CallbackQuery):
    uid = c.from_user.id
    mt = store.get_user_setting(uid, "morning_time", MORNING_DEFAULT)
    rows = [[B(("✅ " if t == mt and rem_on(uid, "morning_on") else "") + t, f"mt:{t}")]
            for t in TIME_CHOICES]
    rows.append([B("🔕 Выключить утренние", "mt:off")])
    rows.append([B("‹ Настройки", "settings")])
    await safe_edit(c.message, "🌅 Во сколько присылать меню на день?\n"
                               f"Время по {TZ} (Ижевск).", KB(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("mt:"))
async def cb_set_time(c: CallbackQuery):
    v = c.data.split(":", 1)[1]
    uid = c.from_user.id
    if v == "off":
        store.set_user_setting(uid, "morning_on", "0")
        await c.answer("Утренние выключены")
    else:
        store.set_user_setting(uid, "morning_time", v)
        store.set_user_setting(uid, "morning_on", "1")
        await c.answer(f"Буду присылать в {v}")
    reschedule()
    await safe_edit(c.message, SETTINGS_TEXT, settings_kb(c.from_user.id))


@dp.callback_query(F.data.in_({"remeve", "remshop"}))
async def cb_toggle_rem(c: CallbackQuery):
    uid = c.from_user.id
    key = "evening_on" if c.data == "remeve" else "shop_on"
    store.set_user_setting(uid, key, "0" if rem_on(uid, key) else "1")
    reschedule()
    await safe_edit(c.message, SETTINGS_TEXT, settings_kb(c.from_user.id))
    await c.answer("Готово")


# ---------- schedule ----------
@dp.callback_query(F.data == "schweeks")
async def cb_schweeks(c: CallbackQuery):
    rows = [[B(f"{w['label']} · {w['dates']}".strip(" ·"), f"sw:{w['id']}")] for w in store.all_weeks()]
    gen = store.all_generated()
    if gen:
        rows.append([B("— 🎲 Свои меню", "noop")])
        rows += [[B(g["label"], f"sw:{g['id']}")] for g in gen]
    rows.append([B("🎲 Собрать своё меню", "gen")])
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
    lines = [f"📅 <b>{esc(day['name'])}</b>", f"<i>{esc(w['label'])}</i>", ""]
    rec_btns, seen = [], set()
    sb = supps_by_slot(c.from_user.id)
    for m in day["meals"]:
        if not m["d"] and m["t"] not in sb:
            continue
        lines.append(f"<b>{esc(m['t'])}</b>")
        for d in m["d"]:
            lines.append(f"  {dish_line(d)}")
            rid = recipe_for(d)
            if rid and rid not in seen:
                seen.add(rid)
                r = store.get_recipe(rid)
                if r:
                    rec_btns.append([B(f"📖 {r['name']}", f"rv:{rid}")])
        lines += [f"  💊 <i>{esc(supp_text(sp))}</i>" for sp in sb.get(m["t"], [])]
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
STORE_ORDER = ["Поставщик", "ВкусВилл", "Эко-маркет", "Рынок / супермаркет",
               "Супермаркет", "idietum", "Ozon/WB"]


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
    checks = store.checked_set(c.from_user.id, wid + ":")
    stores = sorted({store_of(it[2], it[0]) for it in shop},
                    key=lambda s: STORE_ORDER.index(s) if s in STORE_ORDER else 99)
    lines = [f"📋 <b>{esc(w['label'])}</b>", f"<i>{esc(w.get('dates',''))}</i>".strip(), ""]
    if w.get("note"):
        lines += [f"<i>{esc(w['note'])}</i>", ""]
    lines += [f"Дней в меню: <b>{len(w.get('days', []))}</b>",
              f"Позиций в закупке: <b>{len(shop)}</b>",
              bar(len(checks), len(shop)) if shop else "",
              "", "🏬 " + esc(", ".join(stores) if stores else "—")]
    rows = [[B("🛒 Закупки", f"shop:{wid}")],
            [B("📅 Посмотреть меню", f"sw:{wid}")]]
    if wid.startswith("g"):
        rows.append([B("🗑 Удалить это меню", f"gdel:{wid}")])
    rows.append([B("‹ Выбор меню", "shopsrc"), B("⌂ Меню", "menu")])
    await safe_edit(c.message, "\n".join(lines), KB(rows))
    await c.answer()


PREORDER = {"Поставщик", "Ozon/WB", "idietum"}   # это заказываем заранее, а не берём в магазине
TRIP_NAME = {1: "🟢 Закуп 1 — начало недели", 2: "🟣 Закуп 2 — середина недели",
             "p": "📦 Заказать заранее"}
TRIP_SHORT = {1: "🟢 Закуп 1", 2: "🟣 Закуп 2", "p": "📦 Заранее"}

# режим отображения на пользователя
_by_store = {}      # группировать по магазинам
_hide_done = {}     # скрывать купленное
_last_view = {}     # (wid, trip) — куда возвращаться после нажатий


def shop_items(uid, wid):
    """Все позиции меню + свои пункты, в одном виде."""
    w = store.get_week(wid)
    if not w:
        return None, []
    items = []
    for i, it in enumerate(w.get("shop", [])):
        items.append({"iid": f"{wid}:{i}", "cat": it[0], "name": it[1], "badge": it[2],
                      "unit": it[3], "qty": it[4], "note": it[5],
                      "trip": it[6] if len(it) > 6 else 1, "eid": None})
    for e in store.all_extras(uid, wid):
        items.append({"iid": f"{wid}:x{e['eid']}", "cat": "Своё", "name": e["name"], "badge": "",
                      "unit": "", "qty": None, "note": "", "trip": e["trip"], "eid": e["eid"]})
    return w, items


def in_view(it, trip):
    if trip == "p":
        return store_of(it["badge"], it["cat"]) in PREORDER
    return it["trip"] == trip


BTN_WIDTH = 32          # столько символов реально помещается в кнопку на телефоне


def shop_button(mark, name, q):
    tail = f" · {q}" if q else ""
    room = BTN_WIDTH - len(tail) - 2
    if len(name) > room:
        cut = name[:room - 1]
        if " " in cut[room // 2:]:          # режем по слову, если получается
            cut = cut[:cut.rfind(" ")]
        name = cut.rstrip(" ,(") + "…"
    return f"{mark} {name}{tail}"


# ---------- экран выбора захода ----------
async def open_shop(c: CallbackQuery, wid):
    uid = c.from_user.id
    w, items = shop_items(uid, wid)
    if not w:
        return await c.answer("Меню не найдено", show_alert=True)
    checks = store.checked_set(uid, wid + ":")
    people = store.get_people(uid)
    rows = []
    for trip in (1, 2, "p"):
        sel = [it for it in items if in_view(it, trip)]
        if not sel:
            continue
        left = sum(1 for it in sel if it["iid"] not in checks)
        mark = "✅ " if left == 0 else ""
        rows.append([B(f"{mark}{TRIP_SHORT[trip]} · {left} из {len(sel)}", f"st:{wid}:{trip}")])
    rows.append([B("📋 Весь список текстом", f"txt:{wid}:all")])
    rows.append([B("➖", "p:-"), B(f"👥 {people} чел.", "noop"), B("➕", "p:+")])
    rows.append([B("↩︎ Сбросить всё", f"rs:{wid}:all")])
    rows.append([B("‹ Назад", f"plan:{wid}"), B("⌂ Меню", "menu")])
    done = sum(1 for it in items if it["iid"] in checks)
    txt = (f"🛒 <b>Закупки</b>\n<i>{esc(w['label'])} · на {people} чел.</i>\n"
           f"{bar(done, len(items))}\nКуплено <b>{done}</b> из {len(items)}\n\n"
           "Выбери, что закупаешь сейчас:")
    _last_view[c.from_user.id] = (wid, None)
    await safe_edit(c.message, txt, KB(rows))


@dp.callback_query(F.data.startswith("shop:"))
async def cb_shop(c: CallbackQuery):
    await open_shop(c, c.data.split(":", 1)[1])
    await c.answer()


# ---------- список одного захода ----------
def render_trip(wid, trip, uid):
    w, items = shop_items(uid, wid)
    people = store.get_people(uid)
    checks = store.checked_set(uid, wid + ":")
    by_store = _by_store.get(uid, False)
    hide = _hide_done.get(uid, False)
    sel = [it for it in items if in_view(it, trip)]
    left = [it for it in sel if it["iid"] not in checks]
    shown = left if hide else sel

    text = (f"<b>{esc(TRIP_NAME[trip])}</b>\n<i>{esc(w['label'])} · на {people} чел.</i>\n"
            f"{bar(len(sel) - len(left), len(sel))}\n"
            f"Осталось <b>{len(left)}</b> из {len(sel)}")
    if trip == "p":
        text += "\n\n📦 <i>Это не берётся в магазине: рыба и мясо — у поставщика, остальное с доставкой. Закажи заранее.</i>"
    if not left:
        text += "\n\n🎉 <b>Всё куплено!</b>"

    if by_store:
        shown = sorted(shown, key=lambda it: (
            STORE_ORDER.index(store_of(it["badge"], it["cat"]))
            if store_of(it["badge"], it["cat"]) in STORE_ORDER else 99, it["name"]))
        keyf = lambda it: "🏬 " + store_of(it["badge"], it["cat"])
    else:
        keyf = lambda it: "— " + it["cat"]

    rows, last = [], None
    for it in shown:
        h = keyf(it)
        if h != last:
            rows.append([B(h, "noop")])
            last = h
        mark = "✅" if it["iid"] in checks else "⬜"
        q = fmtqty(it["qty"], it["unit"], people)
        rows.append([B(shop_button(mark, it["name"], q), f"t:{it['iid']}")])

    rows.append([B("🏬 Магазины" if not by_store else "📦 Разделы", f"grp:{wid}"),
                 B("🙈 Скрыть ✅" if not hide else "👁 Показать всё", f"hide:{wid}")])
    rows.append([B("➕ Добавить", f"add:{wid}:{trip}"),
                 B("📋 Списком", f"txt:{wid}:{trip}")])
    if any(it["eid"] for it in sel):
        rows.append([B("🗑 Мои пункты", f"xlist:{wid}:{trip}")])
    rows.append([B("➖", "p:-"), B(f"👥 {people} чел.", "noop"), B("➕", "p:+")])
    rows.append([B("↩︎ Сбросить заход", f"rs:{wid}:{trip}")])
    rows.append([B("‹ Заходы", f"shop:{wid}"), B("⌂ Меню", "menu")])
    return text, KB(rows)


def parse_trip(v):
    return "p" if v == "p" else int(v)


@dp.callback_query(F.data.startswith("st:"))
async def cb_trip(c: CallbackQuery):
    _, wid, t = c.data.split(":")
    trip = parse_trip(t)
    _last_view[c.from_user.id] = (wid, trip)
    text, kb = render_trip(wid, trip, c.from_user.id)
    await safe_edit(c.message, text, kb)
    await c.answer()


async def refresh_view(c: CallbackQuery):
    wid, trip = _last_view.get(c.from_user.id, (None, None))
    if not wid:
        return
    if trip is None:
        return await open_shop(c, wid)
    text, kb = render_trip(wid, trip, c.from_user.id)
    await safe_edit(c.message, text, kb)


async def render_shop_here(c: CallbackQuery):
    await refresh_view(c)


@dp.callback_query(F.data.startswith("t:"))
async def cb_toggle(c: CallbackQuery):
    store.toggle_check(c.from_user.id, c.data.split(":", 1)[1])
    await refresh_view(c)
    await c.answer()


@dp.callback_query(F.data.startswith("grp:"))
async def cb_group(c: CallbackQuery):
    _by_store[c.from_user.id] = not _by_store.get(c.from_user.id, False)
    await refresh_view(c)
    await c.answer()


@dp.callback_query(F.data.startswith("hide:"))
async def cb_hide(c: CallbackQuery):
    _hide_done[c.from_user.id] = not _hide_done.get(c.from_user.id, False)
    await refresh_view(c)
    await c.answer("Скрываю купленное" if _hide_done[c.from_user.id] else "Показываю всё")


@dp.callback_query(F.data.startswith("rs:"))
async def cb_reset(c: CallbackQuery):
    _, wid, t = c.data.split(":")
    uid = c.from_user.id
    if t == "all":
        store.reset_week(uid, wid)
    else:
        _, items = shop_items(uid, wid)
        store.uncheck_many(uid, [it["iid"] for it in items if in_view(it, parse_trip(t))])
    await refresh_view(c)
    await c.answer("Отметки сброшены")


# ---------- список текстом ----------
@dp.callback_query(F.data.startswith("txt:"))
async def cb_text_list(c: CallbackQuery):
    _, wid, t = c.data.split(":")
    uid = c.from_user.id
    w, items = shop_items(uid, wid)
    people = store.get_people(uid)
    checks = store.checked_set(uid, wid + ":")
    trips = [1, 2] if t == "all" else [parse_trip(t)]
    out = [f"🛒 <b>{esc(w['label'])}</b> · на {people} чел."]
    for trip in trips:
        sel = [it for it in items if in_view(it, trip) and it["iid"] not in checks]
        if not sel:
            continue
        out += ["", f"<b>{esc(TRIP_NAME[trip])}</b>"]
        last = None
        for it in sorted(sel, key=lambda x: (store_of(x["badge"], x["cat"]), x["name"])):
            st = store_of(it["badge"], it["cat"])
            if st != last:
                out.append(f"🏬 <b>{esc(st)}</b>")
                last = st
            q = fmtqty(it["qty"], it["unit"], people)
            out.append(f"  • {esc(it['name'])}" + (f"  <i>{esc(q)}</i>" if q else ""))
    if len(out) == 1:
        out.append("\n🎉 Всё куплено")
    else:
        out.append("\n<i>Перешли список, если нужен — он исчезнет, когда вернёшься к кнопкам.</i>")
    await c.answer()
    await adopt_window(c.message)          # заодно уберёт прошлый список
    chat = c.message.chat.id
    ids = []
    for part in chunks(out):
        sent = await bot.send_message(chat, part)
        ids.append(str(sent.message_id))
    store.set_setting(f"exp:{chat}", ",".join(ids))


# ---------- свои пункты ----------
@dp.callback_query(F.data.startswith("add:"))
async def cb_add_extra(c: CallbackQuery, state: FSMContext):
    _, wid, t = c.data.split(":")
    await state.update_data(wid=wid, trip=t)
    await state.set_state(Edit.extra_name)
    await safe_edit(c.message, "➕ Что добавить в список?\nМожно сразу несколько — каждый пункт с новой строки.",
                    KB([[B("✖︎ Отмена", f"st:{wid}:{t}")]]))
    await c.answer()


@dp.message(Edit.extra_name)
async def on_extra_name(m: Message, state: FSMContext):
    data = await state.get_data()
    wid, t = data["wid"], data["trip"]
    trip = 1 if t == "p" else int(t)
    names = [x.strip(" -•\t") for x in (m.text or "").split("\n") if x.strip(" -•\t")]
    for n in names[:20]:
        store.add_extra(m.from_user.id, wid, trip, n[:80])
    await state.clear()
    await del_msg(m.chat.id, m.message_id)
    trip_v = parse_trip(t)
    text, kb = render_trip(wid, trip_v, m.from_user.id)
    await show(m.chat.id, text, kb)


async def open_extras(c: CallbackQuery, wid, t):
    _, items = shop_items(c.from_user.id, wid)
    mine = [it for it in items if it["eid"] and in_view(it, parse_trip(t))]
    rows = [[B(f"🗑 {it['name']}"[:60], f"xdel:{wid}:{t}:{it['eid']}")] for it in mine]
    rows.append([B("‹ Назад", f"st:{wid}:{t}")])
    await safe_edit(c.message, "Мои пункты. Нажми, чтобы удалить:", KB(rows))


@dp.callback_query(F.data.startswith("xlist:"))
async def cb_extras(c: CallbackQuery):
    _, wid, t = c.data.split(":")
    await open_extras(c, wid, t)
    await c.answer()


@dp.callback_query(F.data.startswith("xdel:"))
async def cb_extra_del(c: CallbackQuery):
    _, wid, t, eid = c.data.split(":")
    store.del_extra(c.from_user.id, wid, eid)
    await c.answer("Удалено")
    _, items = shop_items(c.from_user.id, wid)
    if any(it["eid"] and in_view(it, parse_trip(t)) for it in items):
        await open_extras(c, wid, t)
    else:
        text, kb = render_trip(wid, parse_trip(t), c.from_user.id)
        await safe_edit(c.message, text, kb)


# ---------- recipes ----------
@dp.callback_query(F.data == "recs")
async def cb_recs(c: CallbackQuery):
    rows = [[B(r["name"], f"rv:{r['id']}")] for r in store.all_recipes()]
    rows.append([B("➕ Добавить рецепт", "radd")])
    rows.append([B("⌂ Меню", "menu")])
    await safe_edit(c.message, "Рецепты:", KB(rows))
    await c.answer()


def recipe_text(r):
    head = f"📖 <b>{esc(r['name'])}</b>"
    if r.get("text"):
        return f"{head}\n{esc(r['text'])}"
    lines = [head]
    if r.get("out"):
        lines.append(f"<i>Выход: {esc(r['out'])}</i>")
    lines += ["", "<b>Ингредиенты</b>"]
    lines += [f"  • {esc(x)}" for x in r.get("ing", [])]
    lines += ["", "<b>Приготовление</b>"]
    lines += [f"  <b>{i}.</b> {esc(x)}" for i, x in enumerate(r.get("steps", []), 1)]
    if r.get("tips"):
        lines += ["", "<b>Советы</b>"] + [f"  • <i>{esc(x)}</i>" for x in r["tips"]]
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
    r = store.get_recipe(rid)
    cur = re.sub(r"<[^>]+>", "", recipe_text(r)) if r else ""
    await safe_edit(c.message,
                    "✏️ <b>Правка рецепта</b>\n"
                    "<i>Нажми на текст ниже — он скопируется. Вставь, поправь и пришли обратно.</i>\n\n"
                    f"<pre>{esc(clip(cur, 3000))}</pre>",
                    KB([[B("✖︎ Отмена", f"rv:{rid}")]]))
    await c.answer()


@dp.message(Edit.recipe_text)
async def on_recipe_text(m: Message, state: FSMContext):
    data = await state.get_data()
    r = store.get_recipe(data["rid"])
    if r:
        r["text"] = m.text or ""
        store.save_recipe(r)
    await del_msg(m.chat.id, m.message_id)
    await state.clear()
    if r:
        await show(m.chat.id, recipe_text(r),
                   KB([[B("✏️ Редактировать", f"re:{r['id']}")],
                       [B("‹ Рецепты", "recs"), B("⌂ Меню", "menu")]]))


@dp.callback_query(F.data == "radd")
async def cb_recipe_add(c: CallbackQuery, state: FSMContext):
    await state.set_state(Edit.new_name)
    await safe_edit(c.message, "➕ Название нового рецепта?", KB([[B("✖︎ Отмена", "recs")]]))
    await c.answer()


@dp.message(Edit.new_name)
async def on_new_name(m: Message, state: FSMContext):
    data_name = m.text or "Без названия"
    await state.update_data(name=data_name)
    await state.set_state(Edit.new_text)
    await del_msg(m.chat.id, m.message_id)
    await show(m.chat.id, f"➕ {data_name}\n\nТеперь пришли текст рецепта одним сообщением.",
               KB([[B("✖︎ Отмена", "recs")]]))


@dp.message(Edit.new_text)
async def on_new_text(m: Message, state: FSMContext):
    data = await state.get_data()
    import re
    rid = "u" + re.sub(r"\W+", "", (data["name"] or "r"))[:12].lower() + str(abs(hash(data["name"])) % 1000)
    r = {"id": rid, "name": data["name"], "out": "", "ing": [], "steps": [], "tips": [], "text": m.text or ""}
    store.save_recipe(r)
    await del_msg(m.chat.id, m.message_id)
    await state.clear()
    await show(m.chat.id, recipe_text(r),
               KB([[B("✏️ Редактировать", f"re:{rid}")],
                   [B("‹ Рецепты", "recs"), B("⌂ Меню", "menu")]]))


# ---------- случайное меню на неделю ----------
SLOTS = ["Завтрак", "Обед", "Полдник", "Ужин", "2-й ужин"]
# блоки по два дня — как в планах диетолога: готовим один раз, едим два дня
BLOCKS = [("Понедельник", "Вторник"), ("Среда", "Четверг"),
          ("Пятница", "Суббота"), ("Воскресенье",)]
PANTRY_CATS = {"Крупы и лапша", "Готовое, напитки, бакалея"}


def meal_pool(allerg=frozenset()):
    """Все варианты приёмов пищи из готовых планов: слот -> список наборов блюд.
    Блюда с выбранными аллергенами не берём."""
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
                if allerg and any(conflicts(d, allerg) for d in dishes):
                    continue          # не подходит по аллергенам
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


def generate_week(uid=None):
    allerg = user_allergens(uid) if uid else set()
    pool = meal_pool(allerg)
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
    lines = [f"🎲 <b>{esc(w['label'])}</b>", f"<i>{esc(w['note'])}</i>", ""]
    for day in w["days"]:
        lines.append(f"<b>{esc(day['name'])}</b>")
        for m in day["meals"]:
            lines.append(f"  <i>{esc(m['t'])}</i>  " + esc("; ".join(m["d"])))
        lines.append("")
    lines.append(f"Позиций в закупке: <b>{len(w['shop'])}</b>")
    return "\n".join(lines)


def draft_kb():
    return KB([[B("🔄 Другой вариант", "genrun")],
               [B("💾 Сохранить меню", "gsave")],
               [B("🚫 Что исключить", "gen")],
               [B("‹ Расписание", "schweeks"), B("⌂ Меню", "menu")]])


@dp.callback_query(F.data == "genrun")
async def cb_generate(c: CallbackQuery):
    w = generate_week(c.from_user.id)
    _draft[c.from_user.id] = w
    await safe_edit(c.message, clip(draft_text(w)), draft_kb())
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
    if not await admin_only(m):
        return
    fn = (m.document.file_name or "").lower()
    await del_msg(m.chat.id, m.message_id)
    if not fn.endswith(".json"):
        return await show(m.chat.id, "Обновить план можно файлом plan.json.",
                          KB([[B("⌂ Меню", "menu")]]))
    await show(m.chat.id, "📥 Читаю план…")
    try:
        buf = io.BytesIO()
        await bot.download(m.document, destination=buf)
        weeks, recipes = await asyncio.to_thread(store.replace_plan, buf.getvalue())
        await show(m.chat.id,
            f"✅ План обновлён.\nНедель: {weeks} · рецептов: {recipes}\n\n"
            "Прежняя версия сохранена. Отметки «куплено» не тронуты.",
            KB([[B("📅 Расписание", "schweeks"), B("🛒 Закупки", "shopsrc")],
                [B("⌂ Меню", "menu")]]))
    except Exception as e:
        logging.exception("replace plan")
        await show(m.chat.id, f"Не получилось обновить план: {e}", KB([[B("⌂ Меню", "menu")]]))


# ---------- всё остальное ----------
@dp.message()
async def fallback(m: Message, state: FSMContext):
    """Непонятное сообщение убираем и показываем главный экран.
    В группах молчим: там чужие сообщения удалять нельзя."""
    if m.chat.id < 0:
        return
    await state.clear()
    store.add_chat(m.chat.id)
    await del_msg(m.chat.id, m.message_id)
    await show(m.chat.id, menu_text(m.from_user.id), menu_kb(m.from_user.id, m.chat.id))


# ---------- напоминания (личные у каждого) ----------
async def remind_morning(uid):
    switched = maybe_rollover(uid)          # понедельник — переходим на следующее меню
    w = active_plan(uid)
    if not w:
        return
    txt, kb = day_card(w, tznow().date(), "🌅 Доброе утро! Сегодня", uid)
    if not txt:
        return
    if switched:
        txt = f"🔄 Началась новая неделя — перешли на «{esc(switched['label'])}».\n\n" + txt
    try:
        await notify(uid, "morning", txt, kb)
    except Exception:
        logging.exception("morning to %s", uid)


async def remind_meal(uid, slot):
    """Напоминание о конкретном приёме пищи: что съесть и какие добавки."""
    w = active_plan(uid)
    if not w:
        return
    day = day_for_date(w, tznow().date(), uid)
    if not day:
        return
    meal = next((m for m in day["meals"] if m["t"] == slot), None)
    sb = supps_by_slot(uid).get(slot, [])
    if not meal or (not meal["d"] and not sb):
        return
    allerg = user_allergens(uid)
    lines = [f"🍽 <b>{esc(slot)}</b>", ""]
    for x in meal["d"]:
        bad = conflicts(x, allerg)
        lines.append(dish_line(x) + (f"  ⚠️ <i>{esc(', '.join(bad))}</i>" if bad else ""))
    for sp in sb:
        lines.append(f"💊 <i>{esc(supp_text(sp))}</i>")
    kb = KB([[B("🍽 Весь день", "today")], [B("⌂ Меню", "menu")]])
    try:
        await notify(uid, "meal" + slot, "\n".join(lines), kb)
    except Exception:
        logging.exception("meal reminder to %s", uid)


async def remind_evening(uid):
    """Вечером — только если завтра начинается новый блок и надо готовить."""
    w = active_plan(uid)
    if not w:
        return
    today = tznow().date()
    d_today = day_for_date(w, today, uid)
    d_tom = day_for_date(w, today + timedelta(days=1), uid)
    if not d_tom or meals_equal(d_tom, d_today):
        return
    recs = recipes_in(d_tom)
    lines = [f"🌙 <b>Завтра — {esc(d_tom['name'])}</b>",
             "<i>Новый блок: блюда меняются, готовим заново.</i>"]
    if recs:
        lines.append("\n🍳 <b>Готовим сами:</b> " + esc(", ".join(r["name"] for r in recs)))
        lines.append("<i>Загляни в рецепт — что-то может понадобиться разморозить заранее.</i>")
    lines.append("")
    for m in d_tom["meals"]:
        if m["d"]:
            lines.append(f"<b>{esc(m['t'])}</b>  " + esc("; ".join(m["d"])))
    rows = [[B(f"📖 {r['name']}", f"rv:{r['id']}")] for r in recs]
    rows.append([B("🛒 Закупки", f"shop:{w['id']}"), B("⌂ Меню", "menu")])
    try:
        await notify(uid, "evening", "\n".join(lines), KB(rows))
    except Exception:
        logging.exception("evening to %s", uid)


async def remind_shop(uid, kind):
    w = active_plan(uid)
    base = {"t1": "🛒 <b>Пора сделать Закуп 1</b>\n<i>Бакалея на неделю + свежее на первую половину.</i>",
            "t2": "🛒 <b>Пора сделать Закуп 2</b>\n<i>Свежее на вторую половину. И свежий хлеб к выходным!</i>"}[kind]
    kb = KB([[B("⌂ Меню", "menu")]])
    if w:
        trip = 1 if kind == "t1" else 2
        shop = w.get("shop", [])
        checks = store.checked_set(uid, w["id"] + ":")
        shop_trip = [i for i, it in enumerate(shop) if (it[6] if len(it) > 6 else 1) == trip]
        left = [i for i in shop_trip if f"{w['id']}:{i}" not in checks]
        if not left:
            return                      # всё уже куплено — не тревожим
        base += (f"\n{esc(w['label'])}\n{bar(len(shop_trip) - len(left), len(shop_trip))}"
                 f"\nОсталось купить: <b>{len(left)}</b>")
        kb = KB([[B("🛒 Открыть список", f"shop:{w['id']}")], [B("⌂ Меню", "menu")]])
    try:
        await notify(uid, "shop" + kind, base, kb)
    except Exception:
        logging.exception("shop reminder to %s", uid)


sched = None


def reschedule():
    """Пересобрать задания: у каждого свои напоминания и своё время."""
    if sched is None:
        return
    for job in sched.get_jobs():
        try:
            sched.remove_job(job.id)
        except Exception:
            pass
    for uid in store.all_chats():
        if rem_on(uid, "morning_on"):
            raw = store.get_user_setting(uid, "morning_time", MORNING_DEFAULT)
            try:
                hh, mm = [int(x) for x in raw.split(":")[:2]]
            except Exception:
                hh, mm = 7, 30
            sched.add_job(remind_morning, "cron", hour=hh, minute=mm, args=[uid],
                          id=f"m:{uid}", misfire_grace_time=3600, coalesce=True)
        if rem_on(uid, "evening_on"):
            hh, mm = [int(x) for x in EVENING_DEFAULT.split(":")]
            sched.add_job(remind_evening, "cron", hour=hh, minute=mm, args=[uid],
                          id=f"e:{uid}", misfire_grace_time=3600, coalesce=True)
        if rem_on(uid, "meals_on", "0"):          # по умолчанию выключено
            for si, slot in enumerate(SLOTS):
                if not meal_on(uid, slot):
                    continue
                try:
                    hh, mm = [int(x) for x in meal_time(uid, slot).split(":")]
                except Exception:
                    continue
                sched.add_job(remind_meal, "cron", hour=hh, minute=mm, args=[uid, slot],
                              id=f"ml:{uid}:{si}", misfire_grace_time=1800, coalesce=True)
        if rem_on(uid, "shop_on"):
            sched.add_job(remind_shop, "cron", day_of_week="sun", hour=10, minute=0,
                          args=[uid, "t1"], id=f"s1:{uid}", misfire_grace_time=7200, coalesce=True)
            sched.add_job(remind_shop, "cron", day_of_week="wed", hour=18, minute=0,
                          args=[uid, "t2"], id=f"s2:{uid}", misfire_grace_time=7200, coalesce=True)


async def main():
    global sched
    store.init()
    if ADMINS:
        store.migrate_personal(sorted(ADMINS)[0])   # старые общие настройки → владельцу
    if os.path.exists(UPDATE_FLAG):
        try:
            with open(UPDATE_FLAG, encoding="utf-8") as f:
                cid = int(f.read().strip())
            await show(cid, f"✅ Бот снова на связи.\n🏷 {version_line()}\n\n" + menu_text(cid),
                       menu_kb(cid, cid))
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
    await setup_commands()
    try:                                   # веб-часть для Mini App
        import webapp
        await webapp.start()
    except Exception:
        logging.exception("Mini App API не запустился — бот работает без него")
    asyncio.create_task(healthcheck())
    logging.info("Bot started, TZ=%s, jobs=%s", TZ, [j.id for j in sched.get_jobs()])
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
