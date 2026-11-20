"""Тесты СтопКонтроль. Запуск: python -m unittest tests -v"""
import glob
import os
import re
import sqlite3
import tempfile
import unittest

from app import create_app


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class StopTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.clock = Clock()
        self.app = create_app(self.path, log=False, clock=self.clock, auto_import=False)
        self.app.config["TESTING"] = True
        db = sqlite3.connect(self.path)
        db.execute("INSERT INTO stops(city_id,name,lat,lng,created_at) VALUES(1,'Ратуша',53.678,23.829,'x')")
        db.execute("INSERT INTO stops(city_id,name,lat,lng,created_at) VALUES(1,'Вокзал',53.68,23.83,'x')")
        db.commit(); db.close()
        self.users = []

    def tearDown(self):
        os.remove(self.path)

    # ---- помощники
    def csrf(self, c):
        with c.session_transaction() as s:
            s["csrf"] = "tok"

    def user(self, n=None):
        n = len(self.users) + 1 if n is None else n
        c = self.app.test_client()
        self.csrf(c)
        r = c.post("/register", data={"csrf": "tok", "name": f"Пассажир{n}", "email": f"u{n}@t.by", "password": "12345678"})
        self.assertEqual(r.status_code, 302)
        self.csrf(c)
        c.post("/city", data={"csrf": "tok", "city_id": 1})
        self.csrf(c)
        self.users.append(c)
        return c

    def vote(self, c, stop=1, kind="present"):
        self.csrf(c)
        return c.post(f"/api/stops/{stop}/vote", json={"kind": kind}, headers={"X-CSRF-Token": "tok"})

    def stop_status(self, stop=1):
        c = self.users[0]
        return c.get(f"/api/stops/{stop}").get_json()

    def rating(self, uid):
        db = sqlite3.connect(self.path)
        r = db.execute("SELECT rating FROM users WHERE id=?", (uid,)).fetchone()[0]
        db.close()
        return r

    # ---- вход, регистрация, город
    def test_first_screen_is_registration(self):
        c = self.app.test_client()
        r = c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/register"))
        self.assertIn("Зарегистрироваться", c.get("/register").get_data(as_text=True))

    def test_register_then_city_then_map(self):
        c = self.app.test_client()
        self.csrf(c)
        r = c.post("/register", data={"csrf": "tok", "name": "Аня", "email": "a@a.by", "password": "12345678"})
        self.assertTrue(r.headers["Location"].endswith("/city"))
        self.assertTrue(c.get("/").headers["Location"].endswith("/city"))     # без города на карту не пускает
        self.assertIn("Гродно", c.get("/city").get_data(as_text=True))
        self.csrf(c)
        c.post("/city", data={"csrf": "tok", "city_id": 1})
        html = c.get("/").get_data(as_text=True)
        self.assertIn('id="map"', html)
        self.assertIn('id="sheet"', html)

    def test_register_validation_and_duplicate(self):
        c = self.app.test_client(); self.csrf(c)
        r = c.post("/register", data={"csrf": "tok", "name": "А", "email": "плохой", "password": "1"})
        self.assertEqual(r.status_code, 400)
        t = r.get_data(as_text=True)
        self.assertIn("Имя", t); self.assertIn("корректный адрес", t); self.assertIn("Пароль", t)
        self.user(1)
        c2 = self.app.test_client(); self.csrf(c2)
        r = c2.post("/register", data={"csrf": "tok", "name": "Двойник", "email": "u1@t.by", "password": "12345678"})
        self.assertIn("уже зарегистрирован", r.get_data(as_text=True))

    def test_login_throttle_and_csrf(self):
        self.user(1)
        c = self.app.test_client(); self.csrf(c)
        for _ in range(5):
            c.post("/login", data={"csrf": "tok", "email": "u1@t.by", "password": "bad"})
        self.assertEqual(c.post("/login", data={"csrf": "tok", "email": "u1@t.by", "password": "12345678"}).status_code, 429)
        self.assertEqual(self.app.test_client().post("/login", data={"email": "a", "password": "b"}).status_code, 400)

    def test_api_requires_login(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/stops").status_code, 401)
        self.assertEqual(c.get("/api/stops/1").status_code, 401)

    # ---- голосование
    def test_three_present_votes_turn_stop_red(self):
        us = [self.user() for _ in range(3)]
        self.assertEqual(self.vote(us[0]).get_json()["resolved"], None)
        self.assertEqual(self.stop_status()["status"], "normal")
        self.assertEqual(self.stop_status()["round"]["counts"]["present"], 1)
        self.clock.advance(5)
        self.vote(us[1])
        self.assertEqual(self.stop_status()["status"], "normal")
        self.clock.advance(5)
        j = self.vote(us[2]).get_json()
        self.assertEqual(j["resolved"], "present")
        self.assertEqual(self.stop_status()["status"], "control")
        stops = {s["id"]: s for s in us[0].get("/api/stops").get_json()["stops"]}
        self.assertEqual(stops[1]["status"], "control")
        self.assertEqual(stops[2]["status"], "normal")

    def test_window_expires_without_changes(self):
        us = [self.user() for _ in range(3)]
        self.vote(us[0]); self.vote(us[1])
        self.clock.advance(16)                       # окно 15 с истекло
        self.vote(us[2])                             # это уже новый раунд, 1 голос из 3
        st = self.stop_status()
        self.assertEqual(st["status"], "normal")
        self.assertEqual(st["round"]["counts"]["present"], 1)

    def test_one_vote_per_user_per_round(self):
        u = self.user()
        self.assertEqual(self.vote(u).status_code, 200)
        r = self.vote(u)
        self.assertEqual(r.status_code, 409)
        self.assertIn("уже отметились", r.get_json()["error"])

    def test_invalid_kind_and_unknown_stop(self):
        u = self.user()
        self.assertEqual(self.vote(u, kind="hack").status_code, 400)
        self.assertEqual(self.vote(u, stop=999).status_code, 404)

    def red_stop(self, stop=1):
        us = [self.user() for _ in range(3)]
        for u in us:
            self.vote(u, stop, "present")
        self.clock.advance(20)
        return us

    def test_left_makes_yellow_for_15_minutes_then_normal(self):
        self.red_stop()
        us = [self.user() for _ in range(3)]
        for u in us:
            self.vote(u, 1, "left")
        self.assertEqual(self.stop_status()["status"], "left")
        self.clock.advance(14 * 60)
        self.assertEqual(self.stop_status()["status"], "left")
        self.clock.advance(61)
        self.assertEqual(self.stop_status()["status"], "normal")

    def test_absent_overturns_control_and_penalizes_reporters(self):
        reporters = self.red_stop()                   # пользователи 1-3 пометили контроль
        self.assertEqual(self.stop_status()["status"], "control")
        self.clock.advance(30)
        deniers = [self.user() for _ in range(3)]
        for u in deniers:
            self.vote(u, 1, "absent")
        self.assertEqual(self.stop_status()["status"], "normal")
        for uid in (1, 2, 3):
            self.assertEqual(self.rating(uid), 9.9)   # -0.1 за введение в заблуждение
        for uid in (4, 5, 6):
            self.assertEqual(self.rating(uid), 10.0)
        notes = reporters[0].get("/profile").get_data(as_text=True)
        self.assertIn("не подтвердилась", notes)

    def test_penalty_applied_once_and_floor_zero(self):
        self.red_stop()
        db = sqlite3.connect(self.path); db.execute("UPDATE users SET rating=0.05 WHERE id=1"); db.commit(); db.close()
        for u in [self.user() for _ in range(3)]:
            self.vote(u, 1, "absent")
        self.assertEqual(self.rating(1), 0.0)
        self.assertEqual(self.rating(2), 9.9)

    def test_control_after_quick_left_penalizes_left_reporters(self):
        self.red_stop()
        lefts = [self.user() for _ in range(3)]
        for u in lefts:
            self.vote(u, 1, "left")                    # объявили «уехал»
        self.clock.advance(30)                         # …но контроль снова на месте
        for u in [self.user() for _ in range(3)]:
            self.vote(u, 1, "present")
        self.assertEqual(self.stop_status()["status"], "control")
        for uid in (4, 5, 6):
            self.assertEqual(self.rating(uid), 9.9)

    def test_red_expires_automatically(self):
        self.red_stop()
        self.clock.advance(61 * 60)
        self.assertEqual(self.stop_status()["status"], "normal")

    def test_api_stops_lists_pending_round(self):
        us = [self.user() for _ in range(2)]
        self.vote(us[0], 2, "left"); self.vote(us[1], 2, "left")
        s = {x["id"]: x for x in us[0].get("/api/stops").get_json()["stops"]}
        self.assertEqual(s[2]["pending"]["left"], 2)
        self.assertEqual(s[1]["pending"], None)

    def test_vote_rate_limit(self):
        u = self.user()
        db = sqlite3.connect(self.path)
        for i in range(40):
            db.execute("INSERT INTO stops(city_id,name,lat,lng,created_at) VALUES(1,?,53.6,23.8,'x')", (f"С{i}",))
        db.commit(); db.close()
        codes = [self.vote(u, 3 + i, "absent").status_code for i in range(32)]
        self.assertEqual(codes[-1], 429)

    def test_admin_panel_routes_are_removed(self):
        c = self.app.test_client()
        for path in ("/password/anything", "/panel", "/panel/settings", "/panel/site",
                     "/panel/import/1", "/panel/cities", "/panel/stops", "/panel/users"):
            self.assertEqual(c.get(path).status_code, 404, path)

    # ---- импорт остановок
    def test_import_parse_and_run(self):
        import import_stops as imp
        data = {"elements": [
            {"type": "node", "id": 1, "lat": 53.67, "lon": 23.82, "tags": {"name": "Парк", "highway": "bus_stop"}},
            {"type": "node", "id": 2, "lat": 53.68, "lon": 23.83, "tags": {"highway": "bus_stop"}},            # без названия
            {"type": "way", "id": 3, "center": {"lat": 53.69, "lon": 23.84}, "tags": {"name:ru": "Площадь"}},
            {"type": "node", "id": 1, "lat": 53.67, "lon": 23.82, "tags": {"name": "Парк"}}]}                 # дубль
        parsed = imp.parse(data)
        self.assertEqual([s["name"] for s in parsed], ["Парк", "Площадь"])
        self.assertEqual(parsed[1]["osm_id"], "w3")
        q = imp.build_query(53.6, 23.72, 53.74, 23.95)
        self.assertIn('"highway"="bus_stop"', q); self.assertIn("53.6,23.72,53.74,23.95", q)
        res = imp.run(self.path, 1, fetch_fn=lambda q: data, log=lambda *_: None)
        self.assertEqual(res["added"], 2)
        res = imp.run(self.path, 1, fetch_fn=lambda q: data, log=lambda *_: None)
        self.assertEqual(res["added"], 0)                                    # повторный импорт не дублирует

    # ---- интерфейс
    def test_mobile_markup_and_pwa(self):
        c = self.user()
        html = c.get("/").get_data(as_text=True)
        self.assertIn("viewport-fit=cover", html)
        self.assertIn("manifest.json", html)
        for kind in ("present", "absent", "left"):
            self.assertIn(f'data-kind="{kind}"', html)
        self.assertNotIn('type="search"', html)                              # поиск убран
        self.assertEqual(c.get("/static/manifest.json").status_code, 200)

    def test_map_client_has_yellow_roads_and_labels(self):
        js = self.users[0].get("/static/map.js").get_data(as_text=True) if self.users else \
            self.app.test_client().get("/static/map.js").get_data(as_text=True)
        for needle in ("openfreemap", "#ffd84d", "labels-on", "/api/stops", "Контроль стоит"):
            self.assertIn(needle, js)

    def test_no_emoji_in_interface(self):
        rx = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u26A0]")
        base = os.path.dirname(os.path.abspath(__file__))
        for f in glob.glob(base + "/templates/*.html") + glob.glob(base + "/static/*.js") + glob.glob(base + "/static/*.css"):
            self.assertIsNone(rx.search(open(f, encoding="utf-8").read()), f)


if __name__ == "__main__":
    unittest.main()
