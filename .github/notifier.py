#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notifier.py — бесплатный монитор нового контента с уведомлениями в Telegram.

Что проверяет:
  * YouTube  — новые видео по ID канала (официальная RSS-лента, без API-ключа и квот);
  * Twitch   — начало стрима (Helix API, эндпоинт Get Streams);
  * RSS/Atom — новые посты блогов и новости игр (включая новости Steam).

Файлы рядом со скриптом:
  config.json  — что проверять (образец: config.example.json);
  seen.json    — что уже отправлено (создаётся сам, руками не редактировать);
  .env         — секреты: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                 TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET.

Команды:
  python notifier.py                  # одна проверка (так работают cron и GitHub Actions)
  python notifier.py --loop           # проверять каждые N минут (локально, пока не нажмёшь Ctrl+C)
  python notifier.py --dry-run        # показать сообщения, ничего не отправляя и не запоминая
  python notifier.py --test-telegram  # отправить одно тестовое сообщение и выйти
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import feedparser
    import requests
except ImportError as exc:  # понятное сообщение вместо голого traceback
    print(f"Не хватает библиотеки: {exc.name}")
    print("Установи зависимости командой:  pip install -r requirements.txt")
    raise SystemExit(2)

# ---------------------------------------------------------------------------
# Константы и пути
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen.json"
ENV_PATH = BASE_DIR / ".env"

STATE_VERSION = 1
MAX_SEEN_PER_SOURCE = 300        # сколько последних ID помнить для каждого источника
MAX_ITEMS_PER_FEED = 50          # больше записей из одной ленты за раз не читаем
TELEGRAM_TEXT_LIMIT = 4000       # у Telegram лимит 4096 символов на сообщение

YOUTUBE_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_STREAMS_URL = "https://api.twitch.tv/helix/streams"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ContentNotifier/1.0"
)
FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
    "text/xml;q=0.8, */*;q=0.5"
)

KIND_LABELS = {
    "youtube": ("🎬", "Новое видео"),
    "twitch": ("🔴", "Начался стрим"),
    "twitch_end": ("⚫", "Стрим закончился"),
    "rss": ("📰", "Новая запись"),
}

# Кнопки под приветственным сообщением бота (callback_data — то, что придёт нам)
MAIN_MENU: dict[str, Any] = {
    "inline_keyboard": [
        [
            {"text": "📋 Источники", "callback_data": "sources"},
            {"text": "🟢 Кто в эфире", "callback_data": "online"},
        ],
        [
            {"text": "➕ Добавить", "callback_data": "add"},
        ],
    ]
}

MENU_TEXT = (
    "👋 Привет! Я слежу за новым контентом и присылаю уведомления в этот чат.\n\n"
    "Что показать?"
)

ADD_TEXT = (
    "➕ Пришли ссылку на источник одним сообщением — я проверю и добавлю:\n\n"
    "• YouTube: https://youtube.com/@handle\n"
    "• Twitch: https://twitch.tv/ник\n"
    "• RSS: https://example.com/rss.xml\n\n"
    "Для YouTube можно просто прислать @handle."
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "interval_minutes": 10,      # как часто проверять в режиме --loop
    "max_messages_per_run": 20,  # защита от «залпа» сообщений
    "first_run_silent": True,    # при первом запуске не присылать старые записи
    "max_age_days": 7,           # не присылать записи старше N дней (0 = без ограничения)
    "request_timeout": 20,       # таймаут сетевых запросов, секунды
    "notify_stream_end": True,   # присылать «стрим закончился», когда стример ушёл в офлайн
}

log = logging.getLogger("notifier")


# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Проблема в config.json или .env."""


class SourceError(RuntimeError):
    """Один источник не удалось проверить (не роняет остальные)."""


# ---------------------------------------------------------------------------
# Мелочи: логирование, .env, JSON, текст, даты
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_dotenv(path: Path) -> None:
    """Простой заменитель python-dotenv: читает KEY=VALUE из .env."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("Не удалось прочитать .env: %s", exc)
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def clean_text(value: Any, limit: int = 300) -> str:
    """Убирает HTML-теги, лишние пробелы и обрезает слишком длинный заголовок."""
    text = TAG_RE.sub(" ", str(value or ""))
    text = html.unescape(text)
    text = SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def entry_datetime(entry: Any) -> datetime | None:
    """Дата публикации записи RSS/Atom в UTC (или None, если её нет)."""
    for attribute in ("published_parsed", "updated_parsed"):
        value = entry.get(attribute)
        if not value:
            continue
        try:
            return datetime(*value[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def normalize_channel_id(value: Any) -> str:
    """Принимает ID канала (UC...) или ссылку вида .../channel/UC... ."""
    text = str(value or "").strip()
    if "channel/" in text:
        text = text.split("channel/", 1)[1].split("/")[0].split("?")[0]
    if not text or text.startswith("@") or not text.startswith("UC"):
        return ""
    return text


def normalize_login(value: Any) -> str:
    """Принимает ник стримера или ссылку https://www.twitch.tv/ник ."""
    text = str(value or "").strip().lower()
    if "twitch.tv/" in text:
        text = text.split("twitch.tv/", 1)[1].split("/")[0].split("?")[0]
    return text.strip()


# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Не найден файл {path.name}. Скопируй config.example.json в config.json "
            "и впиши свои источники."
        )
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Не удалось прочитать {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} должен содержать JSON-объект {{ ... }}")

    settings = dict(DEFAULT_SETTINGS)
    user_settings = data.get("settings") or {}
    if not isinstance(user_settings, dict):
        raise ConfigError("settings в config.json должен быть объектом { ... }")
    settings.update(user_settings)
    for key in ("interval_minutes", "max_messages_per_run", "max_age_days", "request_timeout"):
        try:
            settings[key] = int(settings[key])
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"settings.{key} должен быть целым числом") from exc
    settings["max_messages_per_run"] = max(1, settings["max_messages_per_run"])
    settings["interval_minutes"] = max(1, settings["interval_minutes"])
    settings["request_timeout"] = max(5, settings["request_timeout"])

    for key in ("first_run_silent", "notify_stream_end"):
        value = settings.get(key, True)
        if isinstance(value, str):
            value = value.strip().lower() not in ("", "0", "false", "no", "off")
        settings[key] = bool(value)
    data["settings"] = settings

    if not isinstance(data.get("telegram"), dict):
        data["telegram"] = {}
    for section in ("youtube", "twitch", "rss"):
        value = data.get(section) or []
        if not isinstance(value, list):
            raise ConfigError(f'Раздел "{section}" в config.json должен быть списком [ ... ]')
        data[section] = [item for item in value if isinstance(item, dict)]
    return data


# ---------------------------------------------------------------------------
# Память об отправленном: seen.json
# ---------------------------------------------------------------------------

