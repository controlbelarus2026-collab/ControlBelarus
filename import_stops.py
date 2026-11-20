"""Загрузка ВСЕХ остановок города из OpenStreetMap (нужен Интернет).
Запуск: python import_stops.py            — для всех городов, где ещё нет остановок
        python import_stops.py --force    — докачать новые остановки и в уже заполненные города
Данные © участники OpenStreetMap (ODbL)."""
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime

BASE = os.path.abspath(os.path.dirname(__file__))
DB = os.path.join(BASE, "stopcontrol.db")
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
             "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]
UA = "StopControl/1.0 (student project; stop import)"


def build_query(south, west, north, east):
    bb = f"({south},{west},{north},{east})"
    return ("[out:json][timeout:90];(" + f'nwr["highway"="bus_stop"]{bb};' +
            f'nwr["public_transport"="platform"]["bus"="yes"]{bb};' +
            f'nwr["public_transport"="platform"]["trolleybus"="yes"]{bb};' + ");out center tags;")


def parse(data):
    """Ответ Overpass -> список остановок. Остановки без названия пропускаются."""
    out, seen = [], set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = (tags.get("name:ru") or tags.get("name") or tags.get("name:be") or "").strip()
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lng = el.get("lon", (el.get("center") or {}).get("lon"))
        if not name or lat is None or lng is None:
            continue
        oid = f"{el.get('type', 'n')[0]}{el.get('id')}"
        if oid in seen:
            continue
        seen.add(oid)
        out.append({"osm_id": oid, "name": name[:120], "lat": float(lat), "lng": float(lng)})
    return out


def fetch(query):
    last = None
    for url in ENDPOINTS:
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"Overpass недоступен: {last}")


def run(db_path=DB, city_id=None, fetch_fn=None, log=print, force=True):
    fetch_fn = fetch_fn or fetch
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cities = db.execute("SELECT * FROM cities" + (" WHERE id=?" if city_id else ""),
                        (city_id,) if city_id else ()).fetchall()
    added = skipped = 0
    for c in cities:
        have = db.execute("SELECT COUNT(*) FROM stops WHERE city_id=?", (c["id"],)).fetchone()[0]
        if have and not force:
            continue
        if None in (c["south"], c["west"], c["north"], c["east"]):
            log(f"{c['name']}: не заданы границы города для импорта")
            continue
        stops = parse(fetch_fn(build_query(c["south"], c["west"], c["north"], c["east"])))
        for s in stops:
            cur = db.execute("INSERT OR IGNORE INTO stops(city_id,name,lat,lng,osm_id,created_at) VALUES(?,?,?,?,?,?)",
                             (c["id"], s["name"], s["lat"], s["lng"], s["osm_id"], datetime.now().isoformat(timespec="seconds")))
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
        db.commit()
        log(f"{c['name']}: найдено {len(stops)}, добавлено новых {added}")
    db.close()
    return {"added": added, "skipped": skipped}


if __name__ == "__main__":
    if not os.path.exists(DB):
        print("Сначала запустите приложение один раз, чтобы создалась база.")
        sys.exit(2)
    try:
        res = run(force="--force" in sys.argv or True)
    except Exception as e:  # noqa: BLE001
        print("Не удалось загрузить остановки:", e)
        sys.exit(1)
    print("Готово:", res)
