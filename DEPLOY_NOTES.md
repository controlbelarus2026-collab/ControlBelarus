# Развёртывание на Render

Build Command: `pip install -r requirements.txt`
Start Command: `gunicorn app:create_app()`

В Render → Environment добавьте `STOP_SECRET` со случайной длинной строкой. Если подключён Persistent Disk с Mount Path `/var/data`, добавьте `DATABASE_PATH=/var/data/stopcontrol.db`.

Панельные шаблоны и маршруты удалены. `static/map.js` уже запрашивает `/api/stops` при загрузке карты; `init_db()` запускает `import_stops.py`, если таблица `stops` пуста. Импорт использует Overpass API, поэтому при недоступности внешних серверов сайт всё равно запускается, но остановки не появятся до успешного повторного импорта.