class SeenStore:
    """Хранит, что уже отправлено, и кто из стримеров сейчас в эфире.

    data["sources"] — {источник: [id, ...]} (последние 300 на источник);
    data["live"]    — {"twitch:login": {"stream_id": ..., "game": ..., "announced": bool}}
                      нужно, чтобы отличить новый стрим от того же самого.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {
            "version": STATE_VERSION,
            "sources": {},
            "live": {},
            "telegram_offset": 0,
        }
        self._dirty = False
        if not path.exists():
            return
        try:
            raw = read_json(path)
            if not isinstance(raw, dict) or not isinstance(raw.get("sources"), dict):
                raise ValueError("неожиданная структура файла")
            self.data["sources"] = {
                str(key): [str(item) for item in value]
                for key, value in raw["sources"].items()
                if isinstance(value, list)
            }
            live = raw.get("live")
            if isinstance(live, dict):
                self.data["live"] = {
                    str(key): dict(value) for key, value in live.items() if isinstance(value, dict)
                }
            try:
                self.data["telegram_offset"] = int(raw.get("telegram_offset") or 0)
            except (TypeError, ValueError):
                self.data["telegram_offset"] = 0
        except (OSError, ValueError) as exc:
            backup = path.with_name(path.name + ".broken")
            try:
                path.replace(backup)
                hint = f"копия сохранена в {backup.name}"
            except OSError:
                hint = "копию сделать не удалось"
            log.warning("seen.json повреждён (%s), начинаю с чистого листа; %s", exc, hint)

    def has_source(self, key: str) -> bool:
        return key in self.data["sources"]

    def ensure(self, key: str) -> None:
        if key not in self.data["sources"]:
            self.data["sources"][key] = []
            self._dirty = True

    def is_new(self, key: str, item_id: str) -> bool:
        return item_id not in self.data["sources"].get(key, [])

    def mark(self, key: str, item_id: str) -> None:
        ids = self.data["sources"].setdefault(key, [])
        if item_id in ids:
            return
        ids.insert(0, item_id)
        del ids[MAX_SEEN_PER_SOURCE:]
        self._dirty = True

    # --- состояние Twitch -------------------------------------------------

    def get_live(self, key: str) -> dict[str, Any] | None:
        return self.data["live"].get(key)

    def set_live(self, key: str, info: dict[str, Any]) -> None:
        self.data["live"][key] = dict(info)
        self._dirty = True

    def clear_live(self, key: str) -> None:
        if key in self.data["live"]:
            del self.data["live"][key]
            self._dirty = True

    # --- позиция в очереди обновлений Telegram ----------------------------

    def get_offset(self) -> int:
        try:
            return int(self.data.get("telegram_offset") or 0)
        except (TypeError, ValueError):
            return 0

    def set_offset(self, value: int) -> None:
        value = int(value)
        if self.data.get("telegram_offset") != value:
            self.data["telegram_offset"] = value
            self._dirty = True

    def save(self, force: bool = False) -> None:
        if not (self._dirty or force):
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)  # атомарная замена: файл не побьётся при сбое
        self._dirty = False


# ---------------------------------------------------------------------------
# Загрузка лент (YouTube и RSS)
# ---------------------------------------------------------------------------

def fetch_feed(url: str, timeout: int) -> Any:
    """Скачивает и разбирает RSS/Atom. Ошибки превращает в SourceError."""
    socket.setdefaulttimeout(timeout)  # feedparser не принимает timeout напрямую
    try:
        feed = feedparser.parse(
            url,
            agent=USER_AGENT,
            request_headers={"Accept": FEED_ACCEPT, "Accept-Language": "ru,en;q=0.8"},
        )
    except Exception as exc:  # feedparser умеет падать разными способами
        raise SourceError(f"не удалось загрузить ленту: {exc}") from exc

    status = feed.get("status")
    if isinstance(status, int) and status >= 400:
        raise SourceError(f"лента ответила HTTP {status} — проверь адрес")
    if not feed.entries:
        if feed.get("bozo"):
            raise SourceError(f"ленту не удалось разобрать: {feed.get('bozo_exception')}")
        raise SourceError("в ленте нет записей (возможно, изменился формат)")
    return feed


def fetch_youtube(source: dict[str, Any], timeout: int) -> list[dict[str, Any]]:
    channel_id = normalize_channel_id(source.get("channel_id"))
    if not channel_id:
        raise SourceError(
            "не указан корректный channel_id: нужен ID вида UCxxxxxxxxxxxxxxxxxxxxxx "
            "(не @имя и не ссылка на видео)"
        )
    feed = fetch_feed(YOUTUBE_RSS_URL.format(channel_id=channel_id), timeout)

    items: list[dict[str, Any]] = []
    for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
        video_id = str(entry.get("yt_videoid") or "").strip()
        link = str(entry.get("link") or "").strip()
        if not video_id and "v=" in link:
            video_id = link.split("v=", 1)[1].split("&")[0]
        if not video_id:
            video_id = "sha1:" + short_hash(link or str(entry.get("title") or ""))
        if not link:
            link = f"https://www.youtube.com/watch?v={video_id}"
        items.append(
            {
                "id": video_id,
                "kind": "youtube",
                "source": clean_text(source.get("name") or channel_id, 80),
                "title": clean_text(entry.get("title")),
                "url": link,
                "details": "",
                "published": entry_datetime(entry),
            }
        )
    return items


def fetch_rss(source: dict[str, Any], timeout: int) -> list[dict[str, Any]]:
    url = str(source.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise SourceError("url должен начинаться с http:// или https://")
    feed = fetch_feed(url, timeout)

    items: list[dict[str, Any]] = []
    for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
        link = str(entry.get("link") or "").strip()
        raw_id = str(entry.get("id") or link or entry.get("title") or "").strip()
        if not raw_id:
            continue
        if not link:
            feed_link = ""
            try:
                feed_link = str(feed.feed.get("link") or "")
            except AttributeError:
                feed_link = ""
            link = feed_link or url
        items.append(
            {
                "id": raw_id,
                "kind": "rss",
                "source": clean_text(source.get("name") or url, 80),
                "title": clean_text(entry.get("title")),
                "url": link,
                "details": "",
                "published": entry_datetime(entry),
            }
        )
    return items


# ---------------------------------------------------------------------------
# Twitch
# ---------------------------------------------------------------------------

class TwitchClient:
    """Токен приложения (client_credentials) + Get Streams."""

    def __init__(self, client_id: str, client_secret: str, timeout: int = 20):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0

    def _get_token(self, force: bool = False) -> str:
        if not force and self._token and time.time() < self._token_expires_at - 60:
            return self._token

        try:
            response = requests.post(
                TWITCH_TOKEN_URL,
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SourceError(f"Twitch: сеть недоступна ({exc})") from exc

        if response.status_code != 200:
            raise SourceError(
                "Twitch не выдал токен "
                f"(HTTP {response.status_code}): проверь TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET"
            )
        try:
            payload = response.json()
            self._token = str(payload["access_token"])
            self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        except (ValueError, KeyError) as exc:
            raise SourceError("Twitch вернул неожиданный ответ при выдаче токена") from exc
        return self._token

    def _get_streams_page(self, token: str, logins: list[str]) -> Any:
        try:
            return requests.get(
                TWITCH_STREAMS_URL,
                params=[("user_login", login) for login in logins],
                headers={
                    "Client-Id": self.client_id,
                    "Authorization": f"Bearer {token}",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SourceError(f"Twitch: сеть недоступна ({exc})") from exc

    def get_streams(self, logins: list[str]) -> dict[str, dict[str, Any]]:
        """Возвращает {login: данные_стрима} только для тех, кто сейчас в эфире."""
        logins = [login for login in dict.fromkeys(logins) if login]
        if not logins:
            return {}

        token = self._get_token()
        online: dict[str, dict[str, Any]] = {}
        for start in range(0, len(logins), 100):  # API принимает до 100 логинов за раз
            chunk = logins[start : start + 100]
            response = self._get_streams_page(token, chunk)
            if response.status_code == 401:  # токен протух — берём новый и повторяем
                token = self._get_token(force=True)
                response = self._get_streams_page(token, chunk)
            if response.status_code != 200:
                raise SourceError(f"Twitch: HTTP {response.status_code} при запросе стримов")
            try:
                data = response.json().get("data", [])
            except ValueError as exc:
                raise SourceError("Twitch вернул ответ, который не является JSON") from exc
            for stream in data:
                login = str(stream.get("user_login") or "").lower()
                if login:
                    online[login] = stream
        return online


def twitch_start_item(login: str, stream: dict[str, Any], label: str) -> dict[str, Any]:
    """Сообщение «начался стрим»: ник (в source), название, игра, ссылка."""
    stream_id = str(stream.get("id") or "")
    game = clean_text(stream.get("game_name"), 60)
    return {
        "id": stream_id,
        "kind": "twitch",
        "source": label,
        "title": clean_text(stream.get("title") or "Стрим без названия"),
        "url": f"https://www.twitch.tv/{login}",
        "details": f"Игра: {game}" if game else "",
        "published": None,
        # запомним в seen.json только после успешной отправки
        "live": {"stream_id": stream_id, "game": game, "announced": True},
    }


def handle_twitch_sources(
    sources: list[dict[str, Any]],
    logins: list[str],
    online: dict[str, dict[str, Any]],
    seen: SeenStore,
    settings: dict[str, Any],
    outbox: list[tuple[str, dict[str, Any]]],
) -> None:
    """Сравнивает «кто в эфире» с состоянием в seen.json и готовит start/end.

    Логика:
      * стрим идёт, а мы про него ещё не писали  -> «начался стрим» + запомнить;
      * стрим идёт, id тот же                     -> тишина;
      * стрим идёт, id другой (новый эфир)        -> «начался стрим»;
      * стрим закончился, мы писали о начале      -> «стрим закончился»;
      * стрим закончился, о начале не писали      -> просто забыть (без сообщения).
    """
    for source, login in zip(sources, logins):
        if not login:
            log.warning("[Twitch] пустой login у одного из стримеров — пропускаю")
            continue

        key = f"twitch:{login}"
        label = clean_text(source.get("name") or login, 60)
        stream = online.get(login)
        state = seen.get_live(key)

        if stream:
            stream_id = str(stream.get("id") or "")
            if not stream_id:
                continue
            if state and str(state.get("stream_id")) == stream_id:
                continue  # об этом стриме уже уведомили

            was_known = seen.has_source(key)
            seen.ensure(key)
            item = twitch_start_item(login, stream, label)
            if not was_known and settings["first_run_silent"]:
                # стример уже в эфире в момент настройки — не спамим,
                # но запоминаем, чтобы потом не прислать «закончился» без начала
                seen.set_live(key, {**item["live"], "announced": False})
                log.info("[%s] первый запуск: стрим уже идёт, уведомление пропускаю", label)
                continue
            outbox.append((key, item))  # состояние обновим после отправки
        else:
            seen.ensure(key)
            if not state:
                continue
            if state.get("announced") and settings.get("notify_stream_end", True):
                game = clean_text(state.get("game"), 60)
                outbox.append(
                    (
                        key,
                        {
                            "id": f"end:{state.get('stream_id', '')}",
                            "kind": "twitch_end",
                            "source": label,
                            "title": "Стрим закончился",
                            "url": f"https://www.twitch.tv/{login}",
                            "details": f"Игра: {game}" if game else "",
                            "published": None,
                        },
                    )
                )
            else:
                seen.clear_live(key)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False, timeout: int = 30):
        self.token = token
        self.chat_id = str(chat_id)
        self.dry_run = dry_run
        self.timeout = timeout
        self.api_url = f"https://api.telegram.org/bot{token}"

    def send(self, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
        text = text[:TELEGRAM_TEXT_LIMIT]
        if self.dry_run:
            log.info("--- dry-run: сообщение в Telegram ---\n%s\n%s", text, "-" * 45)
            return True

        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        for attempt in (1, 2, 3):
            try:
                response = requests.post(
                    f"{self.api_url}/sendMessage", json=payload, timeout=self.timeout
                )
            except requests.RequestException as exc:
                log.warning("Telegram: сеть недоступна (%s), попытка %d/3", exc, attempt)
                time.sleep(2 * attempt)
                continue

            if response.status_code == 200:
                return True

            description = ""
            retry_after = 0
            try:
                body = response.json()
                description = str(body.get("description") or "")
                retry_after = int((body.get("parameters") or {}).get("retry_after") or 0)
            except (ValueError, TypeError, AttributeError):
                description = response.text[:200]

            if response.status_code == 429:  # слишком много сообщений
                wait = min(retry_after or 3, 60)
                log.warning("Telegram: лимит отправки, жду %d с", wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500:  # проблемы на стороне Telegram
                time.sleep(2 * attempt)
                continue

            log.error("Telegram: ошибка %s — %s", response.status_code, description)
            return False
        return False

    # --- команды и кнопки -------------------------------------------------

    def get_updates(self, offset: int | None = None, wait: int = 0) -> list[dict[str, Any]]:
        """Забирает новые сообщения/нажатия кнопок. wait — long polling, секунды."""
        if self.dry_run:
            return []
        payload: dict[str, Any] = {
            "timeout": wait,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset:
            payload["offset"] = offset
        try:
            response = requests.post(
                f"{self.api_url}/getUpdates",
                json=payload,
                timeout=self.timeout + wait,
            )
        except requests.RequestException as exc:
            log.debug("Telegram getUpdates: %s", exc)
            return []
        if response.status_code != 200:
            log.warning("Telegram getUpdates: HTTP %s %s", response.status_code, response.text[:200])
            return []
        try:
            result = response.json().get("result") or []
        except (ValueError, AttributeError):
            return []
        return result if isinstance(result, list) else []

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Убирает «часики» с кнопки; text — всплывающая подсказка."""
        if self.dry_run or not callback_id:
            return
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            requests.post(
                f"{self.api_url}/answerCallbackQuery", json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            log.debug("Telegram answerCallbackQuery: %s", exc)

    def set_commands(self) -> None:
        """Показывает /start и /menu в списке команд бота."""
        if self.dry_run:
            return
        try:
            requests.post(
                f"{self.api_url}/setMyCommands",
                json={
                    "commands": [
                        {"command": "start", "description": "Меню"},
                        {"command": "menu", "description": "Меню"},
                    ]
                },
                timeout=self.timeout,
            )
        except requests.RequestException:
            pass


def build_message(item: dict[str, Any]) -> str:
    emoji, kind_label = KIND_LABELS.get(item.get("kind", ""), ("🔔", "Обновление"))
    header = f"{emoji} {kind_label}"
    source = clean_text(item.get("source"), 80)
    if source:
        header += f" · {source}"
    lines = [header]
    title = clean_text(item.get("title"), 300)
    if title and title != kind_label:  # у «стрим закончился» заголовок уже в шапке
        lines.append(title)
    if item.get("details"):
        lines.append(str(item["details"]))
    if item.get("url"):
        lines.append(str(item["url"]))
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Команды бота: /start и кнопки меню
# ---------------------------------------------------------------------------

def sources_summary(config: dict[str, Any]) -> str:
    """Список источников, сгруппированный по платформам."""
    blocks: list[str] = []

    youtube = config.get("youtube") or []
    if youtube:
        lines = [f"🎬 YouTube ({len(youtube)}):"]
        lines += [f"  • {clean_text(s.get('name') or s.get('channel_id'), 60)}" for s in youtube]
        blocks.append("\n".join(lines))

    twitch = config.get("twitch") or []
    if twitch:
        lines = [f"🔴 Twitch ({len(twitch)}):"]
        lines += [f"  • {clean_text(s.get('name') or s.get('login'), 60)}" for s in twitch]
        blocks.append("\n".join(lines))

    rss = config.get("rss") or []
    if rss:
        lines = [f"📰 RSS / сайты ({len(rss)}):"]
        lines += [f"  • {clean_text(s.get('name') or s.get('url'), 60)}" for s in rss]
        blocks.append("\n".join(lines))

    if not blocks:
        return "Пока пусто: добавь источники в config.json."
    return "📋 Откуда приходят уведомления\n\n" + "\n\n".join(blocks)


def online_summary(config: dict[str, Any], timeout: int) -> str:
    """Кто из твоих стримеров сейчас в эфире (запрос к Twitch в момент нажатия)."""
    sources = config.get("twitch") or []
    if not sources:
        return "Twitch не настроен: в config.json нет стримеров."

    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return "Twitch не настроен: нет TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET."

    logins = [normalize_login(source.get("login")) for source in sources]
    try:
        online = TwitchClient(client_id, client_secret, timeout).get_streams(logins)
    except Exception as exc:
        return f"Не удалось узнать, кто в эфире: {exc}"

    if not online:
        return "🟢 Сейчас никто из твоих стримеров не в эфире."

    lines = ["🟢 Сейчас в эфире:"]
    for login, stream in online.items():
        game = clean_text(stream.get("game_name"), 40)
        title = clean_text(stream.get("title"), 80)
        name = clean_text(stream.get("user_name") or login, 60)
        lines.append(f"\n• {name} — {game or 'без категории'}")
        if title:
            lines.append(f"   {title}")
        lines.append(f"   https://www.twitch.tv/{login}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Добавление источников из чата
# ---------------------------------------------------------------------------

YOUTUBE_CHANNEL_RE = re.compile(r'"(?:channelId|externalId)":"(UC[\w-]{22})"')
YOUTUBE_LINK_ID_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
TWITCH_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")


def http_get(url: str, timeout: int) -> Any:
    try:
        return requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise SourceError(f"не удалось открыть страницу: {exc}") from exc


def detect_platform(text: str) -> str | None:
    low = text.strip().lower()
    if "youtu.be/" in low or "watch?v=" in low:
        return "youtube_video"
    if "youtube.com/" in low or low.startswith("@"):
        return "youtube"
    if "twitch.tv/" in low:
        return "twitch"
    if low.startswith(("http://", "https://")):
        return "rss"
    return None


def feed_title(feed: Any) -> str:
    try:
        return clean_text(feed.feed.get("title") or "", 80)
    except AttributeError:
        return ""


def resolve_youtube(text: str, timeout: int) -> tuple[str, str, int]:
    """Возвращает (channel_id, название, сколько видео в ленте)."""
    raw = text.strip()
    if "watch?v=" in raw or "youtu.be/" in raw:
        raise SourceError("это ссылка на видео, а нужна ссылка на канал")

    channel_id = normalize_channel_id(raw)
    if not channel_id:
        url = raw if raw.lower().startswith(("http://", "https://")) else f"https://www.youtube.com/@{raw.lstrip('@')}"
        response = http_get(url, timeout)
        if response.status_code >= 400:
            raise SourceError(f"YouTube ответил HTTP {response.status_code} — канал не найден?")
        match = YOUTUBE_CHANNEL_RE.search(response.text) or YOUTUBE_LINK_ID_RE.search(str(response.url))
        channel_id = match.group(1) if match else ""

    if not channel_id:
        raise SourceError("не нашёл ID канала — пришли ссылку вида youtube.com/@handle")

    feed = fetch_feed(YOUTUBE_RSS_URL.format(channel_id=channel_id), timeout)
    return channel_id, feed_title(feed) or channel_id, len(feed.entries)


def resolve_twitch(text: str, timeout: int) -> tuple[str, str]:
    """Возвращает (login, отображаемое имя)."""
    login = normalize_login(text)
    if not login or not TWITCH_LOGIN_RE.match(login):
        raise SourceError("не похоже на ник Twitch — пришли ссылку twitch.tv/ник")

    response = http_get(f"https://www.twitch.tv/{login}", timeout)
    if response.status_code == 404:
        raise SourceError(f"канал «{login}» не найден на Twitch")
    if response.status_code >= 400:
        raise SourceError(f"Twitch ответил HTTP {response.status_code}")

    name = login
    match = re.search(r"<title>([^<]*)</title>", response.text, re.IGNORECASE)
    if match:
        title = html.unescape(match.group(1)).strip()
        if title.lower().endswith("- twitch"):
            title = title[: -len("- twitch")].strip()
        if title and "twitch" not in title.lower():
            name = clean_text(title, 60)
    return login, name


def resolve_rss(text: str, timeout: int) -> tuple[str, str, int]:
    """Возвращает (url, название, сколько записей в ленте)."""
    url = text.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise SourceError("нужна ссылка на RSS-ленту, начинающаяся с http:// или https://")
    feed = fetch_feed(url, timeout)
    return url, feed_title(feed) or url, len(feed.entries)


def add_source_to_config(
    config: dict[str, Any],
    config_path: Path,
    platform: str,
    value: str,
    name: str,
) -> str:
    """Дописывает источник в config.json. Возвращает 'added' или 'exists'."""
    items = config.get(platform)
    if not isinstance(items, list):
        items = []
        config[platform] = items

    if platform == "youtube":
        exists = any(normalize_channel_id(item.get("channel_id")) == value for item in items)
        entry = {"name": name, "channel_id": value}
    elif platform == "twitch":
        exists = any(normalize_login(item.get("login")) == value for item in items)
        entry = {"name": name, "login": value}
    else:
        exists = any(str(item.get("url") or "").strip() == value for item in items)
        entry = {"name": name, "url": value}

    if exists:
        return "exists"

    items.append(entry)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, config_path)
    return "added"


def handle_add_request(
    telegram: Telegram,
    config: dict[str, Any],
    config_path: Path,
    text: str,
    timeout: int,
) -> None:
    """Разбирает сообщение пользователя и добавляет найденный источник."""
    platform = detect_platform(text)
    if platform == "youtube_video":
        telegram.send("Это ссылка на видео. Пришли ссылку на канал: youtube.com/@name или /channel/UC...")
        return
    if platform is None:
        telegram.send(ADD_TEXT)
        return

    try:
        if platform == "youtube":
            value, name, count = resolve_youtube(text, timeout)
            status = add_source_to_config(config, config_path, "youtube", value, name)
            success = (
                f"✅ Добавил YouTube-канал «{name}».\n"
                f"В ленте сейчас видео: {count}.\n"
                "Уведомления начнутся с нового ролика."
            )
        elif platform == "twitch":
            value, name = resolve_twitch(text, timeout)
            status = add_source_to_config(config, config_path, "twitch", value, name)
            success = (
                f"✅ Добавил стримера «{name}».\n"
                "Уведомление придёт, когда начнётся стрим."
            )
        else:
            value, name, count = resolve_rss(text, timeout)
            status = add_source_to_config(config, config_path, "rss", value, name)
            success = (
                f"✅ Добавил ленту «{name}».\n"
                f"В ленте сейчас записей: {count}.\n"
                "Новые посты начнут приходить с этого момента."
            )
    except SourceError as exc:
        telegram.send(f"❌ Не получилось: {exc}")
        return

    if status == "exists":
        telegram.send(f"☑️ Уже в списке: {name}")
    else:
        telegram.send(success)


def handle_updates(
    telegram: Telegram,
    config: dict[str, Any],
    seen: SeenStore,
    timeout: int,
    wait: int = 0,
    config_path: Path | None = None,
) -> int:
    """Обрабатывает накопившиеся команды, кнопки и ссылки. Возвращает число команд.

    wait > 0 включает long polling: Telegram держит соединение, пока не придёт
    команда или не истечёт wait секунд. Так бот отвечает мгновенно, если
    скрипт запущен в режиме --loop.
    """
    offset = seen.get_offset()
    updates = telegram.get_updates(offset=offset or None, wait=wait)
    if not updates:
        return 0

    handled = 0
    for update in updates:
        try:
            update_id = int(update.get("update_id") or 0)
        except (TypeError, ValueError):
            continue
        if update_id >= offset:
            offset = update_id + 1

        message = update.get("message") or {}
        text = str(message.get("text") or "").strip()
        if text.startswith(("/start", "/menu", "/help")):
            telegram.send(MENU_TEXT, reply_markup=MAIN_MENU)
            handled += 1
            continue

        # дальше — только из «своего» чата (чужой человек не должен править конфиг)
        chat_id = str((message.get("chat") or {}).get("id") or "")
        from_owner = bool(chat_id) and chat_id == telegram.chat_id

        if text and from_owner and config_path is not None:
            if text.startswith("/"):
                telegram.send("Не знаю такую команду. /start — меню, а чтобы добавить источник — пришли ссылку.")
            else:
                handle_add_request(telegram, config, config_path, text, timeout)
            handled += 1
            continue

        callback = update.get("callback_query") or {}
        if not callback:
            continue
        callback_id = str(callback.get("id") or "")
        data = callback.get("data")
        if data == "sources":
            telegram.answer_callback(callback_id)
            telegram.send(sources_summary(config))
        elif data == "online":
            telegram.answer_callback(callback_id, "Смотрю, кто в эфире…")
            telegram.send(online_summary(config, timeout))
        elif data == "add":
            telegram.answer_callback(callback_id)
            telegram.send(ADD_TEXT)
        else:
            telegram.answer_callback(callback_id)
        handled += 1

    seen.set_offset(offset)
    return handled


# ---------------------------------------------------------------------------
# Логика «что новое»
# ---------------------------------------------------------------------------

def is_fresh(item: dict[str, Any], max_age_days: int) -> bool:
    if max_age_days <= 0:
        return True
    published = item.get("published")
    if published is None:
        return True  # без даты считаем запись актуальной
    return published >= datetime.now(timezone.utc) - timedelta(days=max_age_days)


def sort_key(item: dict[str, Any]) -> float:
    published = item.get("published")
    return published.timestamp() if published else 0.0


def process_items(
    key: str,
    label: str,
    items: list[dict[str, Any]],
    seen: SeenStore,
    settings: dict[str, Any],
    outbox: list[tuple[str, dict[str, Any]]],
) -> None:
    """Отбирает новое и складывает в outbox. Ничего не отправляет и не сохраняет."""
    items = [item for item in items if is_fresh(item, settings["max_age_days"])]
    items.sort(key=sort_key)  # старые раньше — сообщения придут по порядку

    if not items:
        seen.ensure(key)  # источник существует, но нового пока нет
        return

    if not seen.has_source(key):
        seen.ensure(key)
        if settings["first_run_silent"]:
            for item in items:
                seen.mark(key, item["id"])
            log.info("[%s] первый запуск: запомнил %d записей, отправлять не буду", label, len(items))
            return

    new_items = [item for item in items if seen.is_new(key, item["id"])]
    if not new_items:
        log.info("[%s] нового нет", label)
        return

    for item in new_items:
        if len(outbox) >= settings["max_messages_per_run"]:
            log.warning(
                "[%s] достигнут лимит %d сообщений за запуск — остальное придёт в следующий раз",
                label,
                settings["max_messages_per_run"],
            )
            break
        outbox.append((key, item))


# ---------------------------------------------------------------------------
# Один цикл проверки
# ---------------------------------------------------------------------------

def run_once(config: dict[str, Any], seen: SeenStore, telegram: Telegram, dry_run: bool) -> int:
    settings = config["settings"]
    timeout = settings["request_timeout"]

    if not (config["youtube"] or config["twitch"] or config["rss"]):
        log.warning("В config.json нет ни одного источника — проверять нечего")
        return 0

    outbox: list[tuple[str, dict[str, Any]]] = []
    errors: list[str] = []
    checked = 0
    ok_sources = 0

    # --- YouTube -----------------------------------------------------------
    for source in config["youtube"]:
        checked += 1
        channel_id = normalize_channel_id(source.get("channel_id"))
        label = clean_text(source.get("name") or channel_id or "YouTube", 60)
        try:
            items = fetch_youtube(source, timeout)
        except Exception as exc:
            log.warning("[YouTube: %s] %s", label, exc)
            errors.append(f"YouTube «{label}»: {exc}")
            continue
        ok_sources += 1
        process_items(f"youtube:{channel_id}", label, items, seen, settings, outbox)

    # --- RSS --------------------------------------------------------------
    for source in config["rss"]:
        checked += 1
        label = clean_text(source.get("name") or source.get("url") or "RSS", 60)
        key = f"rss:{source.get('url')}"
        try:
            items = fetch_rss(source, timeout)
        except Exception as exc:
            log.warning("[RSS: %s] %s", label, exc)
            errors.append(f"RSS «{label}»: {exc}")
            continue
        ok_sources += 1
        process_items(key, label, items, seen, settings, outbox)

    # --- Twitch (один запрос на всех стримеров) ----------------------------
    twitch_sources = config["twitch"]
    if twitch_sources:
        checked += len(twitch_sources)
        client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
        client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            message = "не заданы TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET в .env"
            log.warning("[Twitch] %s", message)
            errors.append(f"Twitch: {message}")
        else:
            logins = [normalize_login(source.get("login")) for source in twitch_sources]
            try:
                online = TwitchClient(client_id, client_secret, timeout).get_streams(logins)
            except Exception as exc:
                log.warning("[Twitch] %s", exc)
                errors.append(f"Twitch: {exc}")
            else:
                ok_sources += len(twitch_sources)
                handle_twitch_sources(twitch_sources, logins, online, seen, settings, outbox)

    # --- Отправка ---------------------------------------------------------
    sent = 0
    for key, item in outbox:
        if telegram.send(build_message(item)):
            seen.mark(key, item["id"])
            # состояние Twitch меняем только после реальной отправки:
            # если Telegram недоступен, попробуем ещё раз в следующий запуск
            if item.get("kind") == "twitch_end":
                seen.clear_live(key)
            elif item.get("live"):
                seen.set_live(key, item["live"])
            sent += 1
            if not dry_run:
                time.sleep(0.3)  # вежливость к лимитам Telegram
        else:
            # не помечаем как отправленное — попробуем в следующий запуск
            errors.append(f"Telegram: не удалось отправить «{item.get('title', '')[:60]}»")

    if sent:
        log.info("Отправлено сообщений: %d", sent)
    if errors:
        log.warning("Проблем за запуск: %d", len(errors))
        for error in errors:
            log.warning("  • %s", error)
    log.info(
        "Проверка завершена: источников %d, отправлено сообщений %d, ошибок %d",
        checked,
        sent,
        len(errors),
    )

    if checked and ok_sources == 0:
        log.error("Ни один источник не удалось проверить — проверь интернет и config.json")
        return 1
    if outbox and sent == 0:
        log.error("Ни одно сообщение не удалось отправить в Telegram")
        return 1
    return 0


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверяет YouTube / Twitch / RSS и присылает новое в Telegram."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="путь к config.json")
    parser.add_argument("--loop", action="store_true", help="работать постоянно (проверка каждые N минут)")
    parser.add_argument("--dry-run", action="store_true", help="показать сообщения, ничего не отправляя")
    parser.add_argument("--test-telegram", action="store_true", help="отправить тестовое сообщение")
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    return parser.parse_args(argv)


def build_telegram(config: dict[str, Any], dry_run: bool) -> Telegram | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = str(config["telegram"].get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if dry_run:
        return Telegram(token or "DRY-RUN", chat_id or "0", dry_run=True)
    if not token or not chat_id:
        return None
    return Telegram(token, chat_id, dry_run=False)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv(ENV_PATH)
    socket.setdefaulttimeout(30)  # feedparser не умеет принимать timeout напрямую

    config_path = Path(args.config)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    telegram = build_telegram(config, args.dry_run)
    if telegram is None:
        log.error(
            "Не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID. "
            "Скопируй .env.example в .env и заполни значения (или задай переменные окружения)."
        )
        return 2

    if args.test_telegram:
        ok = telegram.send("✅ Тест: связка «Python → Telegram» работает.")
        log.info("Тест отправлен" if ok else "Тест НЕ отправлен — смотри ошибку выше")
        return 0 if ok else 1

    seen = SeenStore(SEEN_PATH)
    interval_minutes = config["settings"]["interval_minutes"]
    timeout = config["settings"]["request_timeout"]

    if not args.loop:
        if not args.dry_run:
            telegram.set_commands()
            replied = handle_updates(telegram, config, seen, timeout, config_path=config_path)
            if replied:
                log.info("Ответил на команд/кнопок: %d", replied)
        code = run_once(config, seen, telegram, args.dry_run)
        if not args.dry_run:
            seen.save()
        return code

    log.info(
        "Режим --loop: проверка каждые %d мин, команды бота — сразу. Остановка — Ctrl+C.",
        interval_minutes,
    )
    try:
        while True:
            started = time.time()
            try:
                if not args.dry_run:
                    telegram.set_commands()
                    handle_updates(telegram, config, seen, timeout, config_path=config_path)
                run_once(config, seen, telegram, args.dry_run)
            except Exception:  # цикл не должен умирать из-за одной ошибки
                log.exception("Непредвиденная ошибка цикла")
            if not args.dry_run:
                seen.save()

            # Ждём до следующей проверки, но всё это время слушаем Telegram:
            # long polling отвечает на /start и кнопки мгновенно.
            deadline = started + interval_minutes * 60
            while time.time() < deadline:
                remaining = deadline - time.time()
                if args.dry_run:
                    time.sleep(min(5.0, max(0.1, remaining)))
                    continue
                if remaining <= 2:
                    break
                wait_seconds = max(1, int(min(50, remaining)))
                try:
                    handle_updates(telegram, config, seen, timeout, wait=wait_seconds, config_path=config_path)
                except Exception:
                    log.exception("Ошибка обработки команд бота")
                seen.save()
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")
        if not args.dry_run:
            seen.save()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
