"""Собирает заготовки справочников из реальных планов.
Запуск: python make_catalog.py /путь/к/папке/с/планами
Результат: data/products.csv и data/dishes.csv — их дальше правишь руками.
"""
import re, sys, csv
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from app.parser.rules import extract_text, parse

# --- транслитерация для id ---
TR = {'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
      'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
      'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'c','ч':'ch','ш':'sh','щ':'sch',
      'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',' ':'_','-':'_'}
def slug(s):
    s = re.sub(r"[«»()/,.]", "", s.lower())
    out = "".join(TR.get(c, c if c.isalnum() else "_") for c in s)
    return re.sub(r"_+", "_", out).strip("_")[:28]

# срок хранения, морозится, категория
SHELF = [
 (r"лосос|сёмг|семг|тунец|гребешк|креветк|хек|щук|уха|рыб|семга",  2,1,"Рыба и морепродукты"),
 (r"курин|курица|индейк|говядин|кролик|телячь|баранин|котлет|ветчин|фарш", 2,1,"Мясо и птица"),
 (r"зелен|салат|шпинат|кинз|петрушк|микс|укроп",                   3,0,"Овощи и зелень"),
 (r"огур|помидор|черри|томат|кабач|тыкв|морков|свекл|батат|авокадо|картоф|лук|чеснок", 10,0,"Овощи и зелень"),
 (r"ягод|голубик|малин|черешн|банан|персик|нектарин|манго|финик|кураг|яблок", 4,0,"Фрукты и ягоды"),
 (r"творож|моцарелл|адыгейск|йогурт|velle|велле|кефир|козь|сыр|тофу", 7,0,"Молочное и заменители"),
 (r"молоко|сливк",                                                 10,0,"Молочное и заменители"),
 (r"яйц|перепелин",                                                21,0,"Яйца"),
 (r"хлебц|тартин|брауни|вафл|печень|кекс|брускет|чипс|мармелад|тост|онигир|хлеб", 7,0,"Хлеб и снеки"),
 (r"гречк|рис|киноа|булгур|овсян|вермишель|спагетти|лапш|фунчоз|мук|крахмал|манн|хлопь", 365,0,"Крупы и мука"),
 (r"масл|соль|сахар|разрыхлит|урбеч|шиповник|кисель|какао|чай|кофе|клетчатк|орех|кешью|специ|хумус", 180,0,"Бакалея"),
 (r"бульон|консерв|тунец конс|оливк|маслин|вялен",                 180,0,"Бакалея"),
]
def shelf(n):
    x = n.lower()
    for rx, d, f, c in SHELF:
        if re.search(rx, x): return d, f, c
    return 14, 0, "Прочее"

# коэффициент = готовый вес / сырой вес
COEF = [
 (r"отварн(ая|ый|ые)? гречк|гречк", 2.2), (r"дикий рис|бурый рис|отварной рис|рисовая каша|рис", 3.0),
 (r"киноа", 3.0), (r"булгур", 2.6), (r"овсян", 3.5),
 (r"спагетти|лапш|вермишель|фунчоз|макарон", 2.5),
 (r"запечен|запечён|на пару|гриль|обжарен|отварн(ое|ые)? (филе|мясо|креветк|гребешк)", 0.8),
 (r"котлет", 0.85), (r"отварные перепелин|отварные яйц", 1.0),
]
def coef(n):
    x = n.lower()
    for rx, c in COEF:
        if re.search(rx, x): return c
    return None

COOKED = re.compile(r"отварн|запечен|запечён|обжарен|на пару|гриль|варен|суп|уха|каша|"
                    r"запеканк|компот|салат|вафли|котлет|тост|сырник|кисель|бульон|борщ|"
                    r"с\s|и\s|фунчоз|стейк", re.I)

def main(folder):
    names = {}
    for p in sorted(Path(folder).glob("*.docx")):
        for d in parse(extract_text(p))["days"]:
            for m in d["meals"]:
                for i in m["items"]:
                    names.setdefault(i["name"].strip(), set()).add(p.stem[:12])

    drinks = re.compile(r"^вода|^кофе$|^зелен(ый|ый) чай|^ромашковый чай|^чай", re.I)
    products, dishes = [], []
    for n in sorted(names):
        if drinks.match(n):
            continue
        d, f, c = shelf(n)
        k = coef(n)
        is_dish = bool(COOKED.search(n)) or k is not None
        pid = slug(n)
        if is_dish:
            base = re.sub(r"^(отварн\w+|запечен\w+|запечён\w+|обжарен\w+|свеж\w+)\s+", "", n, flags=re.I)
            base = re.split(r"\s+(?:с|и|из)\s+", base)[0].strip()
            bid = slug(base)
            products.append([bid, base.capitalize(), d, f, c, "", "г", ""])
            dishes.append([n, "блюдо", bid, "", "", k or "", "ПРОВЕРЬ"])
        else:
            products.append([pid, n, d, f, c, "", "г" if "шт" not in n else "шт", ""])
            dishes.append([n, "товар", pid, 1, "порция", "", ""])

    # схлопываем дубли продуктов
    seen, prod = set(), []
    for r in products:
        if r[0] in seen: continue
        seen.add(r[0]); prod.append(r)

    Path("data").mkdir(exist_ok=True)
    with open("data/products.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id","name","shelf_days","freezable","category","pack","unit","url"])
        w.writerows(sorted(prod, key=lambda r: (r[4], r[1])))
    with open("data/dishes.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["dish","type","product","amount","unit","coef","note"])
        w.writerows(dishes)
    print(f"data/products.csv — {len(prod)} товаров")
    print(f"data/dishes.csv   — {len(dishes)} блюд")
    need = sum(1 for r in dishes if r[6] == "ПРОВЕРЬ")
    print(f"из них помечено ПРОВЕРЬ: {need}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/plans")
