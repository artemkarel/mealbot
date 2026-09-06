"""ВкусВилл: официальный MCP-сервер (mcp001.vkusvill.ru) — поиск товаров и ссылка на готовую корзину.

Ссылка вида https://vkusvill.ru/?share_basket=… открывает сайт/приложение ВкусВилл с уже
заполненной корзиной. В одной ссылке до 30 позиций (проверено; в описании схемы стоит 20); количество q — упаковки для штучных
товаров (unit «шт») и килограммы для весовых (unit «кг»).
"""
import json, math, logging, urllib.request, urllib.error
log = logging.getLogger(__name__)

MCP_URL = "https://mcp001.vkusvill.ru/mcp"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
MAX_PER_LINK = 30                            # реальный лимит сервера (схема говорит 20)


def _call(tool: str, args: dict, timeout: int = 15):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode()
    req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
        "User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        log.warning("vkusvill %s: HTTP %s: %s", tool, e.code, e.read()[:300].decode("utf-8", "ignore"))
        raise RuntimeError(f"сервер ВкусВилл вернул ошибку {e.code}")
    except OSError as e:                          # таймаут, обрыв, DNS
        log.warning("vkusvill %s: %s", tool, e)
        raise RuntimeError("сервер ВкусВилл не отвечает")
    try:
        r = json.loads(raw)
    except ValueError:
        log.warning("vkusvill %s: not JSON: %r", tool, raw[:200])
        raise RuntimeError("сервер ВкусВилл ответил не по протоколу")
    if "error" in r:
        raise RuntimeError(str((r["error"] or {}).get("message", "MCP error"))[:160])
    txt = "\n".join(c.get("text", "") for c in r.get("result", {}).get("content", []) if c.get("type") == "text")
    try:
        data = json.loads(txt)
    except ValueError:
        raise RuntimeError("сервер ВкусВилл ответил не по протоколу")
    if isinstance(data, dict) and not data.get("ok", True):
        raise RuntimeError(str(data.get("error") or data)[:200])
    return data.get("data", data) if isinstance(data, dict) else data


def search(q: str, page: int = 1):
    return _call("vkusvill_products_search", {"q": q, "page": page, "mode": "custom",
                 "fields": ["id", "xml_id", "name", "price", "unit", "weight", "url"]}).get("items", [])


def details(xml_id: int):
    return _call("vkusvill_product_details", {"id": int(xml_id)})


def cart_link(products: list) -> str:
    """products: [{"xml_id": int, "q": float}] — не больше MAX_PER_LINK."""
    if not products or len(products) > MAX_PER_LINK:
        raise ValueError("в одной ссылке 1..30 товаров")
    return _call("vkusvill_cart_link_create", {"products": [
        {"xml_id": int(p["xml_id"]), "q": float(p["q"])} for p in products]})["link"]


def need_kg(need_value: float, need_unit: str, unit_g) -> float:
    """Потребность из нашего списка (г/мл/шт) в килограммах; unit_g — граммов в штуке по справочнику."""
    if (need_unit or "г") == "шт":
        return (need_value or 0) * (unit_g or 1) / 1000.0
    return (need_value or 0) / 1000.0             # г и мл считаем ~одинаково


def quantity(kg: float, vv_unit: str, vv_weight_kg, pcs=0, need_pcs: float = 0):
    """Сколько заказать во ВкусВилле по СЫРОЙ суммарной потребности (до округления под нашу фасовку).
    Весовая карточка («кг») — килограммы с шагом 0.1; штучная («шт») — число упаковок:
    по штукам в упаковке (pcs — яйца «20 шт»), иначе по весу упаковки."""
    if vv_unit == "кг":
        return max(0.1, math.ceil(kg * 10 - 1e-9) / 10.0)
    if pcs and need_pcs:
        return max(1, math.ceil(need_pcs / pcs - 1e-9))
    if vv_weight_kg:
        return max(1, math.ceil(kg / vv_weight_kg - 1e-9))
    if need_pcs:
        return max(1, math.ceil(need_pcs - 1e-9))
    return 1


def chunks(seq, n=MAX_PER_LINK):
    return [seq[i:i + n] for i in range(0, len(seq), n)]
