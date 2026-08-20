"""Ссылки на товары из примечаний плана.

Диетолог пишет «*Котлеты из индейки «Диетические»: https://…». Мы один раз при
импорте определяем, о каком товаре справочника речь, и сохраняем это в plan_links —
дальше закупки просто берут готовую ссылку.
"""
from app.match import best, similar


def resolve_product(con, label):
    """Товар справочника, к которому относится подпись ссылки (или None)."""
    label = (label or "").strip()
    if len(label) < 3:
        return None
    # блюдо справочника с единственным продуктом — самый точный случай
    dishes = {}
    for r in con.execute("SELECT dish, product FROM dishes WHERE product IS NOT NULL"):
        dishes.setdefault(r["dish"], set()).add(r["product"])
    d = best(label, list(dishes), threshold=0.6)
    if d and len(dishes[d]) == 1:
        return next(iter(dishes[d]))
    # иначе — прямое совпадение с названием товара
    prods = [dict(r) for r in con.execute("SELECT id, name FROM products")]
    p = best(label, prods, key=lambda x: x["name"], threshold=0.6)
    return p["id"] if p else None


def save_links(con, plan_id, links):
    """links: [{"text": подпись, "url": адрес}] — пишем без дублей, с привязкой к товару."""
    seen, saved = set(), 0
    for l in links:
        text, url = (l.get("text") or "").strip(), (l.get("url") or "").strip()
        if not url or (text, url) in seen:
            continue
        seen.add((text, url))
        con.execute("INSERT INTO plan_links(plan_id,name,url,product) VALUES(?,?,?,?)",
                    (plan_id, text[:200] or None, url[:500], resolve_product(con, text)))
        saved += 1
    return saved


def urls_by_product(con, plan_id):
    """{product_id: url} — то, что нужно закупкам."""
    out = {}
    for r in con.execute("SELECT product, url FROM plan_links"
                         " WHERE plan_id=? AND product IS NOT NULL", (plan_id,)):
        out.setdefault(r["product"], r["url"])
    return out
