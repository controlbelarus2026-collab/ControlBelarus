# СтопКонтроль

Flask + SQLite + MapLibre. Пользователь регистрируется, выбирает город и отмечает состояние остановок. Карта загружает остановки через `/api/stops` автоматически.

## Локальный запуск (Windows)

```powershell
py -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

## Render

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:create_app()`
- Environment: задайте `STOP_SECRET` как длинную случайную строку.
- Если подключён Persistent Disk с Mount Path `/var/data`, задайте `DATABASE_PATH=/var/data/stopcontrol.db`.

При первом старте `init_db()` создаёт таблицы и, если таблица `stops` пуста, запускает `import_stops.py` для импорта остановок из OpenStreetMap. Импорт зависит от доступности серверов Overpass и может занять некоторое время. Если импорт не удался, приложение всё равно запустится; после исправления причины можно перезапустить сервис.

## Структура проекта

```text
app.py
import_stops.py
requirements.txt
static/
  style.css
  map.js
  manifest.json
  img/
templates/
  base.html
  map.html
  login.html
  register.html
  city.html
  profile.html
  error.html
```

Панель администратора и связанные маршруты удалены. Не добавляйте `.secret_key`, `.env`, `venv/`, `logs/` или локальную SQLite-базу в публичный репозиторий.

Данные карты остановок получены из OpenStreetMap; © участники OpenStreetMap (ODbL).
