"""СтопКонтроль — отметки о контроле на остановках города (Гродно). Flask + SQLite + MapLibre. Панель администратора удалена.
Запуск: python app.py  (или run.bat / python serve.py)."""
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from functools import wraps
from logging.handlers import RotatingFileHandler

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KINDS = ("present", "absent", "left")
DEFAULTS = {"window_sec": 15, "votes_needed": 3, "yellow_min": 15, "red_min": 60, "penalty": 0.1}
SCHEMA = """
CREATE TABLE IF NOT EXISTS cities(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    lat REAL NOT NULL, lng REAL NOT NULL, zoom REAL NOT NULL DEFAULT 15,
    south REAL, west REAL, north REAL, east REAL);
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    password_hash TEXT NOT NULL, rating REAL NOT NULL DEFAULT 10.0,
    city_id INTEGER REFERENCES cities(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stops(
    id INTEGER PRIMARY KEY AUTOINCREMENT, city_id INTEGER NOT NULL REFERENCES cities(id),
    name TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, osm_id TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'normal', status_since REAL, status_until REAL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_stops_city ON stops(city_id);
CREATE TABLE IF NOT EXISTS rounds(
    id INTEGER PRIMARY KEY AUTOINCREMENT, stop_id INTEGER NOT NULL REFERENCES stops(id) ON DELETE CASCADE,
    started_at REAL NOT NULL, ends_at REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
    result TEXT, penalized INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_rounds_stop ON rounds(stop_id, resolved);
CREATE TABLE IF NOT EXISTS votes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    stop_id INTEGER NOT NULL, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('present','absent','left')), created_at REAL NOT NULL,
    UNIQUE(round_id, user_id));
CREATE TABLE IF NOT EXISTS notifications(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT NOT NULL, is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def create_app(db_path=None, log=True, clock=None, auto_import=True):
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app.config["DATABASE"] = db_path or os.environ.get('DATABASE_PATH') or os.path.join(BASE_DIR, 'stopcontrol.db')
    app.config["CLOCK"] = clock or time.time
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    secret = os.environ.get("STOP_SECRET")
    if not secret:
        # Для локальной разработки создаём ключ автоматически.
        # На Render обязательно задайте STOP_SECRET в Environment.
        key_file = os.path.join(BASE_DIR, ".secret_key")
        if not os.path.exists(key_file):
            with open(key_file, "w", encoding="utf-8") as f:
                f.write(secrets.token_hex(32))
        with open(key_file, encoding="utf-8") as f:
            secret = f.read().strip()
    app.secret_key = secret
    if log:
        os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
        h = RotatingFileHandler(os.path.join(BASE_DIR, "logs", "stopcontrol.log"), maxBytes=500_000,
                                backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        app.logger.addHandler(h)
        app.logger.setLevel(logging.INFO)

    # ------------------------------------------------------------ БД
    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"], timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
        return g.db

    @app.teardown_appcontext
    def close_db(_e):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def init_db():
        db_path_local = app.config["DATABASE"]
        os.makedirs(os.path.dirname(os.path.abspath(db_path_local)), exist_ok=True)
        db = sqlite3.connect(db_path_local, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA)
        if db.execute("SELECT COUNT(*) FROM cities").fetchone()[0] == 0:
            db.execute("INSERT INTO cities(name,lat,lng,zoom,south,west,north,east) VALUES(?,?,?,?,?,?,?,?)",
                       ("Гродно", 53.6778, 23.8294, 14.5, 53.60, 23.72, 53.74, 23.95))
        stops_count = db.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
        db.commit()
        db.close()

        # Автоимпорт запускается только при полностью пустой таблице stops.
        # Ошибка внешнего сервиса не должна мешать запуску самого сайта.
        if stops_count == 0 and auto_import:
            try:
                import import_stops
                result = import_stops.run(db_path_local, force=False, log=app.logger.info)
                app.logger.info("Автоимпорт остановок завершён: добавлено %s, пропущено %s",
                                result.get("added", 0), result.get("skipped", 0))
            except Exception:
                app.logger.exception("Автоимпорт остановок не удался; приложение продолжит запуск")

    init_db()
    app.init_db = init_db

    def ts():
        return float(app.config["CLOCK"]())

    def now_iso():
        return datetime.fromtimestamp(ts()).isoformat(timespec="seconds")

    def cfg(key):
        r = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        default = DEFAULTS[key]
        if r is None:
            return default
        try:
            return float(r["value"]) if isinstance(default, float) else int(float(r["value"]))
        except ValueError:
            return default

    def get_setting(key, default=""):
        r = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(key, value):
        get_db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, str(value)))

    # ------------------------------------------------------------ сессия, защита
    @app.before_request
    def load_user():
        g.user = None
        if session.get("uid"):
            g.user = get_db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
            if g.user is None:
                session.pop("uid", None)

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                if request.path.startswith("/api/"):
                    return jsonify(error="Сессия устарела. Обновите страницу."), 400
                abort(400, "Недействительный токен формы. Обновите страницу и повторите действие.")

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        unread = 0
        if g.get("user"):
            unread = get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                                      (g.user["id"],)).fetchone()[0]
        return {"csrf_token": csrf_token, "current_user": g.get("user"), "unread": unread}

    def login_required(f):
        @wraps(f)
        def w(*a, **kw):
            if g.user is None:
                if request.path.startswith("/api/"):
                    return jsonify(error="Войдите в систему."), 401
                return redirect(url_for("register"))
            return f(*a, **kw)
        return w

    fails = {}

    def throttled(bucket, ip, limit, window):
        t = time.time()
        lst = [x for x in fails.get((bucket, ip), []) if t - x < window]
        fails[(bucket, ip)] = lst
        return len(lst) >= limit

    def add_fail(bucket, ip):
        fails.setdefault((bucket, ip), []).append(time.time())

    def notify(user_id, text):
        get_db().execute("INSERT INTO notifications(user_id,text,created_at) VALUES(?,?,?)",
                         (user_id, text, now_iso()))

    # ------------------------------------------------------------ вход и регистрация
    @app.route("/")
    def index():
        if g.user is None:
            return redirect(url_for("register"))
        if not g.user["city_id"]:
            return redirect(url_for("city"))
        city = get_db().execute("SELECT * FROM cities WHERE id=?", (g.user["city_id"],)).fetchone()
        if city is None:
            return redirect(url_for("city"))
        return render_template("map.html", city=dict(city), window=cfg("window_sec"), needed=cfg("votes_needed"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if g.user is not None and request.method == "GET":
            return redirect(url_for("index"))
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            pwd = request.form.get("password", "")
            errors = []
            if not 2 <= len(name) <= 30:
                errors.append("Имя: от 2 до 30 символов.")
            if not EMAIL_RE.match(email) or len(email) > 120:
                errors.append("Введите корректный адрес электронной почты.")
            if len(pwd) < 8:
                errors.append("Пароль: не менее 8 символов.")
            if not errors and get_db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                errors.append("Этот адрес уже зарегистрирован. Войдите в аккаунт.")
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("register.html", name=name, email=email), 400
            db = get_db()
            cur = db.execute("INSERT INTO users(email,name,password_hash,created_at) VALUES(?,?,?,?)",
                             (email, name, generate_password_hash(pwd), now_iso()))
            db.commit()
            session.clear()
            session["uid"] = cur.lastrowid
            return redirect(url_for("city"))
        return render_template("register.html", name="", email="")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            ip = request.remote_addr or "?"
            if throttled("login", ip, 5, 600):
                flash("Слишком много попыток входа. Подождите 10 минут.", "error")
                return render_template("login.html", email=email), 429
            u = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
                fails.pop(("login", ip), None)
                session.clear()
                session["uid"] = u["id"]
                return redirect(url_for("index"))
            add_fail("login", ip)
            flash("Неверный адрес почты или пароль.", "error")
            return render_template("login.html", email=email), 401
        return render_template("login.html", email="")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("register"))

    @app.route("/city", methods=["GET", "POST"])
    @login_required
    def city():
        db = get_db()
        if request.method == "POST":
            cid = request.form.get("city_id", type=int)
            if not db.execute("SELECT 1 FROM cities WHERE id=?", (cid,)).fetchone():
                abort(400, "Город не найден.")
            db.execute("UPDATE users SET city_id=? WHERE id=?", (cid, g.user["id"]))
            db.commit()
            return redirect(url_for("index"))
        cities = db.execute("SELECT c.*, (SELECT COUNT(*) FROM stops s WHERE s.city_id=c.id) AS n "
                            "FROM cities c ORDER BY name").fetchall()
        return render_template("city.html", cities=cities)

    @app.route("/profile")
    @login_required
    def profile():
        db = get_db()
        uid = g.user["id"]
        total = db.execute("SELECT COUNT(*) FROM votes WHERE user_id=?", (uid,)).fetchone()[0]
        ok = db.execute("SELECT COUNT(*) FROM votes v JOIN rounds r ON r.id=v.round_id "
                        "WHERE v.user_id=? AND r.resolved=1 AND r.result=v.kind", (uid,)).fetchone()[0]
        items = db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,)).fetchall()
        city = db.execute("SELECT name FROM cities WHERE id=?", (g.user["city_id"],)).fetchone()
        db.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (uid,))
        db.commit()
        return render_template("profile.html", total=total, confirmed=ok, items=items,
                               city_name=city["name"] if city else "не выбран")

    # ------------------------------------------------------------ логика голосования
    def effective(row, t):
        st, until = row["status"], row["status_until"]
        if st != "normal" and until is not None and until <= t:
            return "normal", None
        return st, until

    def round_counts(db, stop_id, t):
        ro = db.execute("SELECT * FROM rounds WHERE stop_id=? AND resolved=0 AND ends_at>? ORDER BY id DESC LIMIT 1",
                        (stop_id, t)).fetchone()
        if not ro:
            return None, None
        counts = {k: 0 for k in KINDS}
        for r in db.execute("SELECT kind, COUNT(*) c FROM votes WHERE round_id=? GROUP BY kind", (ro["id"],)):
            counts[r["kind"]] = r["c"]
        return ro, counts

    def stop_state(db, stop_id, user_id, t):
        s = db.execute("SELECT * FROM stops WHERE id=?", (stop_id,)).fetchone()
        if s is None:
            return None
        st, until = effective(s, t)
        ro, counts = round_counts(db, stop_id, t)
        mine = None
        if ro and user_id:
            v = db.execute("SELECT kind FROM votes WHERE round_id=? AND user_id=?", (ro["id"], user_id)).fetchone()
            mine = v["kind"] if v else None
        return {"id": s["id"], "name": s["name"], "lat": s["lat"], "lng": s["lng"], "status": st,
                "remaining": int(until - t) if until else None,
                "since": int(t - s["status_since"]) if s["status_since"] and st != "normal" else None,
                "round": ({"counts": counts, "ends_in": max(0, int(round(ro["ends_at"] - t))),
                           "needed": cfg("votes_needed"), "window": cfg("window_sec"), "mine": mine} if ro else None)}

    @app.route("/api/stops")
    @login_required
    def api_stops():
        db = get_db()
        t = ts()
        rows = db.execute("SELECT id,name,lat,lng,status,status_until FROM stops WHERE city_id=?",
                          (g.user["city_id"],)).fetchall()
        pend = {}
        for r in db.execute("SELECT ro.stop_id sid, ro.ends_at e, v.kind k, COUNT(*) c FROM rounds ro "
                            "JOIN votes v ON v.round_id=ro.id WHERE ro.resolved=0 AND ro.ends_at>? "
                            "GROUP BY ro.id, v.kind", (t,)):
            p = pend.setdefault(r["sid"], {"present": 0, "absent": 0, "left": 0, "ends_in": max(0, int(round(r["e"] - t)))})
            p[r["k"]] = r["c"]
        out = []
        for s in rows:
            st, until = effective(s, t)
            out.append({"id": s["id"], "name": s["name"], "lat": s["lat"], "lng": s["lng"], "status": st,
                        "remaining": int(until - t) if until else None, "pending": pend.get(s["id"])})
        return jsonify(stops=out, now=int(t))

    @app.route("/api/stops/<int:stop_id>")
    @login_required
    def api_stop(stop_id):
        st = stop_state(get_db(), stop_id, g.user["id"], ts())
        if st is None:
            return jsonify(error="Остановка не найдена."), 404
        return jsonify(st)

    def penalize(db, round_id, kind, stop_name):
        ro = db.execute("SELECT penalized FROM rounds WHERE id=?", (round_id,)).fetchone()
        if not ro or ro["penalized"]:
            return 0
        pen = cfg("penalty")
        n = 0
        for v in db.execute("SELECT user_id FROM votes WHERE round_id=? AND kind=?", (round_id, kind)).fetchall():
            db.execute("UPDATE users SET rating=MAX(0, ROUND(rating-?,1)) WHERE id=?", (pen, v["user_id"]))
            notify(v["user_id"], f"Ваша отметка по остановке «{stop_name}» не подтвердилась. "
                                 f"Уровень порядочности снижен на {pen:g}.")
            n += 1
        db.execute("UPDATE rounds SET penalized=1 WHERE id=?", (round_id,))
        return n

    def resolve(db, stop, round_id, kind, t):
        prev, _ = effective(stop, t)
        since = stop["status_since"] or 0
        if kind == "present":
            db.execute("UPDATE stops SET status='control', status_since=?, status_until=? WHERE id=?",
                       (t, t + cfg("red_min") * 60, stop["id"]))
        elif kind == "left":
            db.execute("UPDATE stops SET status='left', status_since=?, status_until=? WHERE id=?",
                       (t, t + cfg("yellow_min") * 60, stop["id"]))
        else:
            db.execute("UPDATE stops SET status='normal', status_since=?, status_until=NULL WHERE id=?",
                       (t, stop["id"]))
        db.execute("UPDATE rounds SET resolved=1, result=? WHERE id=?", (kind, round_id))
        # ложные отметки: тех, кто ввёл в заблуждение, штрафуем
        if kind == "absent" and prev == "control":
            last = db.execute("SELECT id FROM rounds WHERE stop_id=? AND resolved=1 AND result='present' AND id<>? "
                              "ORDER BY id DESC LIMIT 1", (stop["id"], round_id)).fetchone()
            if last:
                penalize(db, last["id"], "present", stop["name"])
        elif kind == "present" and prev == "left" and t - since < 120:
            last = db.execute("SELECT id FROM rounds WHERE stop_id=? AND resolved=1 AND result='left' AND id<>? "
                              "ORDER BY id DESC LIMIT 1", (stop["id"], round_id)).fetchone()
            if last:
                penalize(db, last["id"], "left", stop["name"])

    @app.route("/api/stops/<int:stop_id>/vote", methods=["POST"])
    @login_required
    def api_vote(stop_id):
        data = request.get_json(silent=True) or {}
        kind = data.get("kind")
        if kind not in KINDS:
            return jsonify(error="Некорректное действие."), 400
        if throttled("vote", g.user["id"], 30, 60):
            return jsonify(error="Слишком много отметок. Подождите минуту."), 429
        add_fail("vote", g.user["id"])
        db = get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            stop = db.execute("SELECT * FROM stops WHERE id=?", (stop_id,)).fetchone()
            if stop is None:
                db.rollback()
                return jsonify(error="Остановка не найдена."), 404
            t = ts()
            db.execute("UPDATE rounds SET resolved=2 WHERE stop_id=? AND resolved=0 AND ends_at<=?", (stop_id, t))
            ro = db.execute("SELECT * FROM rounds WHERE stop_id=? AND resolved=0 AND ends_at>? ORDER BY id DESC LIMIT 1",
                            (stop_id, t)).fetchone()
            if ro is None:
                cur = db.execute("INSERT INTO rounds(stop_id,started_at,ends_at) VALUES(?,?,?)",
                                 (stop_id, t, t + cfg("window_sec")))
                rid = cur.lastrowid
            else:
                rid = ro["id"]
            if db.execute("SELECT 1 FROM votes WHERE round_id=? AND user_id=?", (rid, g.user["id"])).fetchone():
                db.rollback()
                return jsonify(error="Вы уже отметились. Ждём подтверждения других.",
                               state=stop_state(db, stop_id, g.user["id"], t)), 409
            db.execute("INSERT INTO votes(round_id,stop_id,user_id,kind,created_at) VALUES(?,?,?,?,?)",
                       (rid, stop_id, g.user["id"], kind, t))
            n = db.execute("SELECT COUNT(*) FROM votes WHERE round_id=? AND kind=?", (rid, kind)).fetchone()[0]
            resolved = None
            if n >= cfg("votes_needed"):
                resolve(db, stop, rid, kind, t)
                resolved = kind
                app.logger.info("Остановка %s: подтверждено «%s»", stop_id, kind)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return jsonify(state=stop_state(db, stop_id, g.user["id"], ts()), resolved=resolved)

    # ------------------------------------------------------------ ошибки
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(429)
    def http_error(e):
        msgs = {400: "Некорректный запрос.", 403: "Недостаточно прав.", 404: "Страница не найдена.",
                429: "Слишком много запросов. Повторите позже."}
        text = getattr(e, "description", None) if e.code == 400 else None
        return render_template("error.html", code=e.code, message=text or msgs.get(e.code, "Ошибка")), e.code

    @app.errorhandler(500)
    def server_error(_e):
        app.logger.exception("Внутренняя ошибка")
        return render_template("error.html", code=500, message="Внутренняя ошибка сервера."), 500

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
