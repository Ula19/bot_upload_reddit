# bot_4_reddit

Telegram-бот для скачивания медиа с Reddit.

## Что умеет

- Видео Reddit (hosted) с аудиодорожкой — автоматический DASH merge через yt-dlp
- Фото (`i.redd.it`, `preview.redd.it`)
- GIF — конвертирует в mp4 для корректного воспроизведения в Telegram
- Галереи (`reddit.com/gallery/...`) — отправляет пачками по 10 медиа
- Кросспосты — достаёт оригинал рекурсивно
- Внешние хостинги (imgur, gfycat, redgifs, streamable) через yt-dlp
- Выбор качества видео (240p / 360p / 480p / 720p / 1080p)
- Кэш `file_id` — повторный запрос той же ссылки отдаёт из БД без скачивания
- Мультиязычность: ru / uz / en
- Обязательная подписка на каналы (настраивается через `/admin`)

## Стек

- Python 3.12 + Aiogram 3
- PostgreSQL + SQLAlchemy (asyncpg)
- yt-dlp (основной движок видео) + ручной HTTP + ffmpeg как fallback
- Reddit JSON API для галерей и метаданных
- Local Bot API (файлы до 2 ГБ)
- Cloudflare WARP (SOCKS5) — обход блокировки Reddit на датацентровых IP

## Fallback-цепочка

При каждом запросе источники перебираются по порядку:

1. **Direct** (без прокси)
2. **Резидентный SOCKS5** (`PROXY_URL` из `.env`)
3. **WARP** (контейнер `warp`, Cloudflare)

## Быстрый старт

```bash
cp .env.example .env
# заполнить .env (BOT_TOKEN, API_ID, API_HASH, DB_PASSWORD, ADMIN_IDS)
docker compose up -d --build
```

Локально без WARP (если WARP конфликтует с Tailscale/VPN):

```bash
docker compose up -d --build --no-deps bot postgres bot-api
```

## Структура

```
bot/
  main.py              — точка входа, сборка диспетчера
  config.py            — настройки из .env
  i18n.py              — переводы ru/uz/en
  emojis.py            — кастомные эмодзи
  handlers/
    start.py           — /start, меню, профиль, язык, подписка
    admin.py           — /admin: статистика, каналы, рассылка
    download.py        — приём ссылок, выбор качества, отправка медиа
  services/
    reddit.py          — yt-dlp + Reddit JSON API + ffmpeg merge
  middlewares/
    subscription.py    — проверка подписки на каналы
    rate_limit.py      — 5 запросов/мин
  keyboards/
    inline.py          — клавиатуры юзера
    admin.py           — клавиатуры админа
  database/
    models.py          — User, Channel, DownloadCache
    crud.py            — CRUD функции
  utils/
    commands.py        — меню команд Telegram
    helpers.py         — is_reddit_url() и другие утилиты
    video_meta.py      — ffprobe (width/height/duration)
    docker.py          — рестарт WARP при блокировках
```

## Переменные окружения

| Переменная | Описание |
|-----------|----------|
| `BOT_TOKEN` | Токен бота от @BotFather |
| `API_ID` / `API_HASH` | Для Local Bot API (my.telegram.org) |
| `BOT_USERNAME` | Юзернейм бота без `@` — для подписи к медиа |
| `ADMIN_IDS` | ID администраторов через запятую |
| `ADMIN_USERNAME` | Юзернейм админа для связи |
| `PROXY_URL` | Резидентный SOCKS5 (опционально) |
| `DB_*` | Параметры PostgreSQL |
| `CACHE_TTL_DAYS` | TTL кэша `file_id` в БД (дни) |
| `MAX_QUALITY_SIZE_MB` | Максимальный размер качества в префлайте (МБ) |

## Поддерживаемые URL

- `reddit.com/r/<sub>/comments/<id>/...`
- `reddit.com/gallery/<id>`
- `redd.it/<id>` (short link)
- `reddit.com/r/<sub>/s/<id>` (share link — через yt-dlp)
- Кросспосты
- Внешние: `imgur.com`, `gfycat.com`, `redgifs.com`, `streamable.com`
