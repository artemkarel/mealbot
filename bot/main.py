"""Бот: принимает пересланный файл плана, шлёт напоминания, открывает мини-приложение."""
import asyncio, os, shutil, subprocess, sys, tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, WebAppInfo, InlineKeyboardMarkup,
                           InlineKeyboardButton, BotCommand)
from import_plan import save
from app.db import connect
from app.cooking import same_days

ROOT = Path(__file__).parent.parent

TOKEN = os.environ["BOT_TOKEN"]
URL = os.environ["WEBAPP_URL"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
MORNING = os.getenv("REMIND_MORNING", "07:30")   # что взять с собой + сроки годности
EVENING = os.getenv("REMIND_EVENING", "21:00")   # что подготовить на завтра

bot = Bot(TOKEN)
kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Открыть план", web_app=WebAppInfo(url=URL))]])

DAY_NAMES = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


def allowed(m: Message) -> bool:
    return not ALLOWED or m.from_user.id in ALLOWED


async def start(m: Message):
    if not allowed(m): return
    await m.answer("Пришли файл с планом от диетолога — разберу и открою.", reply_markup=kb)


async def got_file(m: Message):
    if not allowed(m): return
    name = m.document.file_name or "plan.docx"
    if not name.lower().endswith((".docx", ".txt")):
        return await m.answer("Нужен .docx или .txt")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        await bot.download(m.document, destination=path)
        try:
            plan_id = save(str(path))
        except Exception as e:
            return await m.answer(f"Не смог разобрать: {e}")
    await m.answer(f"План загружен (#{plan_id}).", reply_markup=kb)


# ---------- команды ----------

DAY_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def day_menu_text(offset: int) -> str:
    """Меню на сегодня (offset=0) или завтра (offset=1) одним сообщением."""
    con = connect()
    plan = _active_plan(con)
    if not plan:
        return "План не загружен — пришли файл от диетолога."
    day = (date.today() + timedelta(days=offset)).weekday()
    rows = con.execute(
        "SELECT meal, name, COALESCE(qty_max,qty_min) q, unit FROM plan_items"
        " WHERE plan_id=? AND day_index=? ORDER BY meal_index, id",
        (plan["id"], day)).fetchall()
    if not rows:
        return "На этот день в плане ничего нет."
    lines, cur = [f"🍽 {DAY_FULL[day]}"], None
    for r in rows:
        if r["meal"] != cur:
            cur = r["meal"]
            lines.append(f"\n{cur}:")
        q = f' — {r["q"]:g} {r["unit"]}' if r["q"] else ""
        lines.append(f"  • {r['name']}{q}")
    return "\n".join(lines)


async def cmd_today(m: Message):
    if not allowed(m): return
    await m.answer(day_menu_text(0), reply_markup=kb)


async def cmd_tomorrow(m: Message):
    if not allowed(m): return
    await m.answer(day_menu_text(1), reply_markup=kb)


async def cmd_myid(m: Message):
    # без проверки allowed: команда и нужна, чтобы узнать свой ID для белого списка
    await m.answer(f"Твой Telegram ID: {m.from_user.id}")


async def cmd_status(m: Message):
    if not allowed(m): return
    con = connect()
    plan = _active_plan(con)
    if plan:
        n = con.execute("SELECT COUNT(*) c FROM plan_items WHERE plan_id=?",
                        (plan["id"],)).fetchone()["c"]
        head = (f"План: {plan['title']}\nПозиций: {n}, человек: {plan['persons']}, "
                f"загружен {plan['created_at'][:10]}")
    else:
        head = "План не загружен."
    fridge = con.execute("SELECT COUNT(*) c FROM purchases WHERE used=0").fetchone()["c"]
    await m.answer(f"{head}\nВ холодильнике на учёте: {fridge}\n"
                   f"Напоминания: утром {MORNING}, вечером {EVENING}", reply_markup=kb)


async def cmd_version(m: Message):
    if not allowed(m): return
    try:
        v = subprocess.run(["git", "-C", str(ROOT), "log", "-1",
                            "--format=%h · %ad · %s", "--date=format:%d.%m.%Y %H:%M"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        v = f"не удалось узнать: {e}"
    await m.answer(f"Версия кода: {v or 'git недоступен'}")


def _run_detached(args):
    """Запуск через systemd-run: переживает перезапуск самого бота."""
    if shutil.which("systemd-run"):
        subprocess.Popen(["systemd-run", "--collect"] + args)
    else:  # локальная разработка без systemd
        subprocess.Popen(args)


async def cmd_update(m: Message):
    if not allowed(m): return
    await m.answer("Обновляюсь с GitHub… Сервисы перезапустятся, "
                   "через минуту проверь /version.")
    _run_detached([str(ROOT / "update.sh")])


async def cmd_restart(m: Message):
    if not allowed(m): return
    await m.answer("Перезапускаю бота и веб…")
    _run_detached(["systemctl", "restart", "mealplan-bot", "mealplan-web"])


BOT_COMMANDS = [
    BotCommand(command="start", description="Открыть меню"),
    BotCommand(command="today", description="Что сегодня едим"),
    BotCommand(command="tomorrow", description="Что завтра"),
    BotCommand(command="myid", description="Мой Telegram ID"),
    BotCommand(command="status", description="Состояние бота"),
    BotCommand(command="version", description="Версия кода"),
    BotCommand(command="update", description="Обновить с GitHub"),
    BotCommand(command="restart", description="Перезапустить бота"),
]


# ---------- напоминания ----------

def _active_plan(con):
    return con.execute("SELECT * FROM plans WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()


def build_morning() -> str:
    """Утро: что взять с собой (обед и полдник) + что истекает по сроку."""
    con = connect()
    plan = _active_plan(con)
    parts = []
    if plan:
        day = date.today().weekday()
        rows = con.execute(
            "SELECT meal, name, COALESCE(qty_max,qty_min) q, unit FROM plan_items"
            " WHERE plan_id=? AND day_index=? AND meal IN ('Обед','Полдник')"
            " ORDER BY meal_index, id", (plan["id"], day)).fetchall()
        if rows:
            lines, cur = [], None
            for r in rows:
                if r["meal"] != cur:
                    cur = r["meal"]
                    lines.append(f"\n{cur}:")
                q = f' — {r["q"]:g} {r["unit"]}' if r["q"] else ""
                lines.append(f"  • {r['name']}{q}")
            parts.append("🎒 Собрать с собой на работу:" + "\n".join(lines))
    expiring = con.execute(
        "SELECT pr.name, CAST(julianday(pu.expires_at) - julianday(date('now')) AS INT) d"
        " FROM purchases pu JOIN products pr ON pr.id = pu.product"
        " WHERE pu.used=0 AND pu.frozen=0"
        " AND julianday(pu.expires_at) - julianday(date('now')) <= 1"
        " ORDER BY d").fetchall()
    if expiring:
        lines = [f"  • {r['name']} — " + ("срок вышел" if r["d"] < 0 else
                 "сегодня последний день" if r["d"] == 0 else "до завтра") for r in expiring]
        parts.append("⏳ Съесть в первую очередь:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def build_evening() -> str:
    """Вечер: что подготовить на завтра + подсказка готовить сразу на два дня."""
    con = connect()
    plan = _active_plan(con)
    if not plan:
        return ""
    tomorrow = (date.today() + timedelta(days=1)).weekday()
    parts = []
    prep = con.execute("SELECT DISTINCT text FROM prep_tasks WHERE plan_id=? AND day_index=?",
                       (plan["id"], tomorrow)).fetchall()
    if prep:
        parts.append("🌙 Подготовить на завтра с вечера:\n" +
                     "\n".join(f"  • {r['text']}" for r in prep))
    dup = same_days(plan["id"]).get(tomorrow, [])
    after = [d for d in dup if d == tomorrow + 1]
    if after:
        parts.append(f"♻️ Завтра и в {DAY_NAMES[after[0]]} меню совпадает — "
                     "готовь завтра сразу на два дня.")
    return "\n\n".join(parts)


async def send_all(text: str):
    if not text:
        return
    if not ALLOWED:
        print("Напоминание не отправлено: ALLOWED_USER_IDS пуст")
        return
    for uid in ALLOWED:
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            print(f"Не отправилось {uid}: {e}")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


async def reminder_loop():
    """Раз в минуту сверяет время. Шлёт в 30-минутном окне после цели —
    чтобы после перезапуска не рассылать вчерашнее."""
    sent = set()
    while True:
        now = datetime.now()
        cur = now.hour * 60 + now.minute
        for tag, target, build in (("morning", MORNING, build_morning),
                                   ("evening", EVENING, build_evening)):
            key = (now.date(), tag)
            t = _minutes(target)
            if t <= cur < t + 30 and key not in sent:
                sent.add(key)
                try:
                    await send_all(build())
                except Exception as e:
                    print(f"Ошибка напоминания {tag}: {e}")
        if len(sent) > 100:
            sent = {k for k in sent if k[0] >= now.date()}
        await asyncio.sleep(60)


async def main():
    # Dispatcher создаётся внутри запущенного event loop —
    # на Python 3.9 иначе падает asyncio.Lock внутри aiogram
    dp = Dispatcher()
    dp.message.register(start, CommandStart())
    dp.message.register(cmd_today, Command("today"))
    dp.message.register(cmd_tomorrow, Command("tomorrow"))
    dp.message.register(cmd_myid, Command("myid"))
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(cmd_version, Command("version"))
    dp.message.register(cmd_update, Command("update"))
    dp.message.register(cmd_restart, Command("restart"))
    dp.message.register(got_file, F.document)
    await bot.set_my_commands(BOT_COMMANDS)   # заменяет список команд старого бота
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
