"""Бот: принимает пересланный файл плана, шлёт напоминания, открывает мини-приложение."""
import asyncio, os, sys, tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from import_plan import save
from app.db import connect
from app.cooking import same_days

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
    dp.message.register(got_file, F.document)
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
