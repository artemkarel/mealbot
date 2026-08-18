"""Вызов Claude API — используется разбором документов при добавлении плана.

Ключ ANTHROPIC_API_KEY в .env; без него распознавание выключено.
"""
import os

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "claude-sonnet-5")

DAY_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

async def ask_claude(system, messages, max_tokens=1400, timeout=60):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": max_tokens,
                      "system": system, "messages": messages},
                timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", "HTTP %s" % r.status))
            return "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
