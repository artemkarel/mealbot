"""Бот: принимает пересланный файл плана, шлёт напоминания, открывает мини-приложение."""
import asyncio, json, os, shutil, subprocess, sys, tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:          # старые питоны без zoneinfo — работаем по часам сервера
    ZoneInfo = None
sys.path.insert(0, str(Path(__file__).parent.parent))
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (Message, WebAppInfo, InlineKeyboardMarkup,
                           InlineKeyboardButton, BotCommand)
from import_plan import save
from app.db import connect, current_plan, persons_of, set_current_plan, touch_user, MAX_UID_OFFSET
from app.texts import (DAY_FULL, DAY_NAMES, day_menu_text, build_morning, build_evening,
                       build_meal, build_menu, build_shopping_note, user_tz, _minutes)

ROOT = Path(__file__).parent.parent

TOKEN = os.environ["BOT_TOKEN"]
URL = os.environ["WEBAPP_URL"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
ADMINS = {int(x) for x in os.getenv("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x} or ALLOWED

bot = Bot(TOKEN)
kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Открыть план", web_app=WebAppInfo(url=URL))]])

# ---------- чат «в одном окне» ----------
# Перед каждым новым сообщением бот удаляет предыдущую пару вопрос-ответ,
# чтобы переписка не копилась. id для очистки живут в таблице settings.

def _clean_ids(chat_id):
    con = connect()
    r = con.execute("SELECT value FROM settings WHERE key=?",
                    ("clean:%s" % chat_id,)).fetchone()
    try:
        return json.loads(r["value"]) if r else []
    except Exception:
        return []


def _save_clean_ids(chat_id, ids):
    con = connect()
    con.execute("INSERT INTO settings(key,value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("clean:%s" % chat_id, json.dumps(ids[-20:])))
    con.commit()


async def tidy_chat(chat_id):
    for mid in _clean_ids(chat_id):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass                     # старше 48 часов или уже удалено
    _save_clean_ids(chat_id, [])


async def reply_clean(m: Message, text, **kw):
    """Ответ с очисткой: удаляет прошлые сообщения, запоминает новую пару."""
    await tidy_chat(m.chat.id)
    sent = await m.answer(text, **kw)
    _save_clean_ids(m.chat.id, [m.message_id, sent.message_id])
    return sent


def allowed(m: Message) -> bool:
    """Пустой ALLOWED_USER_IDS = бот открыт для всех; данные у каждого свои."""
    if ALLOWED and m.from_user.id not in ALLOWED:
        return False
    try:                                # отмечаем активность для списка «кто пользуется»
        u = m.from_user
        touch_user(connect(), u.id, u.first_name, u.last_name, u.username, "bot")
    except Exception:
        pass
    return True


def admin(m: Message) -> bool:
    return m.from_user.id in ADMINS


async def start(m: Message):
    if not allowed(m): return
    await reply_clean(m, "Пришли файл с планом от диетолога — разберу и открою.\n"
                   "План, отметки и закупки у каждого пользователя свои.", reply_markup=kb)


async def got_file(m: Message):
    if not allowed(m): return
    name = m.document.file_name or "plan.docx"
    if not name.lower().endswith((".docx", ".txt")):
        return await reply_clean(m, "Нужен .docx или .txt")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        await bot.download(m.document, destination=path)
        try:
            plan_id = save(str(path), m.from_user.id)
        except Exception as e:
            return await reply_clean(m, f"Не смог разобрать: {e}")
    con = connect()
    set_current_plan(con, m.from_user.id, plan_id)
    con.commit()
    await reply_clean(m, f"План загружен (#{plan_id}) и выбран текущим.", reply_markup=kb)


# ---------- команды ----------

async def cmd_today(m: Message):
    if not allowed(m): return
    await reply_clean(m, day_menu_text(0, m.from_user.id), reply_markup=kb)


async def cmd_tomorrow(m: Message):
    if not allowed(m): return
    await reply_clean(m, day_menu_text(1, m.from_user.id), reply_markup=kb)


async def cmd_myid(m: Message):
    # без проверки allowed: команда и нужна, чтобы узнать свой ID для белого списка
    await reply_clean(m, f"Твой Telegram ID: {m.from_user.id}")


async def cmd_status(m: Message):
    if not allowed(m): return
    uid = m.from_user.id
    con = connect()
    plan = current_plan(con, uid)
    if plan:
        n = con.execute("SELECT COUNT(*) c FROM plan_items WHERE plan_id=?",
                        (plan["id"],)).fetchone()["c"]
        head = (f"План: {plan['title']}\nПозиций: {n}, человек: {persons_of(con, uid)}, "
                f"загружен {plan['created_at'][:10]}")
    else:
        head = "План не загружен."
    fridge = con.execute("SELECT COUNT(*) c FROM purchases WHERE used=0 AND user_id=?",
                         (uid,)).fetchone()["c"]
    rems = con.execute("SELECT COUNT(*) c FROM user_reminders WHERE user_id=? AND enabled=1",
                       (uid,)).fetchone()["c"]
    await reply_clean(m, f"{head}\nВ холодильнике на учёте: {fridge}\n"
                   f"Напоминаний настроено: {rems} (настраиваются в приложении, Профиль)",
                   reply_markup=kb)


async def cmd_users(m: Message):
    if not admin(m): return
    con = connect()
    rows = con.execute(
        "SELECT u.*, (SELECT COUNT(*) FROM plans p WHERE p.user_id = u.user_id) plans"
        " FROM users u ORDER BY u.last_seen DESC LIMIT 30").fetchall()
    if not rows:
        return await reply_clean(m, "Пока никого не видно — список наполнится по мере визитов.")
    lines = ["👥 Кто пользуется:"]
    for r in rows:
        name = " ".join(x for x in (r["first_name"], r["last_name"]) if x) or "Без имени"
        if r["username"]: name += f" (@{r['username']})"
        lines.append(f"• {name} — id {r['user_id']}, планов: {r['plans']},"
                     f" был(а): {(r['last_seen'] or '')[:16]}")
    await reply_clean(m, "\n".join(lines))


async def cmd_version(m: Message):
    if not admin(m): return
    try:
        v = subprocess.run(["git", "-C", str(ROOT), "log", "-1",
                            "--format=%h · %ad · %s", "--date=format:%d.%m.%Y %H:%M"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        v = f"не удалось узнать: {e}"
    await reply_clean(m, f"Версия кода: {v or 'git недоступен'}")


def _run_detached(args):
    """Запуск через systemd-run: переживает перезапуск самого бота."""
    if shutil.which("systemd-run"):
        subprocess.Popen(["systemd-run", "--collect"] + args)
    else:  # локальная разработка без systemd
        subprocess.Popen(args)


async def cmd_update(m: Message):
    if not admin(m): return
    await reply_clean(m, "Обновляюсь с GitHub… Сервисы перезапустятся, "
                   "через минуту проверь /version.")
    _run_detached([str(ROOT / "update.sh")])


async def cmd_restart(m: Message):
    if not admin(m): return
    await reply_clean(m, "Перезапускаю бота и веб…")
    _run_detached(["systemctl", "restart", "mealplan-bot", "mealplan-web"])


BOT_COMMANDS = [
    BotCommand(command="start", description="Открыть меню"),
    BotCommand(command="today", description="Что сегодня едим"),
    BotCommand(command="tomorrow", description="Что завтра"),
    BotCommand(command="myid", description="Мой Telegram ID"),
    BotCommand(command="status", description="Состояние бота"),
    BotCommand(command="users", description="Кто пользуется (для админа)"),
    BotCommand(command="version", description="Версия кода"),
    BotCommand(command="update", description="Обновить с GitHub"),
    BotCommand(command="restart", description="Перезапустить бота"),
]


async def on_text(m: Message):
    """Свободный текст: подсказываем команды — вопросы больше не обрабатываем."""
    if not allowed(m) or not m.text:
        return
    await reply_clean(m, "Я показываю твой план питания.\n"
                         "/today — что сегодня, /tomorrow — что завтра.\n"
                         "Планы, закупки и напоминания — в приложении (кнопка ниже).",
                      reply_markup=kb)


# ---------- напоминания ----------

# если по плану делать нечего — бот всё равно отвечает, а не молчит,
# иначе кажется, что напоминание не сработало
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
        await tidy_chat(uid)         # чат «в одном окне»: убираем прошлые сообщения
        sent = await bot.send_message(uid, text, reply_markup=kb)
        _save_clean_ids(uid, [sent.message_id])


async def reminder_loop():
    """Раз в минуту сверяет время с настройками пользователей — по часовому поясу
    телефона каждого. Шлёт в 30-минутном окне после цели, чтобы после перезапуска
    не рассылать вчерашнее."""
    sent = set()
    while True:
        now_utc = datetime.now(timezone.utc)
        try:
            con = connect()
            tz_cache = {}
            for r in con.execute("SELECT * FROM user_reminders WHERE enabled=1"):
                if r["user_id"] >= MAX_UID_OFFSET:
                    continue          # пользователей MAX обслуживает их бот
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
                        print(f"Ошибка напоминания #{r['id']}: {e}")
        except Exception as e:
            print(f"Ошибка цикла напоминаний: {e}")
        if len(sent) > 500:
            today_utc = now_utc.date()
            sent = {k for k in sent if (k[0] - today_utc).days >= -1}
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
    dp.message.register(cmd_users, Command("users"))
    dp.message.register(cmd_version, Command("version"))
    dp.message.register(cmd_update, Command("update"))
    dp.message.register(cmd_restart, Command("restart"))
    dp.message.register(got_file, F.document)
    dp.message.register(on_text, F.text)      # всё остальное — короткая подсказка
    await bot.set_my_commands(BOT_COMMANDS)   # заменяет список команд старого бота
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
