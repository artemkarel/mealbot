"""Сопоставление названий из плана: «Котлеты из индейки «Диетические»» ≈ «Котлеты из индейки на пару».

Нужно, чтобы ссылку из примечания диетолога привязать к позиции плана и к продукту.
"""
import re

STOP = {"из", "на", "с", "со", "и", "в", "для", "по", "без", "или", "пару", "гриле",
        "отварной", "отварная", "отварные", "запеченный", "запеченная", "запеченные",
        "свежий", "свежая", "свежие", "домашний", "домашняя"}


def words(s):
    """Значимые основы слов: регистр, ё, кавычки и окончания не мешают сравнению."""
    s = (s or "").lower().replace("ё", "е")
    out = set()
    for w in re.findall(r"[a-zа-я0-9]+", s):
        if len(w) < 3 or w in STOP:
            continue
        out.add(w[:5])                 # грубая основа: «индейки»/«индейка» -> «индей»
    return out


def similar(a, b) -> float:
    """0..1 — насколько названия про одно и то же.

    Учитываем обе стороны: «Тофу» входит в «Сырники из тофу с курагой»,
    но это разные вещи — ингредиент не должен наследовать ссылку на готовое блюдо.
    """
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return 2 * len(wa & wb) / (len(wa) + len(wb))


def best(label, candidates, key=lambda x: x, threshold=0.6):
    """Лучший кандидат для подписи ссылки или None."""
    best_item, best_score = None, 0.0
    for c in candidates:
        sc = similar(label, key(c))
        if sc > best_score:
            best_item, best_score = c, sc
    return best_item if best_score >= threshold else None
