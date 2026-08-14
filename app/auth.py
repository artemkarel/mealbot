"""Проверка initData мини-приложений: Telegram и MAX (алгоритм у обоих одинаковый)."""
from __future__ import annotations
import hashlib, hmac, os, json
from urllib.parse import parse_qsl

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}

def check(init_data: str) -> dict | None:
    if not init_data or not BOT_TOKEN:
        return None
    data = dict(parse_qsl(init_data, strict_parsing=True))
    got = data.pop("hash", "")
    check_str = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    user = json.loads(data.get("user", "{}"))
    if ALLOWED and user.get("id") not in ALLOWED:
        return None
    return user


def check_max(init_data: str) -> dict | None:
    """WebAppData мини-приложения MAX: HMAC-SHA256, secret = HMAC('WebAppData', токен)."""
    if not init_data or not MAX_BOT_TOKEN:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    got = data.pop("hash", "")
    if not got:
        return None
    check_str = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hmac.new(b"WebAppData", MAX_BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        user = json.loads(data.get("user", "{}") or "{}")
    except Exception:
        return None
    return user if user.get("id") else None
