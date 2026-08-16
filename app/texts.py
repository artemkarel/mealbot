"""Тексты меню и напоминаний — общие для ботов Telegram и MAX."""
from datetime import date, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:          # старые питоны без zoneinfo — работаем по часам сервера
    ZoneInfo = None
from app.db import connect, current_plan, persons_of, day_items, plan_for
from app.cooking import same_days
from app.shopping import build as build_shopping

DAY_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
DAY_NAMES = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]


def day_menu_text(offset: int, uid: int) -> str:
    """Меню на сегодня (offset=0) или завтра (offset=1) одним сообщением."""
    con = connect()
    target = date.today() + timedelta(days=offset)
    plan = plan_for(con, uid, target)
    if not plan:
        return ("План не загружен — пришли файл от диетолога." if offset == 0
                else "На эту неделю план ещё не выбран — открой приложение и выбери.")
    day = target.weekday()
    rows = day_items(con, uid, plan["id"], day)   # с учётом замен из приложения
    if not rows:
        return "На этот день в плане ничего нет."
    lines, cur = [f"🍽 {DAY_FULL[day]}"], None
    for r in rows:
        if r["meal"] != cur:
            cur = r["meal"]
            lines.append(f"\n{cur}:")
        q = r["qty_max"] if r["qty_max"] is not None else r["qty_min"]
        lines.append(f"  • {r['name']}" + (f" — {q:g} {r['unit']}" if q else ""))
    return "\n".join(lines)


def build_morning(uid: int) -> str:
    """Утро: что взять с собой (обед и полдник) + что истекает по сроку."""
    con = connect()
    plan = plan_for(con, uid, date.today())
    parts = []
    if plan:
        day = date.today().weekday()
        rows = [r for r in day_items(con, uid, plan["id"], day)
                if r["meal"] in ("Обед", "Полдник")]
        if rows:
            lines, cur = [], None
            for r in rows:
                if r["meal"] != cur:
                    cur = r["meal"]
                    lines.append(f"\n{cur}:")
                q = r["qty_max"] if r["qty_max"] is not None else r["qty_min"]
                lines.append(f"  • {r['name']}" + (f" — {q:g} {r['unit']}" if q else ""))
            parts.append("🎒 Собрать с собой на работу:" + "\n".join(lines))
    expiring = con.execute(
        "SELECT pr.name, CAST(julianday(pu.expires_at) - julianday(date('now')) AS INT) d"
        " FROM purchases pu JOIN products pr ON pr.id = pu.product"
        " WHERE pu.used=0 AND pu.frozen=0 AND pu.user_id=?"
        " AND julianday(pu.expires_at) - julianday(date('now')) <= 1"
        " ORDER BY d", (uid,)).fetchall()
    if expiring:
        lines = [f"  • {r['name']} — " + ("срок вышел" if r["d"] < 0 else
                 "сегодня последний день" if r["d"] == 0 else "до завтра") for r in expiring]
        parts.append("⏳ Съесть в первую очередь:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def build_evening(uid: int) -> str:
    """Вечер: что подготовить на завтра + подсказка готовить сразу на два дня."""
    con = connect()
    plan = plan_for(con, uid, date.today() + timedelta(days=1))
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


def build_meal(uid: int, meal: str) -> str:
    """Напоминание о конкретном приёме пищи + БАДы к нему."""
    con = connect()
    parts = []
    plan = plan_for(con, uid, date.today())
    if plan and meal:
        day = date.today().weekday()
        rows = [r for r in day_items(con, uid, plan["id"], day) if r["meal"] == meal]
        if rows:
            lines = []
            for r in rows:
                q = r["qty_max"] if r["qty_max"] is not None else r["qty_min"]
                lines.append(f"  • {r['name']}" + (f" — {q:g} {r['unit']}" if q else ""))
            parts.append(f"🍽 {meal} по плану:\n" + "\n".join(lines))
    sups = con.execute("SELECT * FROM supplements WHERE user_id=? AND meal=?"
                       " ORDER BY timing", (uid, meal or "")).fetchall()
    if sups:
        lines = [f"  • {s['name']}" + (f" — {s['dose']}" if s["dose"] else "")
                 + f" ({s['timing']})" for s in sups]
        parts.append("💊 Добавки:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def build_menu(uid: int) -> str:
    """Напоминание «что мы едим сегодня» — меню дня одним сообщением."""
    con = connect()
    return day_menu_text(0, uid) if plan_for(con, uid, date.today()) else ""


def build_shopping_note(uid: int) -> str:
    con = connect()
    plan = plan_for(con, uid, date.today())
    if not plan:
        return ""
    s = build_shopping(plan["id"], persons=persons_of(con, uid))
    n1, n2 = len(s["part1"]), len(s["part2"])
    return (f"🧺 Напоминание о закупке.\nВ списке: {n1} позиций в первой части"
            f" и {n2} во второй — открой вкладку «Закупки».")


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def user_tz(con, uid: int):
    """Пояс из телефона пользователя: IANA («Europe/Moscow») или смещение («+03:00»).
    Нет данных — None, время считается по часам сервера."""
    r = con.execute("SELECT tz FROM user_prefs WHERE user_id=?", (uid,)).fetchone()
    name = (r["tz"] or "").strip() if r else ""
    if not name:
        return None
    if name[0] in "+-":
        try:
            sign = 1 if name[0] == "+" else -1
            h, m = name[1:].split(":")
            return timezone(sign * timedelta(hours=int(h), minutes=int(m)))
        except Exception:
            return None
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            return None
    return None
