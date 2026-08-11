"""Разбирает файл плана и кладёт в базу. python import_plan.py план.docx"""
import sys, json
from app.parser.rules import parse_file
from app.db import init

def save(path):
    res = parse_file(path)
    con = init()
    con.execute("UPDATE plans SET active=0")
    cur = con.execute(
        "INSERT INTO plans(title,source_file,raw_json) VALUES(?,?,?)",
        (path.split("/")[-1], path, json.dumps(res, ensure_ascii=False)))
    pid = cur.lastrowid
    for di, d in enumerate(res["days"]):
        for mi, m in enumerate(d["meals"]):
            for it in m["items"]:
                con.execute(
                    "INSERT INTO plan_items(plan_id,day_index,day_name,meal,meal_index,"
                    "optional,name,qty_min,qty_max,unit,note,url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, di, d["day"], m["meal"], mi, int(m["optional"]), it["name"],
                     it["qty_min"], it["qty_max"], it["unit"],
                     " ".join(m["notes"])[:500] or None, (m["links"] or [None])[0]))
            if m["recipe"]:
                con.execute("INSERT INTO recipes(plan_id,dish,title,ingredients_json,steps_json)"
                            " VALUES(?,?,?,?,?)",
                            (pid, m["items"][0]["name"] if m["items"] else "", m["recipe"]["title"],
                             json.dumps(m["recipe"]["ingredients"], ensure_ascii=False),
                             json.dumps(m["recipe"]["steps"], ensure_ascii=False)))
            for t in m["prep"]:
                con.execute("INSERT INTO prep_tasks(plan_id,day_index,meal,text) VALUES(?,?,?,?)",
                            (pid, di, m["meal"], t))
    con.commit()
    s = res["stats"]
    print(f"План #{pid}: {s['days']} дней, {s['items']} позиций, "
          f"{s['recipes']} рецептов, {s['prep']} подготовок")
    print("Одинаковые дни:", res["duplicates"] or "нет")
    unknown = con.execute(
        "SELECT DISTINCT p.name FROM plan_items p LEFT JOIN dishes d ON d.dish=p.name"
        " WHERE p.plan_id=? AND d.id IS NULL", (pid,)).fetchall()
    if unknown:
        print(f"\nНет в справочнике ({len(unknown)}) — добавь в data/dishes.csv:")
        for r in unknown: print("   ", r["name"])
    return pid

if __name__ == "__main__":
    save(sys.argv[1])
