# LINKCHECK — backend с проверкой публичных Telegram-ресурсов

import asyncio
import csv
from collections import deque
import gzip
import hashlib
import html
import ipaddress
import hmac
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import socket
import time

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram import F
from aiogram.types import (
    Message,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery
)

from dotenv import load_dotenv

try:
    from telethon import TelegramClient, functions
    from telethon.errors import FloodWaitError, RPCError
    from telethon.tl.types import (
        User,
        Chat,
        Channel,
        EmojiStatusCollectible
    )
except ImportError:
    TelegramClient = None
    functions = None
    FloodWaitError = RPCError = Exception
    User = Chat = Channel = None
    EmojiStatusCollectible = None

load_dotenv()

# Persistent data directory:
# - on Bothost, /app/data survives container rebuilds and archive redeploys;
# - locally, the project directory is used to preserve the existing layout.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = "/app/data" if os.path.isdir("/app/data") else APP_DIR
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", DEFAULT_DATA_DIR))
os.makedirs(DATA_DIR, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "linkcheck.db"))
PORT = int(os.getenv("PORT", "8080"))
PLUS_PAYMENTS_ENABLED = os.getenv("PLUS_PAYMENTS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

# Bothost's documentation uses TELEGRAM_API_ID / TELEGRAM_API_HASH;
# TG_API_ID / TG_API_HASH remain supported for the existing local .env.
TG_API_ID = int(os.getenv("TG_API_ID", os.getenv("TELEGRAM_API_ID", "0")) or "0")
TG_API_HASH = os.getenv("TG_API_HASH", os.getenv("TELEGRAM_API_HASH", "")).strip()
TG_SESSION = os.getenv("TG_SESSION", os.path.join(DATA_DIR, "linkcheck_telegram")).strip()

# Ensure an explicitly configured database path also has an existing parent.
_db_parent = os.path.dirname(os.path.abspath(DB_PATH))
os.makedirs(_db_parent, exist_ok=True)

FOUNDER_ID = 6182269112
FOUNDER_FALLBACK_USERNAME = "nifort"

PUBLIC_DATASET_URL = (
    "https://huggingface.co/datasets/"
    "starnikovoleg/telegram-public-channels-2026-W36/"
    "resolve/main/channels.csv"
)
PUBLIC_DATASET_PAGE = (
    "https://huggingface.co/datasets/"
    "starnikovoleg/telegram-public-channels-2026-W36"
)
PUBLIC_DATASET_LIMIT = 1500

# ============================================================
# НАСТРОЙКИ ПРОВЕРКИ TELEGRAM
# ============================================================

TELEGRAM_CHECK_CACHE_TTL = 900
TELEGRAM_NOT_FOUND_CACHE_TTL = 300
TELEGRAM_UNAVAILABLE_CACHE_TTL = 5
TELEGRAM_CHECK_MIN_INTERVAL = 2.0
TELEGRAM_AUTO_WAIT_MAX = 20

# Публичная страница t.me используется как основной способ проверки открытых
# username. Она не расходует MTProto-лимит ResolveUsernameRequest.
TELEGRAM_PUBLIC_PAGE_CACHE_TTL = 900
TELEGRAM_PUBLIC_PAGE_NOT_FOUND_TTL = 300
TELEGRAM_PUBLIC_PAGE_MIN_INTERVAL = 2.0
TELEGRAM_PUBLIC_PAGE_TIMEOUT = 12
telegram_public_page_cache = {}
telegram_public_page_lock = asyncio.Lock()
telegram_public_page_last_request_at = 0

# Все операции MTProto, включая профиль создателя и медиа,
# проходят через одну очередь. Ограничения Telegram не обходятся:
# короткий FloodWait реально выжидается, длинный возвращается клиенту.
TELEGRAM_MTPROTO_MIN_INTERVAL = 2.0
telegram_flood_wait_until = 0
telegram_last_check_at = 0
telegram_last_mtproto_request_at = 0
telegram_check_cache = {}
telegram_check_lock = asyncio.Lock()
telegram_mtproto_lock = asyncio.Lock()

# ============================================================
# НАСТРОЙКИ КЭША ПРОФИЛЯ СОЗДАТЕЛЯ
# ============================================================

FOUNDER_PROFILE_CACHE_TTL = 60
FOUNDER_AVATAR_CACHE_TTL = 120
FOUNDER_GIFT_CACHE_TTL = 3600

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

tg_client = None
tg_ready = False
tg_start_error = None

founder_entity_cache = None
founder_entity_cache_time = 0
founder_entity_lock = asyncio.Lock()
founder_profile_cache = None
founder_profile_cache_time = 0
founder_profile_lock = asyncio.Lock()
founder_avatar_lock = asyncio.Lock()
founder_avatar_cache_time = 0
founder_gift_cache = {}
founder_gift_lock = asyncio.Lock()


def founder_profile_cache_path():
    """Путь к постоянному кэшу профиля создателя."""
    return os.path.join(DATA_DIR, "founder_profile_cache.json")


def founder_gift_directory():
    """Каталог постоянного хранения Lottie-анимаций коллекционного подарка."""
    return os.path.join(DATA_DIR, "founder_gifts")


def founder_avatar_path():
    """Постоянный кэш актуальной фотографии создателя."""
    return os.path.join(DATA_DIR, "founder_nifort.jpg")


def load_founder_identity_from_telethon_session():
    """Читает сохранённые username/name основателя из Telethon session (read-only)."""
    session_name = TG_SESSION
    if not session_name.endswith(".session"):
        session_name += ".session"
    session_path = session_name if os.path.isabs(session_name) else os.path.join(APP_DIR, session_name)
    if not os.path.isfile(session_path):
        return None

    try:
        # Режим read-only: существующая Telegram session не изменяется.
        uri = "file:" + session_path.replace("\\", "/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            row = connection.execute(
                "SELECT username, name FROM entities WHERE id = ? LIMIT 1",
                (FOUNDER_ID,)
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        username = str(row[0] or "").strip().lstrip("@")
        display_name = str(row[1] or "").strip()
        if not username and not display_name:
            return None
        logging.info(
            "Founder identity restored from local Telethon session: username=%s name_available=%s",
            username or FOUNDER_FALLBACK_USERNAME,
            bool(display_name)
        )
        return {
            "username": username,
            "display_name": display_name
        }
    except Exception as exc:
        logging.warning("Could not read founder identity from Telethon session: %s", exc)
        return None


def find_cached_founder_gift_document_id():
    """Восстанавливает ID из уже сохранённой анимации, не обращаясь к Telegram."""
    gift_dir = founder_gift_directory()
    if not os.path.isdir(gift_dir):
        return None

    candidates = []
    try:
        filenames = os.listdir(gift_dir)
    except OSError as exc:
        logging.warning("Cannot inspect founder gift cache: %s", exc)
        return None

    for filename in filenames:
        match = re.fullmatch(r"(\d+)\.json", filename)
        if not match:
            continue
        document_id = int(match.group(1))
        path = os.path.join(gift_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as file:
                animation = json.load(file)
            # Принимаем только Lottie JSON, а не произвольный файл с числовым именем.
            if not isinstance(animation, dict) or not isinstance(animation.get("layers"), list):
                continue
            candidates.append((os.path.getmtime(path), document_id))
        except Exception as exc:
            logging.warning("Invalid cached founder gift animation %s: %s", filename, exc)

    if not candidates:
        return None

    candidates.sort(reverse=True)
    if len(candidates) > 1:
        logging.warning(
            "Several founder gift animations are cached; using the most recently saved one"
        )
    document_id = candidates[0][1]
    logging.info("Founder gift document restored from local animation cache: %s", document_id)
    return document_id


def load_founder_profile_cache_from_disk():
    """Загружает последний удачный профиль; пустой fallback не перезаписывает его."""
    path = founder_profile_cache_path()
    profile = None

    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                stored = json.load(file)
            if isinstance(stored, dict) and int(stored.get("id", 0)) == FOUNDER_ID:
                username = (
                    str(stored.get("username") or "").strip().lstrip("@")
                    if "username" in stored else FOUNDER_FALLBACK_USERNAME
                )
                first_name = str(stored.get("first_name") or "").strip()
                last_name = str(stored.get("last_name") or "").strip()
                display_name = str(stored.get("display_name") or "").strip()
                if not display_name:
                    display_name = " ".join(x for x in (first_name, last_name) if x) or username
                try:
                    gift_document_id = int(stored.get("gift_document_id")) if stored.get("gift_document_id") else None
                except (TypeError, ValueError):
                    gift_document_id = None
                profile = {
                    "id": FOUNDER_ID,
                    "first_name": first_name,
                    "last_name": last_name,
                    "display_name": display_name,
                    "username": username,
                    "profile_url": (
                        "https://t.me/" + username if username
                        else "tg://user?id=" + str(FOUNDER_ID)
                    ),
                    "has_collectible": bool(stored.get("has_collectible") and gift_document_id),
                    "gift_document_id": gift_document_id,
                    "gift_title": stored.get("gift_title")
                }
                logging.info(
                    "Founder profile restored from disk: username=%s has_collectible=%s gift_document_id=%s",
                    username, profile["has_collectible"], gift_document_id
                )
        except Exception as exc:
            logging.warning("Founder profile disk cache is invalid: %s", exc)

    # Восстанавливаем ник и имя из локальной Telethon session (без сетевого запроса).
    identity = load_founder_identity_from_telethon_session()
    if profile is None and identity:
        username = identity["username"]
        display_name = identity["display_name"] or username
        profile = {
            "id": FOUNDER_ID,
            "first_name": "",
            "last_name": "",
            "display_name": display_name,
            "username": username,
            "profile_url": (
                "https://t.me/" + username if username
                else "tg://user?id=" + str(FOUNDER_ID)
            ),
            "has_collectible": False,
            "gift_document_id": None,
            "gift_title": None
        }
    # Даже если JSON профиля ещё не создавался, ID можно восстановить из ранее
    # скачанной анимации. Это особенно важно во время Telegram FloodWait.
    cached_document_id = find_cached_founder_gift_document_id()
    if cached_document_id and profile is None:
        username = identity["username"] if identity else FOUNDER_FALLBACK_USERNAME
        display_name = identity["display_name"] if identity and identity.get("display_name") else (username or str(FOUNDER_ID))
        profile = {
            "id": FOUNDER_ID,
            "first_name": "",
            "last_name": "",
            "display_name": display_name,
            "username": username,
            "profile_url": (
                "https://t.me/" + username if username
                else "tg://user?id=" + str(FOUNDER_ID)
            ),
            "has_collectible": True,
            "gift_document_id": cached_document_id,
            "gift_title": None
        }

    if profile is not None:
        save_founder_profile_cache_to_disk(profile)
    return profile


def save_founder_profile_cache_to_disk(profile):
    """Атомарно сохраняет только подтверждённый профиль основателя."""
    if not isinstance(profile, dict) or int(profile.get("id", 0)) != FOUNDER_ID:
        return
    username = str(profile.get("username") or "").strip().lstrip("@")
    payload = {
        "id": FOUNDER_ID,
        "first_name": str(profile.get("first_name") or ""),
        "last_name": str(profile.get("last_name") or ""),
        "display_name": str(profile.get("display_name") or username or FOUNDER_ID),
        "username": username,
        "profile_url": (
            "https://t.me/" + username if username
            else "tg://user?id=" + str(FOUNDER_ID)
        ),
        "has_collectible": bool(profile.get("has_collectible") and profile.get("gift_document_id")),
        "gift_document_id": int(profile["gift_document_id"]) if profile.get("gift_document_id") else None,
        "gift_title": profile.get("gift_title")
    }
    path = founder_profile_cache_path()
    temp_path = path + ".tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception as exc:
        logging.warning("Could not save founder profile cache: %s", exc)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

# ============================================================
# СТАРТОВЫЕ РЕСУРСЫ LINKCHECK
# ============================================================

SEED_PROJECTS = [
    {"title": "Telegram", "link": "@telegram", "category": "channel", "description": "Официальный канал Telegram.", "is_official": 1, "source_url": "https://telegram.org/"},
    {"title": "Telegram Tips", "link": "@TelegramTips", "category": "channel", "description": "Официальные советы и новости о возможностях Telegram.", "is_official": 1, "source_url": "https://telegram.org/"},
    {"title": "BotNews", "link": "@BotNews", "category": "channel", "description": "Новости платформы Telegram для разработчиков ботов.", "is_official": 1, "source_url": "https://core.telegram.org/bots"},
    {"title": "Telegram Premium", "link": "@premium", "category": "channel", "description": "Информация о возможностях Telegram Premium.", "is_official": 1, "source_url": "https://telegram.org/faq_premium"},
    {"title": "BotFather", "link": "@BotFather", "category": "bot", "description": "Официальный бот Telegram для создания и управления ботами.", "is_official": 1, "source_url": "https://core.telegram.org/bots"},
    {"title": "Premium Bot", "link": "@PremiumBot", "category": "bot", "description": "Официальный бот для оформления Telegram Premium.", "is_official": 1, "source_url": "https://telegram.org/faq_premium"},
    {"title": "Stickers", "link": "@Stickers", "category": "bot", "description": "Официальный бот Telegram для создания и управления стикерами.", "is_official": 1, "source_url": "https://telegram.org/"},
    {"title": "GIF", "link": "@gif", "category": "bot", "description": "Встроенный бот Telegram для поиска GIF.", "is_official": 1, "source_url": "https://telegram.org/"},
    {"title": "Vote", "link": "@vote", "category": "bot", "description": "Бот Telegram для создания опросов.", "is_official": 1, "source_url": "https://telegram.org/"},
    {"title": "QuizBot", "link": "@QuizBot", "category": "bot", "description": "Бот Telegram для создания викторин.", "is_official": 1, "source_url": "https://telegram.org/"},
    {"title": "DiscussBot", "link": "@discussbot", "category": "bot", "description": "Бот для обсуждений и комментариев в каналах.", "is_official": 1, "source_url": "https://telegram.org/"},
    {"title": "Binance", "link": "@BinanceExchange", "category": "group", "description": "Официальное англоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Announcements", "link": "@binance_announcements", "category": "channel", "description": "Официальный канал объявлений Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Chinese", "link": "@BinanceChinese", "category": "group", "description": "Официальное китайскоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance China Announcements", "link": "@binance_cn", "category": "channel", "description": "Китайскоязычный канал объявлений Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Russian Community", "link": "@BinanceRussianSpeaking", "category": "group", "description": "Официальное сообщество Binance для русскоговорящей аудитории.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Russia", "link": "@binance_ru", "category": "channel", "description": "Русскоязычный канал новостей Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Kazakhstan", "link": "@binance_kz", "category": "group", "description": "Региональное сообщество Binance для Казахстана.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Kyrgyzstan", "link": "@BinanceKyrgyz", "category": "group", "description": "Региональное сообщество Binance для Кыргызстана.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Armenia", "link": "@Binance_Armenian", "category": "group", "description": "Официальное сообщество Binance для Армении.", "is_official": 1, "source_url": "https://www.binance.com/en/community"},
    {"title": "Binance Balkans", "link": "@binancebalkans", "category": "group", "description": "Официальное сообщество Binance для Балкан.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Bangladesh", "link": "@BinanceBangladeshi", "category": "group", "description": "Региональное сообщество Binance для Бангладеш.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Cameroon", "link": "@BinanceCameroun", "category": "group", "description": "Региональное сообщество Binance для Камеруна.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Germany", "link": "@BinanceGerman", "category": "group", "description": "Немецкоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Australia", "link": "@binanceaustralia", "category": "group", "description": "Австралийское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Namibia", "link": "@BinanceNamibia", "category": "group", "description": "Региональное сообщество Binance для Намибии.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance New Zealand", "link": "@BinanceNewZealand", "category": "group", "description": "Региональное сообщество Binance для Новой Зеландии.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Zambia", "link": "@BinanceZambia", "category": "group", "description": "Региональное сообщество Binance для Замбии.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Spain", "link": "@BinanceSpanish", "category": "group", "description": "Испаноязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Argentina", "link": "@binanceargentinaoficial", "category": "group", "description": "Региональное сообщество Binance для Аргентины.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Spain Official", "link": "@Binance_ESP", "category": "group", "description": "Испаноязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Philippines", "link": "@BinanceFilipino", "category": "group", "description": "Филиппинское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance France", "link": "@BinanceFrench", "category": "group", "description": "Франкоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Greece", "link": "@BinanceGreek", "category": "group", "description": "Грекоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance India", "link": "@BinanceIndian", "category": "group", "description": "Индийское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Italy", "link": "@BinanceItalian", "category": "group", "description": "Италоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Kenya", "link": "@BinanceKenya", "category": "group", "description": "Кенийское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Nigeria", "link": "@BinanceNigeria", "category": "group", "description": "Нигерийское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Poland", "link": "@Binance_Polish", "category": "group", "description": "Польскоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Portugal", "link": "@BinancePortuguese", "category": "group", "description": "Региональное сообщество Binance для Португалии.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Portugal Community", "link": "@Binance_PT", "category": "group", "description": "Региональное сообщество Binance для Португалии.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Romania", "link": "@BinanceRomanian", "category": "group", "description": "Румыноязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Seychelles", "link": "@BinanceSeychelles", "category": "group", "description": "Региональное сообщество Binance для Сейшельских Островов.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance South Africa", "link": "@BinanceSouthAfrica", "category": "group", "description": "Южноафриканское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Sri Lanka", "link": "@binancesrilanka", "category": "group", "description": "Сообщество Binance для Шри-Ланки.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Tanzania", "link": "@BinanceTanzania", "category": "group", "description": "Танзанийское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Vietnam", "link": "@BinanceVietnamese", "category": "group", "description": "Вьетнамское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Uganda", "link": "@BinanceUganda", "category": "group", "description": "Угандийское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Czech and Slovakia", "link": "@BinanceCzechoSlovakia", "category": "group", "description": "Сообщество Binance для Чехии и Словакии.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Mongolian", "link": "@BinanceMongolian", "category": "group", "description": "Монгольское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Ukraine", "link": "@BinanceUkrainian", "category": "group", "description": "Украиноязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Hebrew", "link": "@BinanceHebrew", "category": "group", "description": "Ивритоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Arabic", "link": "@BinanceArabic", "category": "group", "description": "Арабоязычное сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Pakistan", "link": "@binancepakistani", "category": "group", "description": "Пакистанское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Nepal", "link": "@BinanceNepali", "category": "group", "description": "Непальское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Laos", "link": "@BinanceLaotian", "category": "group", "description": "Лаосское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Myanmar", "link": "@BinanceBurmese", "category": "group", "description": "Бирманское сообщество Binance.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Cambodia", "link": "@BinanceKhmer", "category": "group", "description": "Сообщество Binance для Камбоджи.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Flemish", "link": "@BinanceFlemish", "category": "group", "description": "Нидерландоязычное сообщество Binance для Фландрии.", "is_official": 1, "source_url": "https://www.binance.com/en/blog/community/421499824684903053"},
    {"title": "Binance Academy", "link": "@BinanceAcademy", "category": "channel", "description": "Образовательные материалы Binance Academy.", "is_official": 1, "source_url": "https://www.binance.com/en/academy"},
    {"title": "Binance Academy Chat", "link": "@BinanceAcademyChat", "category": "group", "description": "Сообщество Binance Academy.", "is_official": 1, "source_url": "https://www.binance.com/en/academy"},
    {"title": "Binance NFT", "link": "@BinanceNFTs", "category": "channel", "description": "Канал Binance, посвящённый NFT.", "is_official": 1, "source_url": "https://www.binance.com/"},
    {"title": "Binance Futures", "link": "@BinanceFutures_Official", "category": "channel", "description": "Обновления Binance Futures.", "is_official": 1, "source_url": "https://www.binance.com/"},
    {"title": "Binance API English", "link": "@binance_api_english", "category": "group", "description": "Сообщество пользователей и разработчиков Binance API.", "is_official": 1, "source_url": "https://www.binance.com/"},
    {"title": "Trust Wallet", "link": "@trustwallet", "category": "channel", "description": "Telegram-ресурс Trust Wallet, указанный в материалах Binance.", "is_official": 1, "source_url": "https://trustwallet.com/"},
    {"title": "TON Blockchain", "link": "@tonblockchain", "category": "channel", "description": "Официальный информационный ресурс TON.", "is_official": 1, "source_url": "https://ton.org/"}
]

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Единственная команда бота: открывает LINKCHECK Mini App."""
    text = (
        "Проверяйте подлинность Telegram-ресурсов и узнавайте, кому можно доверять. 🇷🇺\n\n"
        "Verify the authenticity of Telegram resources and find out which ones you can trust. 🇬🇧"
    )
    if MINIAPP_URL:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Открыть LINKCHECK",
                web_app=WebAppInfo(url=MINIAPP_URL)
            )
        ]])
        await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer(
            text + "\n\n⚠️ Mini App пока не настроен. Проверь MINIAPP_URL в .env."
        )


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_column(conn, table: str, column: str, definition: str):
    columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_projects_owner_nullable(conn):
    info = conn.execute("PRAGMA table_info(projects)").fetchall()
    owner_column = next((row for row in info if row["name"] == "owner_tg_id"), None)
    if owner_column is None or owner_column["notnull"] == 0:
        return

    logging.warning(
        "Обнаружена старая схема projects: owner_tg_id NOT NULL. Выполняется безопасная миграция."
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE projects_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_tg_id INTEGER,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                category TEXT,
                description TEXT,
                website TEXT,
                ownership_status TEXT NOT NULL DEFAULT 'unverified',
                moderation_status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                is_official INTEGER NOT NULL DEFAULT 0,
                telegram_verified INTEGER NOT NULL DEFAULT 0,
                source_url TEXT,
                is_seed INTEGER NOT NULL DEFAULT 0,
                topic TEXT,
                community_type TEXT,
                linkcheck_badge_manual INTEGER NOT NULL DEFAULT 0,
                linkcheck_badge_manual_by INTEGER,
                linkcheck_badge_manual_at TEXT,
                linkcheck_badge_manual_reason TEXT,
                FOREIGN KEY (owner_tg_id) REFERENCES users(tg_id) ON DELETE CASCADE
            )
        """)
        old_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        defaults = {
            "id": "NULL", "owner_tg_id": "NULL", "title": "''", "link": "''",
            "category": "NULL", "description": "NULL", "website": "NULL",
            "ownership_status": "'unverified'", "moderation_status": "'pending'",
            "created_at": "''", "is_official": "0", "telegram_verified": "0",
            "source_url": "NULL", "is_seed": "0", "topic": "NULL",
            "community_type": "NULL", "linkcheck_badge_manual": "0",
            "linkcheck_badge_manual_by": "NULL", "linkcheck_badge_manual_at": "NULL",
            "linkcheck_badge_manual_reason": "NULL"
        }
        target_columns = list(defaults.keys())
        select_expressions = [
            f'"{column}"' if column in old_columns else defaults[column]
            for column in target_columns
        ]
        conn.execute(
            f"INSERT INTO projects_new ({', '.join(target_columns)}) "
            f"SELECT {', '.join(select_expressions)} FROM projects"
        )
        old_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM projects_new").fetchone()[0]
        if old_count != new_count:
            raise RuntimeError("Миграция projects прервана: количество записей не совпало")
        conn.execute("DROP TABLE projects")
        conn.execute("ALTER TABLE projects_new RENAME TO projects")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_link ON projects(link)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_tg_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_status "
            "ON projects(moderation_status, ownership_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_catalog "
            "ON projects(moderation_status, is_official, telegram_verified)"
        )
        conn.commit()
        logging.info("Миграция projects завершена. Сохранено записей: %s", new_count)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def linkcheck_badge_payload(row):
    """Фирменный значок: автоматически при OFFICIAL RESOURCE или вручную администратором."""
    keys = set(row.keys()) if hasattr(row, "keys") else set(row or {})
    is_official = bool(row["is_official"]) if "is_official" in keys else False
    manual = bool(row["linkcheck_badge_manual"]) if "linkcheck_badge_manual" in keys else False
    source = "official" if is_official else ("manual" if manual else None)
    payload = {
        "linkcheck_badge": bool(is_official or manual),
        "linkcheck_badge_source": source,
        "linkcheck_badge_manual": manual,
    }
    if "linkcheck_badge_manual_by" in keys:
        payload["linkcheck_badge_manual_by"] = row["linkcheck_badge_manual_by"] or None
    if "linkcheck_badge_manual_at" in keys:
        payload["linkcheck_badge_manual_at"] = row["linkcheck_badge_manual_at"] or ""
    if "linkcheck_badge_manual_reason" in keys:
        payload["linkcheck_badge_manual_reason"] = row["linkcheck_badge_manual_reason"] or ""
    return payload


def write_moderation_log(conn, admin_tg_id, project_id, action, comment=None):
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(moderation_log)").fetchall()
    }
    values = {
        "admin_tg_id": admin_tg_id,
        "project_id": project_id,
        "action": action,
        "comment": comment,
        "created_at": now_iso()
    }
    usable = [(name, value) for name, value in values.items() if name in columns]
    if usable:
        names = ", ".join(name for name, _ in usable)
        placeholders = ", ".join("?" for _ in usable)
        conn.execute(
            f"INSERT INTO moderation_log ({names}) VALUES ({placeholders})",
            [value for _, value in usable]
        )


def canonical_project_link(raw_link, project_id=None):
    value = str(raw_link or "").strip()
    if not value:
        return f"__empty_link_{project_id or 'unknown'}"
    try:
        return normalize_link(value)
    except (ValueError, TypeError):
        return value.rstrip("/").lower()


def _first_nonempty(rows, column):
    for row in rows:
        value = row[column]
        if value is not None and str(value).strip():
            return value
    return None


def deduplicate_projects(conn):
    rows = conn.execute("""
        SELECT id, owner_tg_id, title, link, category, description, website,
               ownership_status, moderation_status, created_at, is_official,
               telegram_verified, source_url, is_seed, topic, community_type
        FROM projects ORDER BY id ASC
    """).fetchall()
    groups = {}
    for row in rows:
        canonical_link = canonical_project_link(row["link"], row["id"])
        groups.setdefault(canonical_link, []).append(row)

    duplicate_groups = removed_rows = normalized_rows = 0
    for canonical_link, group in groups.items():
        keeper = group[0]
        keeper_id = keeper["id"]
        owner_tg_id = keeper["owner_tg_id"]
        if owner_tg_id is None:
            owner_tg_id = next(
                (row["owner_tg_id"] for row in group if row["owner_tg_id"] is not None),
                None
            )

        ownership_statuses = {str(row["ownership_status"] or "").lower() for row in group}
        ownership_status = "verified" if "verified" in ownership_statuses else (
            keeper["ownership_status"] or _first_nonempty(group, "ownership_status") or "unverified"
        )
        moderation_statuses = {str(row["moderation_status"] or "").lower() for row in group}
        if "approved" in moderation_statuses:
            moderation_status = "approved"
        elif "pending" in moderation_statuses:
            moderation_status = "pending"
        elif "rejected" in moderation_statuses:
            moderation_status = "rejected"
        else:
            moderation_status = keeper["moderation_status"] or "pending"

        is_official = max(int(row["is_official"] or 0) for row in group)
        telegram_verified = max(int(row["telegram_verified"] or 0) for row in group)
        is_seed = max(int(row["is_seed"] or 0) for row in group)
        title = keeper["title"] or _first_nonempty(group, "title") or canonical_link
        category = keeper["category"] or _first_nonempty(group, "category")
        description = keeper["description"] or _first_nonempty(group, "description")
        website = keeper["website"] or _first_nonempty(group, "website") or ""
        source_url = _first_nonempty(group, "source_url")
        topic = _first_nonempty(group, "topic")
        community_type = _first_nonempty(group, "community_type")
        created_values = [str(row["created_at"]) for row in group if row["created_at"]]
        created_at = min(created_values) if created_values else now_iso()
        duplicate_ids = [row["id"] for row in group[1:]]

        if duplicate_ids:
            duplicate_groups += 1
            removed_rows += len(duplicate_ids)
            placeholders = ", ".join("?" for _ in duplicate_ids)
            reports_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'"
            ).fetchone()
            if reports_exists:
                report_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(reports)").fetchall()
                }
                if "project_id" in report_columns:
                    conn.execute(
                        f"UPDATE reports SET project_id=? WHERE project_id IN ({placeholders})",
                        [keeper_id, *duplicate_ids]
                    )
            log_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moderation_log'"
            ).fetchone()
            if log_exists:
                log_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(moderation_log)").fetchall()
                }
                if "project_id" in log_columns:
                    conn.execute(
                        f"UPDATE moderation_log SET project_id=? WHERE project_id IN ({placeholders})",
                        [keeper_id, *duplicate_ids]
                    )
            conn.execute(f"DELETE FROM projects WHERE id IN ({placeholders})", duplicate_ids)

        if str(keeper["link"] or "") != canonical_link:
            normalized_rows += 1
        conn.execute("""
            UPDATE projects SET owner_tg_id=?, title=?, link=?, category=?, description=?,
                website=?, ownership_status=?, moderation_status=?, created_at=?,
                is_official=?, telegram_verified=?, source_url=?, is_seed=?, topic=?,
                community_type=? WHERE id=?
        """, (
            owner_tg_id, title, canonical_link, category, description, website,
            ownership_status, moderation_status, created_at, is_official,
            telegram_verified, source_url, is_seed, topic, community_type, keeper_id
        ))

    if duplicate_groups or normalized_rows:
        logging.warning(
            "Очистка каталога завершена: групп дублей объединено=%s, строк удалено=%s, ссылок нормализовано=%s",
            duplicate_groups, removed_rows, normalized_rows
        )
    else:
        logging.info("Дубли проектов не обнаружены.")


def seed_catalog(conn):
    added = 0
    for project in SEED_PROJECTS:
        link = project["link"].lower()
        cursor = conn.execute("""
            INSERT OR IGNORE INTO projects
            (owner_tg_id, title, link, category, description, website,
             ownership_status, moderation_status, created_at, is_official,
             telegram_verified, source_url, is_seed, topic, community_type)
            VALUES (NULL, ?, ?, ?, ?, '', 'verified', 'approved', ?, ?, 0, ?, 1, ?, ?)
        """, (
            project["title"], link, project["category"], project["description"],
            now_iso(), project.get("is_official", 0), project.get("source_url"),
            project.get("topic"), project.get("community_type")
        ))
        if cursor.rowcount > 0:
            added += 1
    if added:
        logging.info("В стартовый каталог добавлено проектов: %s", added)


async def seed_public_catalog():
    with db_connect() as conn:
        already_imported = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE source_url=? AND is_seed=1",
            (PUBLIC_DATASET_PAGE,)
        ).fetchone()[0]
    if already_imported >= PUBLIC_DATASET_LIMIT:
        logging.info("Публичный каталог уже импортирован: %s ресурсов", already_imported)
        return

    timeout = aiohttp.ClientTimeout(total=90, connect=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                PUBLIC_DATASET_URL,
                headers={"User-Agent": "LINKCHECK/1.0", "Accept": "text/csv,*/*"}
            ) as response:
                if response.status != 200:
                    raise RuntimeError(f"Источник каталога вернул HTTP {response.status}")
                csv_text = await response.text()
        reader = csv.DictReader(io.StringIO(csv_text))
        if not reader.fieldnames:
            raise RuntimeError("В CSV отсутствуют заголовки")
        normalized_headers = {name.strip().lower() for name in reader.fieldnames if name}
        if "username" not in normalized_headers:
            raise RuntimeError("В CSV отсутствует обязательная колонка username")

        imported = skipped = 0
        with db_connect() as conn:
            for item in reader:
                if imported >= PUBLIC_DATASET_LIMIT:
                    break
                item = {
                    str(key).strip().lower(): value
                    for key, value in item.items() if key is not None
                }
                raw_username = str(item.get("username") or "").strip()
                username = raw_username.lstrip("@").strip()
                if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
                    skipped += 1
                    continue
                link = "@" + username.lower()
                title = str(item.get("title") or "").strip() or username
                title = title[:80]
                topic = str(item.get("topic") or "").strip() or None
                community_type = str(item.get("community_type") or "").strip() or None
                description = (
                    f"Публичный Telegram-канал. Тематика: {topic}." if topic
                    else "Публичный Telegram-канал."
                )
                if community_type:
                    description += f" Тип сообщества: {community_type}."
                description = description[:1000]
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO projects
                    (owner_tg_id, title, link, category, description, website,
                     ownership_status, moderation_status, created_at, is_official,
                     telegram_verified, source_url, is_seed, topic, community_type)
                    VALUES (NULL, ?, ?, 'channel', ?, '', 'unverified', 'approved', ?, 0, 0, ?, 1, ?, ?)
                """, (
                    title, link, description, now_iso(), PUBLIC_DATASET_PAGE,
                    topic, community_type
                ))
                if cursor.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1
        logging.info(
            "Импорт публичного каталога завершён: добавлено %s, пропущено %s",
            imported, skipped
        )
        if imported < PUBLIC_DATASET_LIMIT:
            logging.warning(
                "Импортировано меньше 1500 новых записей. Добавлено: %s. Возможно, часть username уже была в базе.",
                imported
            )
    except Exception as exc:
        logging.exception("Не удалось загрузить публичный каталог: %s", exc)


def init_db():
    with db_connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                is_trusted INTEGER NOT NULL DEFAULT 0,
                has_requested_access INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_tg_id INTEGER,
                title TEXT NOT NULL,
                link TEXT NOT NULL,
                category TEXT,
                description TEXT,
                website TEXT,
                ownership_status TEXT NOT NULL DEFAULT 'unverified',
                moderation_status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                is_official INTEGER NOT NULL DEFAULT 0,
                telegram_verified INTEGER NOT NULL DEFAULT 0,
                source_url TEXT,
                is_seed INTEGER NOT NULL DEFAULT 0,
                topic TEXT,
                community_type TEXT
            )
        """)
        ensure_column(conn, "projects", "owner_tg_id", "INTEGER")
        ensure_column(conn, "projects", "website", "TEXT")
        ensure_column(conn, "projects", "ownership_status", "TEXT NOT NULL DEFAULT 'unverified'")
        ensure_column(conn, "projects", "moderation_status", "TEXT NOT NULL DEFAULT 'pending'")
        ensure_column(conn, "projects", "is_official", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "projects", "telegram_verified", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "projects", "source_url", "TEXT")
        ensure_column(conn, "projects", "is_seed", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "projects", "topic", "TEXT")
        ensure_column(conn, "projects", "community_type", "TEXT")
        ensure_column(conn, "projects", "linkcheck_badge_manual", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "projects", "linkcheck_badge_manual_by", "INTEGER")
        ensure_column(conn, "projects", "linkcheck_badge_manual_at", "TEXT")
        ensure_column(conn, "projects", "linkcheck_badge_manual_reason", "TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ownership_challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                owner_tg_id INTEGER NOT NULL,
                method TEXT NOT NULL,
                domain TEXT,
                token_hash TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        ensure_column(conn, "ownership_challenges", "screenshot_file_id", "TEXT")
        ensure_column(conn, "ownership_challenges", "screenshot_data", "BLOB")
        ensure_column(conn, "ownership_challenges", "screenshot_mime", "TEXT")
        ensure_column(conn, "ownership_challenges", "screenshot_filename", "TEXT")
        ensure_column(conn, "ownership_challenges", "screenshot_uploaded_at", "TEXT")
        ensure_column(conn, "ownership_challenges", "manual_note", "TEXT")
        ensure_column(conn, "ownership_challenges", "reviewed_by", "INTEGER")
        ensure_column(conn, "ownership_challenges", "reviewed_at", "TEXT")
        ensure_column(conn, "ownership_challenges", "review_note", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ownership_project_status "
            "ON ownership_challenges(project_id, status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ownership_manual_queue "
            "ON ownership_challenges(method, status, created_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_tg_id INTEGER,
                project_id INTEGER,
                reason TEXT,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS moderation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_tg_id INTEGER,
                project_id INTEGER,
                action TEXT,
                comment TEXT,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS check_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_tg_id INTEGER NOT NULL,
                project_id INTEGER,
                link TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL,
                result_source TEXT,
                telegram_verified INTEGER NOT NULL DEFAULT 0,
                is_official INTEGER NOT NULL DEFAULT 0,
                linkcheck_badge INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_check_history_user_id "
            "ON check_history(user_tg_id, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_check_history_project_date "
            "ON check_history(project_id, checked_at DESC)"
        )
        # Older releases may have stored the same resource multiple times.
        # Keep only its latest row for each Telegram user before enforcing uniqueness.
        conn.execute("""
            DELETE FROM check_history
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM check_history
                GROUP BY user_tg_id, lower(trim(link))
            )
        """)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_check_history_user_link "
            "ON check_history(user_tg_id, lower(trim(link)))"
        )
        for col, definition in (
            ("admin_tg_id", "INTEGER"), ("project_id", "INTEGER"),
            ("action", "TEXT"), ("comment", "TEXT"), ("created_at", "TEXT")
        ):
            ensure_column(conn, "moderation_log", col, definition)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_moderation_log_admin_id "
            "ON moderation_log(admin_tg_id, id DESC)"
        )
        for col, definition in (
            ("author_tg_id", "INTEGER"), ("project_id", "INTEGER"),
            ("reason", "TEXT"), ("description", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'pending'"),
            ("created_at", "TEXT")
        ):
            ensure_column(conn, "reports", col, definition)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_status_created "
            "ON reports(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_author_project "
            "ON reports(author_tg_id, project_id, status)"
        )
        ensure_column(conn, "users", "is_trusted", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "users", "has_requested_access", "INTEGER NOT NULL DEFAULT 0")
        migrate_projects_owner_nullable(conn)
        deduplicate_projects(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS official_verification_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                requester_tg_id INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                note TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                automatic_status TEXT NOT NULL DEFAULT 'unavailable',
                automatic_detail TEXT,
                http_status INTEGER,
                checked_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                review_note TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_official_requests_status_created "
            "ON official_verification_requests(status, created_at, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_official_requests_requester_created "
            "ON official_verification_requests(requester_tg_id, created_at)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_official_pending_project "
            "ON official_verification_requests(project_id) WHERE status='pending'"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_link ON projects(link)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_tg_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(moderation_status, ownership_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_catalog ON projects(moderation_status, is_official, telegram_verified)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_link_unique ON projects(lower(link))"
        )
        # LINKCHECK Plus: сохранённые проекты, уведомления, журнал изменений и аналитика.
        # Все таблицы добавляются без изменения существующих пользовательских записей.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_watchlist (
                user_tg_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_tg_id, project_id),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_watchlist_project "
            "ON project_watchlist(project_id, user_tg_id)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_watch_snapshots (
                project_id INTEGER PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_change_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                changed_fields TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_change_history_project "
            "ON project_change_history(project_id, id DESC)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_tg_id INTEGER NOT NULL,
                project_id INTEGER,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                read_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_read "
            "ON notifications(recipient_tg_id, read_at, id DESC)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                viewer_tg_id INTEGER NOT NULL,
                viewed_at TEXT NOT NULL,
                view_day TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                UNIQUE(project_id, viewer_tg_id, view_day)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_views_project_date "
            "ON project_views(project_id, viewed_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_project_views_viewer "
            "ON project_views(viewer_tg_id, viewed_at DESC)"
        )
        # LINKCHECK PLUS: подписки, платежи Telegram Stars и Telegram Business.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_subscriptions (
                tg_id INTEGER PRIMARY KEY,
                plan_code TEXT,
                started_at TEXT,
                expires_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_invoice_intents (
                payload TEXT PRIMARY KEY,
                tg_id INTEGER NOT NULL,
                plan_code TEXT NOT NULL,
                amount_stars INTEGER NOT NULL,
                duration_days INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        ensure_column(conn, "plus_invoice_intents", "duration_days", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_plan_settings (
                plan_code TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                days INTEGER NOT NULL,
                base_stars INTEGER NOT NULL,
                discount_percent INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_trial_claims (
                tg_id INTEGER PRIMARY KEY,
                claimed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        for _code, _plan in {
            "1m": ("1 месяц", 30, 200, 0),
            "6m": ("6 месяцев", 180, 1000, 17),
            "12m": ("12 месяцев", 365, 1800, 25),
        }.items():
            conn.execute(
                "INSERT OR IGNORE INTO plus_plan_settings(plan_code,title,days,base_stars,discount_percent,updated_at) VALUES(?,?,?,?,?,?)",
                (_code, _plan[0], _plan[1], _plan[2], _plan[3], now_iso())
            )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL,
                plan_code TEXT NOT NULL,
                amount_stars INTEGER NOT NULL,
                currency TEXT NOT NULL,
                telegram_payment_charge_id TEXT NOT NULL UNIQUE,
                provider_payment_charge_id TEXT,
                paid_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plus_payments_user "
            "ON plus_payments(tg_id, paid_at DESC)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_business_connections (
                connection_id TEXT PRIMARY KEY,
                owner_tg_id INTEGER NOT NULL,
                user_chat_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                rights_json TEXT,
                connected_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plus_business_messages (
                connection_id TEXT NOT NULL,
                owner_tg_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                message_date TEXT,
                chat_title TEXT,
                sender_label TEXT,
                text_content TEXT,
                caption TEXT,
                media_type TEXT,
                media_file_id TEXT,
                is_outgoing INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(connection_id, chat_id, message_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plus_business_messages_owner "
            "ON plus_business_messages(owner_tg_id, updated_at DESC)"
        )
        # Старые сохранённые сообщения не храним дольше 30 дней.
        conn.execute(
            "DELETE FROM plus_business_messages WHERE updated_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),)
        )
        seed_catalog(conn)

# ============================================================
# ОБЩИЕ ФУНКЦИИ И АВТОРИЗАЦИЯ
# ============================================================

def upsert_user(tg_id: int, username: str | None):
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO users (tg_id, username, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username
        """, (tg_id, username, now_iso()))


def validate_telegram_init_data(init_data: str):
    if not init_data or len(init_data) > 16_384:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            return None
        data = dict(pairs)
        received_hash = data.pop("hash", None)
        if not received_hash or not re.fullmatch(r"[0-9a-fA-F]{64}", received_hash):
            return None
        auth_date = int(data.get("auth_date", "0"))
        current = int(time.time())
        if auth_date <= 0 or auth_date > current + 60 or current - auth_date > INIT_DATA_MAX_AGE:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash.lower()):
            return None
        user_raw = data.get("user")
        if not user_raw:
            return None
        user = json.loads(user_raw)
        user_id = user.get("id")
        if not isinstance(user_id, int) or user_id <= 0:
            return None
        return {"id": user_id, "username": user.get("username")}
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def authenticated_user(request):
    auth = request.headers.get("Authorization", "")
    init_data = (
        auth[4:].strip() if auth.startswith("tma ")
        else request.headers.get("X-Telegram-Init-Data", "")
    )
    return validate_telegram_init_data(init_data)


def json_error(message, status=400):
    return web.json_response({"error": message}, status=status)


async def read_json(request):
    try:
        data = await request.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (json.JSONDecodeError, ValueError, web.HTTPException):
        raise web.HTTPBadRequest(text="Некорректный JSON")


def normalize_link(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 300:
        raise ValueError("Укажите корректную ссылку или username")
    value = raw.strip()
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", value):
        username = value
    elif value.startswith("@"):
        username = value[1:]
    else:
        candidate = value
        if candidate.startswith(("t.me/", "www.t.me/", "telegram.me/", "www.telegram.me/")):
            candidate = "https://" + candidate
        elif not candidate.startswith(("http://", "https://")):
            raise ValueError("Введите username, @username или ссылку t.me/username")
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or host not in {
            "t.me", "www.t.me", "telegram.me", "www.telegram.me"
        }:
            raise ValueError("Поддерживаются только username и ссылки t.me")
        if parsed.query or parsed.fragment:
            raise ValueError("Укажите публичную ссылку без параметров")
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) != 1:
            raise ValueError("Укажите публичный username без дополнительных путей")
        username = parts[0]
    username = username.strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        raise ValueError("Некорректный Telegram username")
    return "@" + username


def valid_website(value):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("Некорректный адрес сайта")

    normalized = value.strip()
    if not normalized or any(char.isspace() for char in normalized):
        raise ValueError("Некорректный адрес сайта")

    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        # Не разрешаем URL с учётными данными и нестандартными схемами.
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Сайт должен начинаться с https:// или http://")
        # urlsplit сам по себе не проверяет корректность номера порта.
        if parsed.port is not None and not (1 <= parsed.port <= 65535):
            raise ValueError("Некорректный адрес сайта")
    except ValueError as exc:
        if "Сайт должен начинаться" in str(exc):
            raise
        raise ValueError("Некорректный адрес сайта") from exc

    return normalized


class UnsafeOfficialSourceError(Exception):
    """Сайт ведёт на локальный, служебный или иной недопустимый адрес."""


def official_source_contains_telegram_link(page_text, telegram_link):
    """Ищет в HTML ссылку именно на username проверяемого Telegram-ресурса."""
    username = str(telegram_link or "").strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        return False
    normalized = html.unescape(str(page_text or "")).replace("\\/", "/")
    normalized = unquote(normalized)
    pattern = re.compile(
        rf"(?:https?:)?//(?:www\.)?(?:t\.me|telegram\.me)/{re.escape(username)}(?:[/\?#\"'\s<>]|$)",
        re.IGNORECASE,
    )
    return bool(pattern.search(normalized))


class PinnedPublicResolver(aiohttp.abc.AbstractResolver):
    """Разрешает только публичные IP и закрепляет DNS-ответ на время запроса."""

    def __init__(self):
        self._cache = {}

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        cache_key = (str(host).lower(), int(port))
        if cache_key in self._cache:
            return [dict(item) for item in self._cache[cache_key]]

        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
        except (OSError, socket.gaierror) as exc:
            raise OSError("Не удалось определить IP-адрес сайта") from exc

        resolved = []
        seen = set()
        for addr_family, socktype, proto, _canonname, sockaddr in records:
            ip_text = str(sockaddr[0]).split("%", 1)[0]
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except ValueError as exc:
                raise UnsafeOfficialSourceError("DNS сайта вернул некорректный IP") from exc
            if not ip_obj.is_global:
                raise UnsafeOfficialSourceError(
                    "Источник разрешается в локальный или служебный IP-адрес"
                )
            marker = (ip_text, addr_family, proto)
            if marker in seen:
                continue
            seen.add(marker)
            resolved.append({
                "hostname": str(host),
                "host": ip_text,
                "port": int(port),
                "family": addr_family,
                "proto": proto,
                "flags": 0,
            })

        if not resolved:
            raise OSError("У сайта нет доступных публичных IP-адресов")
        self._cache[cache_key] = [dict(item) for item in resolved]
        return [dict(item) for item in resolved]

    async def close(self):
        self._cache.clear()


async def check_official_source_url(source_url, telegram_link):
    """Проверяет публичную HTML-страницу на ссылку на конкретный Telegram-ресурс.

    Результат — только техническая подсказка для администратора, не решение об officialness.
    Запросы ограничены публичными IP, стандартными портами, редиректами и размером ответа.
    """
    try:
        normalized = valid_website(source_url)
        parsed = urlsplit(normalized)
        if parsed.scheme.lower() not in {"http", "https"}:
            return {
                "status": "blocked", "detail": "Разрешены только HTTP- и HTTPS-источники.",
                "http_status": None,
            }
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        if port not in {80, 443}:
            return {
                "status": "blocked", "detail": "Проверка разрешена только на стандартных портах 80 и 443.",
                "http_status": None,
            }
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if not hostname or hostname in {"localhost"} or hostname.endswith((".localhost", ".local", ".internal")):
            return {
                "status": "blocked", "detail": "Локальные и внутренние домены проверять нельзя.",
                "http_status": None,
            }
        current_url = normalized.split("#", 1)[0]
        username = str(telegram_link or "").strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
            return {
                "status": "unavailable", "detail": "Не удалось определить username Telegram-ресурса.",
                "http_status": None,
            }

        resolver = PinnedPublicResolver()
        timeout = aiohttp.ClientTimeout(total=9, connect=4, sock_read=5)
        connector = aiohttp.TCPConnector(
            resolver=resolver, use_dns_cache=False, limit=2,
            family=socket.AF_UNSPEC, enable_cleanup_closed=True
        )
        headers = {
            "User-Agent": "LINKCHECK-OfficialSourceCheck/1.0",
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
            "Accept-Encoding": "gzip, deflate",
        }
        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers,
            trust_env=False, raise_for_status=False
        ) as session:
            last_status = None
            for redirect_number in range(6):
                parsed_current = urlsplit(current_url)
                if parsed_current.scheme.lower() not in {"http", "https"}:
                    return {
                        "status": "blocked", "detail": "Редирект ведёт на недопустимую схему URL.",
                        "http_status": last_status,
                    }
                current_port = parsed_current.port or (443 if parsed_current.scheme.lower() == "https" else 80)
                if current_port not in {80, 443}:
                    return {
                        "status": "blocked", "detail": "Редирект ведёт на нестандартный порт.",
                        "http_status": last_status,
                    }
                current_host = (parsed_current.hostname or "").rstrip(".").lower()
                if (
                    not current_host or current_host == "localhost"
                    or current_host.endswith((".localhost", ".local", ".internal"))
                    or parsed_current.username is not None
                    or parsed_current.password is not None
                ):
                    return {
                        "status": "blocked", "detail": "Редирект ведёт на локальный домен или содержит учётные данные.",
                        "http_status": last_status,
                    }
                # Выполняем DNS-проверку до HTTP-запроса; resolver закрепляет найденные IP.
                await resolver.resolve(current_host, current_port, socket.AF_UNSPEC)
                async with session.get(current_url, allow_redirects=False) as response:
                    last_status = int(response.status)
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location", "").strip()
                        if not location:
                            return {
                                "status": "unavailable", "detail": "Сайт вернул редирект без адреса назначения.",
                                "http_status": last_status,
                            }
                        if redirect_number >= 5:
                            return {
                                "status": "unavailable", "detail": "Слишком много перенаправлений (более 5).",
                                "http_status": last_status,
                            }
                        current_url = urljoin(current_url, location)
                        continue

                    if response.status < 200 or response.status >= 300:
                        return {
                            "status": "unavailable",
                            "detail": f"Сайт ответил HTTP {response.status}; содержимое не проверено.",
                            "http_status": last_status,
                        }

                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        return {
                            "status": "unsupported",
                            "detail": f"Источник доступен, но вернул формат {content_type or 'неизвестный'}, а не HTML.",
                            "http_status": last_status,
                        }

                    body = bytearray()
                    truncated = False
                    async for chunk in response.content.iter_chunked(65536):
                        remaining = 1_000_000 - len(body)
                        if remaining <= 0:
                            truncated = True
                            break
                        body.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            truncated = True
                            break
                    charset = response.charset or "utf-8"
                    page_text = bytes(body).decode(charset, errors="replace")
                    if official_source_contains_telegram_link(page_text, username):
                        return {
                            "status": "linked",
                            "detail": f"В HTML-странице найдена ссылка на t.me/{username}. Это техническое совпадение, а не автоматическое подтверждение официальности.",
                            "http_status": last_status,
                        }
                    if truncated:
                        return {
                            "status": "partial",
                            "detail": "Просканирован только первый 1 МБ HTML; ссылка не найдена. Нужна ручная проверка.",
                            "http_status": last_status,
                        }
                    return {
                        "status": "no_link",
                        "detail": f"Страница доступна, но ссылка на t.me/{username} в её HTML не найдена.",
                        "http_status": last_status,
                    }

        return {
            "status": "unavailable", "detail": "Не удалось получить HTML-страницу источника.",
            "http_status": last_status,
        }
    except UnsafeOfficialSourceError as exc:
        return {"status": "blocked", "detail": str(exc), "http_status": None}
    except (asyncio.TimeoutError, aiohttp.ClientError, OSError, ValueError) as exc:
        logging.info("Official source check failed for %s: %s", source_url, type(exc).__name__)
        return {
            "status": "unavailable",
            "detail": "Не удалось автоматически открыть источник. Администратор может проверить его вручную.",
            "http_status": None,
        }
    except Exception:
        logging.exception("Unexpected official source check error")
        return {
            "status": "unavailable", "detail": "Автоматическая проверка завершилась ошибкой; требуется ручная проверка.",
            "http_status": None,
        }

# ============================================================
# FLOODWAIT И ЕДИНАЯ ОЧЕРЕДЬ MTPROTO
# ============================================================

def is_flood_wait_error(exc):
    name = type(exc).__name__
    return (
        name in {"FloodWaitError", "FloodTestPhoneWaitError"}
        or "FloodWait" in name
        or bool(re.search(r"FLOOD_WAIT_\d+", str(exc), re.IGNORECASE))
    )


def get_flood_wait_seconds(exc):
    seconds = getattr(exc, "seconds", None)
    if seconds is None:
        seconds = getattr(exc, "value", None)
    if seconds is not None:
        try:
            return max(1, int(seconds))
        except (TypeError, ValueError):
            pass
    match = re.search(r"FLOOD_WAIT_(\d+)", str(exc), re.IGNORECASE)
    return max(1, int(match.group(1))) if match else 60


def register_telegram_flood_wait(exc, source="Telegram"):
    global telegram_flood_wait_until
    if not is_flood_wait_error(exc):
        return 0
    seconds = get_flood_wait_seconds(exc)
    telegram_flood_wait_until = max(
        telegram_flood_wait_until,
        time.monotonic() + seconds
    )
    logging.warning(
        "%s FloodWait: %s сек. Новые MTProto-запросы будут приостановлены; публичная проверка t.me продолжает работать.",
        source, seconds
    )
    return seconds


def telegram_flood_remaining():
    return max(0, telegram_flood_wait_until - time.monotonic())


async def run_mtproto_operation(operation, source="Telegram"):
    """Одна общая очередь MTProto с соблюдением FloodWait и ограниченным retry."""
    global telegram_last_mtproto_request_at
    if not tg_ready or tg_client is None:
        raise RuntimeError("Telegram client is not ready")

    async with telegram_mtproto_lock:
        attempts = 0
        while True:
            remaining = telegram_flood_remaining()
            if remaining > 0:
                raise RuntimeError(
                    f"Telegram FloodWait active: {math.ceil(remaining)} seconds"
                )

            elapsed = time.monotonic() - telegram_last_mtproto_request_at
            delay = TELEGRAM_MTPROTO_MIN_INTERVAL - elapsed
            if delay > 0:
                await asyncio.sleep(delay)

            remaining = telegram_flood_remaining()
            if remaining > 0:
                raise RuntimeError(
                    f"Telegram FloodWait active: {math.ceil(remaining)} seconds"
                )

            try:
                result = await operation()
                telegram_last_mtproto_request_at = time.monotonic()
                return result
            except Exception as exc:
                telegram_last_mtproto_request_at = time.monotonic()
                if not is_flood_wait_error(exc):
                    raise

                seconds = register_telegram_flood_wait(exc, source)
                if attempts == 0 and 0 < seconds <= TELEGRAM_AUTO_WAIT_MAX:
                    attempts += 1
                    logging.info(
                        "%s: соблюдаю FloodWait %s сек. и повторяю запрос один раз.",
                        source, seconds
                    )
                    await asyncio.sleep(seconds + 1)
                    continue

                # Long waits are not retried or bypassed. The API returns an
                # accurate retry_after to the Mini App.
                raise

# ============================================================
# КЭШ РЕЗУЛЬТАТОВ ПРОВЕРКИ
# ============================================================

def telegram_unavailable_result(reason=None, retry_after=0):
    retry_after = max(0, int(retry_after or 0))
    if not reason:
        reason = "Telegram временно недоступен. Не удалось получить достоверный результат."
    return {
        "status": "unavailable", "verified": False, "member_count": None,
        "reason": reason, "retry_after": retry_after, "verification_source": None
    }


def is_telegram_not_found_error(exc):
    name = type(exc).__name__
    if name in {
        "UsernameNotOccupiedError", "UsernameInvalidError",
        "UsernamePurchaseAvailableError"
    }:
        return True
    error_text = str(exc).upper()
    if any(token in error_text for token in (
        "USERNAME_NOT_OCCUPIED", "USERNAME_INVALID", "USERNAME_PURCHASE_AVAILABLE"
    )):
        return True
    if name == "ValueError":
        message = str(exc).lower()
        return any(phrase in message for phrase in (
            "no user has", "no entity corresponds", "cannot find any entity",
            "username not found", "could not find the input entity"
        ))
    return False


def cache_telegram_check(key, result, ttl):
    telegram_check_cache[key] = {
        "result": dict(result), "expires_at": time.monotonic() + ttl
    }


def get_cached_telegram_check(key):
    cached = telegram_check_cache.get(key)
    if not cached:
        return None
    if time.monotonic() >= cached["expires_at"]:
        telegram_check_cache.pop(key, None)
        return None
    return dict(cached["result"])


def cache_result_by_status(key, result):
    status = result.get("status")
    if status == "exists":
        cache_telegram_check(key, result, TELEGRAM_CHECK_CACHE_TTL)
    elif status == "not_found":
        cache_telegram_check(key, result, TELEGRAM_NOT_FOUND_CACHE_TTL)
    elif status == "wrong_type":
        cache_telegram_check(key, result, TELEGRAM_CHECK_CACHE_TTL)
    elif status == "unavailable":
        cache_telegram_check(key, result, TELEGRAM_UNAVAILABLE_CACHE_TTL)

# ============================================================
# ОПРЕДЕЛЕНИЕ ENTITY И ПРОВЕРКА TELEGRAM
# ============================================================

def extract_resolved_telegram_entity(resolved):
    peer = getattr(resolved, "peer", None)
    if peer is None:
        return None
    user_id = getattr(peer, "user_id", None)
    if user_id is not None:
        return next((
            entity for entity in (getattr(resolved, "users", None) or [])
            if getattr(entity, "id", None) == user_id
        ), None)
    channel_id = getattr(peer, "channel_id", None)
    if channel_id is not None:
        return next((
            entity for entity in (getattr(resolved, "chats", None) or [])
            if getattr(entity, "id", None) == channel_id
        ), None)
    chat_id = getattr(peer, "chat_id", None)
    if chat_id is not None:
        return next((
            entity for entity in (getattr(resolved, "chats", None) or [])
            if getattr(entity, "id", None) == chat_id
        ), None)
    return None


def detect_telegram_entity_type(entity):
    if User is not None and isinstance(entity, User):
        return "bot" if bool(getattr(entity, "bot", False)) else "user"
    if Channel is not None and isinstance(entity, Channel):
        if bool(getattr(entity, "megagroup", False)) or bool(getattr(entity, "gigagroup", False)):
            return "group"
        if bool(getattr(entity, "broadcast", False)):
            return "channel"
        return "unknown"
    if Chat is not None and isinstance(entity, Chat):
        return "group"
    return "unknown"


async def get_telegram_member_count(entity, entity_type):
    """Не делает дополнительный MTProto-запрос; возвращает только готовое поле entity."""
    if entity_type not in {"channel", "group"}:
        return None
    count = getattr(entity, "participants_count", None)
    if count is None:
        return None
    try:
        count = int(count)
        return count if count >= 0 else None
    except (TypeError, ValueError):
        return None


def clean_telegram_page_text(value):
    # Удаляет HTML-разметку и нормализует текст фрагмента t.me.
    value = re.sub(r"(?is)<br\s*/?>", " ", str(value or ""))
    value = re.sub(r"(?is)</(?:div|span|a|b|strong|i|p)>\s*<", " <", value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_telegram_public_page(page_html, username):
    # Разбирает публичную карточку Telegram; не делает выводов без tgme_page_title.
    title_match = re.search(
        r"(?is)<div\b[^>]*class=[\"'][^\"']*\btgme_page_title\b[^\"']*[\"'][^>]*>(.*?)</div>",
        page_html
    )
    if not title_match:
        return None

    title_html = title_match.group(1)
    extra_match = re.search(
        r"(?is)<div\b[^>]*class=[\"'][^\"']*\btgme_page_extra\b[^\"']*[\"'][^>]*>(.*?)</div>",
        page_html
    )
    action_match = re.search(
        r"(?is)<div\b[^>]*class=[\"'][^\"']*\btgme_page_action\b[^\"']*[\"'][^>]*>(.*?)</div>",
        page_html
    )
    title = clean_telegram_page_text(title_html)
    extra = clean_telegram_page_text(extra_match.group(1) if extra_match else "")
    action = clean_telegram_page_text(action_match.group(1) if action_match else "")
    if not title:
        return None

    # Именно verified-icon — штатная галочка Telegram в публичной карточке.
    # Сторонние/кастомные значки не считаются Telegram Verified.
    verified = bool(re.search(
        r"(?is)<i\b[^>]*class=[\"'][^\"']*\bverified-icon\b[^\"']*[\"']",
        title_html
    ))

    extra_lower = extra.casefold()
    action_lower = action.casefold()
    if "monthly user" in extra_lower or "start bot" in action_lower:
        entity_type = "bot"
    elif "subscriber" in extra_lower:
        entity_type = "channel"
    elif "member" in extra_lower:
        entity_type = "group"
    else:
        entity_type = "user"

    member_count = None
    if entity_type in {"channel", "group"}:
        count_match = re.search(
            r"([0-9][0-9\s,\.\u00a0]*\s*[kKmM]?)\s+(?:subscribers?|members?)",
            extra,
            re.IGNORECASE
        )
        if count_match:
            token = re.sub(r"[\s,\u00a0]", "", count_match.group(1)).strip()
            multiplier = 1
            if token[-1:].casefold() == "k":
                multiplier, token = 1_000, token[:-1]
            elif token[-1:].casefold() == "m":
                multiplier, token = 1_000_000, token[:-1]
            try:
                member_count = int(float(token) * multiplier)
            except (TypeError, ValueError, OverflowError):
                member_count = None

    return {
        "status": "exists",
        "type": entity_type,
        "verified": verified,
        "member_count": member_count,
        "reason": "",
        "retry_after": 0,
        "verification_source": "telegram_public_page",
        "title": title,
    }


async def check_telegram_public_page(username):
    # Проверяет общедоступную карточку t.me без использования MTProto.
    global telegram_public_page_last_request_at

    now = time.monotonic()
    cached = telegram_public_page_cache.get(username)
    if cached and now < cached["expires_at"]:
        return dict(cached["result"])
    if cached:
        telegram_public_page_cache.pop(username, None)

    async with telegram_public_page_lock:
        now = time.monotonic()
        cached = telegram_public_page_cache.get(username)
        if cached and now < cached["expires_at"]:
            return dict(cached["result"])
        if cached:
            telegram_public_page_cache.pop(username, None)

        delay = TELEGRAM_PUBLIC_PAGE_MIN_INTERVAL - (now - telegram_public_page_last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)

        timeout = aiohttp.ClientTimeout(
            total=TELEGRAM_PUBLIC_PAGE_TIMEOUT,
            connect=5,
            sock_read=8
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
        telegram_public_page_last_request_at = time.monotonic()
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(
                    f"https://t.me/{username}",
                    allow_redirects=True,
                    max_redirects=5
                ) as response:
                    if response.status == 404:
                        result = {
                            "status": "not_found", "type": None, "verified": False,
                            "member_count": None,
                            "reason": "Такой публичный username не найден в Telegram.",
                            "retry_after": 0,
                            "verification_source": "telegram_public_page",
                        }
                        telegram_public_page_cache[username] = {
                            "result": dict(result),
                            "expires_at": time.monotonic() + TELEGRAM_PUBLIC_PAGE_NOT_FOUND_TTL,
                        }
                        return result

                    if response.status == 429:
                        retry_header = response.headers.get("Retry-After", "")
                        try:
                            retry_after = max(1, int(retry_header))
                        except (TypeError, ValueError):
                            retry_after = 60
                        return telegram_unavailable_result(
                            "Публичная страница Telegram временно ограничила запросы.",
                            retry_after
                        )

                    if response.status != 200:
                        logging.info(
                            "Telegram public page fallback needed for @%s: HTTP %s",
                            username, response.status
                        )
                        return None

                    page_html = await response.text(errors="replace")
                    result = parse_telegram_public_page(page_html, username)
                    if result is None:
                        logging.info(
                            "Telegram public page markup was not recognized for @%s; trying MTProto cache fallback",
                            username
                        )
                        return None

                    telegram_public_page_cache[username] = {
                        "result": dict(result),
                        "expires_at": time.monotonic() + TELEGRAM_PUBLIC_PAGE_CACHE_TTL,
                    }
                    return result
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
            logging.info(
                "Telegram public page check failed for @%s: %s",
                username, type(exc).__name__
            )
            return None


async def check_telegram_resource(link, expected_category="other"):
    username = str(link or "").strip().lstrip("@").lower()
    if not username:
        return {
            "status": "not_found", "type": None, "verified": False,
            "member_count": None, "reason": "Не указан Telegram username.",
            "retry_after": 0, "verification_source": None
        }
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        return {
            "status": "not_found", "type": None, "verified": False,
            "member_count": None, "reason": "Некорректный Telegram username.",
            "retry_after": 0, "verification_source": None
        }

    cache_key = (username, expected_category)
    cached = get_cached_telegram_check(cache_key)
    if cached is not None:
        return cached

    async with telegram_check_lock:
        cached = get_cached_telegram_check(cache_key)
        if cached is not None:
            return cached

        # Основной путь: публичная страница самого Telegram. Он не вызывает
        # contacts.ResolveUsername и продолжает работать во время MTProto FloodWait.
        public_result = await check_telegram_public_page(username)
        if public_result is not None and public_result.get("status") in {"exists", "not_found"}:
            if (
                public_result.get("status") == "exists"
                and expected_category != "other"
                and public_result.get("type") != expected_category
            ):
                result = {
                    **public_result,
                    "status": "wrong_type",
                    "reason": (
                        "Username существует в Telegram, но тип ресурса "
                        f"не соответствует выбранной категории «{expected_category}»."
                    ),
                }
            else:
                result = dict(public_result)
            logging.info(
                "Проверка @%s выполнена через публичную страницу Telegram: status=%s type=%s verified=%s",
                username, result.get("status"), result.get("type"), result.get("verified")
            )
            cache_result_by_status(cache_key, result)
            return result

        # Если web preview временно недоступен/неизвестен, используем Telethon.
        # В первую очередь читаем entity из локальной session-кэша. В отличие от
        # ResolveUsernameRequest, повторное чтение entity не разрешает username
        # через Telegram каждый раз.
        if not tg_ready or tg_client is None or functions is None:
            if not TG_API_ID or not TG_API_HASH:
                reason = "Публичная страница Telegram не ответила, а серверная проверка MTProto не настроена: проверьте TG_API_ID и TG_API_HASH в .env."
            elif TelegramClient is None or functions is None:
                reason = "Публичная страница Telegram не ответила, а модуль Telethon недоступен."
            else:
                reason = "Публичная страница Telegram не ответила, и подключение MTProto не готово. Проверьте журнал запуска."
            result = telegram_unavailable_result(reason, 0)
            cache_result_by_status(cache_key, result)
            return result

        remaining = telegram_flood_remaining()
        if remaining > 0:
            result = telegram_unavailable_result(
                "Публичная страница Telegram не вернула результат, а MTProto соблюдает установленную Telegram паузу.",
                math.ceil(remaining)
            )
            cache_result_by_status(cache_key, result)
            return result

        try:
            input_entity = await run_mtproto_operation(
                lambda: tg_client.get_input_entity(username),
                "Telegram username cache lookup"
            )
            entity = await run_mtproto_operation(
                lambda: tg_client.get_entity(input_entity),
                "Telegram entity fetch"
            )
            entity_type = detect_telegram_entity_type(entity)
            member_count = await get_telegram_member_count(entity, entity_type)
            result = {
                "status": "exists",
                "type": entity_type,
                "verified": bool(getattr(entity, "verified", False)),
                "member_count": member_count,
                "reason": "",
                "retry_after": 0,
                "verification_source": "mtproto_cached_entity",
            }
            if expected_category != "other" and entity_type != expected_category:
                result["status"] = "wrong_type"
                result["reason"] = (
                    "Username существует в Telegram, но тип ресурса "
                    f"не соответствует выбранной категории «{expected_category}»."
                )
            cache_result_by_status(cache_key, result)
            return result
        except Exception as exc:
            if is_flood_wait_error(exc):
                seconds = register_telegram_flood_wait(exc, "Telegram entity lookup")
                result = telegram_unavailable_result(
                    "Telegram временно ограничил MTProto-проверку. Публичная страница Telegram не вернула достоверный ответ.",
                    seconds
                )
                cache_result_by_status(cache_key, result)
                return result
            if is_telegram_not_found_error(exc):
                result = {
                    "status": "not_found", "type": None, "verified": False,
                    "member_count": None,
                    "reason": "Такой публичный username не найден в Telegram.",
                    "retry_after": 0, "verification_source": "mtproto_cached_entity"
                }
                cache_result_by_status(cache_key, result)
                return result
            remaining = telegram_flood_remaining()
            logging.warning(
                "Telegram username check failed for @%s: %s: %s",
                username, type(exc).__name__, exc
            )
            if remaining > 0:
                retry_after = math.ceil(remaining)
                reason = "Telegram временно ограничил MTProto. LINKCHECK соблюдает паузу Telegram."
            elif "client is not ready" in str(exc).lower():
                retry_after = 0
                reason = "Подключение к Telegram ещё не готово. Проверьте состояние сервера."
            else:
                retry_after = 0
                reason = "Telegram не вернул достоверный ответ. Это не означает, что ресурс не существует."
            result = telegram_unavailable_result(reason, retry_after)
            cache_result_by_status(cache_key, result)
            return result


async def verify_seed_projects():
    """Сохранена для отдельного ручного запуска; автоматически не запускается."""
    if not tg_ready or tg_client is None:
        logging.warning("Проверка стартовых проектов пропущена: MTProto недоступен.")
        return
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT id, link, category FROM projects
            WHERE is_seed=1 AND COALESCE(source_url, '') != ?
        """, (PUBLIC_DATASET_PAGE,)).fetchall()
    total = len(rows)
    checked = 0
    for row in rows:
        try:
            result = await check_telegram_resource(row["link"], row["category"])
            if result["status"] == "exists":
                with db_connect() as conn:
                    conn.execute(
                        "UPDATE projects SET telegram_verified=? WHERE id=? AND is_seed=1",
                        (1 if result.get("verified") else 0, row["id"])
                    )
                checked += 1
            elif result["status"] == "not_found":
                logging.warning("Seed resource not found: %s", row["link"])
            elif result["status"] == "unavailable":
                logging.warning(
                    "Seed verification остановлена: Telegram временно недоступен. Проверено %s из %s.",
                    checked, total
                )
                break
            await asyncio.sleep(2)
        except Exception:
            logging.exception("Seed verification failed for %s", row["link"])
    logging.info(
        "Проверка стартового каталога завершена: успешно проверено %s из %s",
        checked, total
    )

# ============================================================
# ПРОФИЛЬ СОЗДАТЕЛЯ И КОЛЛЕКЦИОННЫЙ СТАТУС
# ============================================================

def is_collectible_status(status):
    if status is None:
        return False
    if EmojiStatusCollectible is not None:
        try:
            if isinstance(status, EmojiStatusCollectible):
                return True
        except TypeError:
            pass
    return type(status).__name__ == "EmojiStatusCollectible"


async def get_founder_entity(force=False):
    """Получает свежий профиль по постоянному Telegram ID, а не по старому username."""
    global founder_entity_cache, founder_entity_cache_time
    if not tg_ready or tg_client is None or functions is None:
        if founder_entity_cache is not None and getattr(founder_entity_cache, "id", None) == FOUNDER_ID:
            return founder_entity_cache
        raise RuntimeError("Telegram client is not ready")

    async with founder_entity_lock:
        cache_age = time.time() - founder_entity_cache_time if founder_entity_cache else None
        if (
            not force and founder_entity_cache is not None
            and getattr(founder_entity_cache, "id", None) == FOUNDER_ID
            and cache_age is not None and cache_age < FOUNDER_PROFILE_CACHE_TTL
        ):
            return founder_entity_cache

        remaining = telegram_flood_remaining()
        if remaining > 0:
            if founder_entity_cache is not None and getattr(founder_entity_cache, "id", None) == FOUNDER_ID:
                return founder_entity_cache
            raise RuntimeError(f"Telegram FloodWait active: {math.ceil(remaining)} seconds")

        try:
            entity = None
            resolve_errors = []

            # Telethon session already knows the stable numeric ID and access hash.
            # Resolve by ID first so a username change does not break the profile.
            try:
                input_peer = await tg_client.get_input_entity(FOUNDER_ID)
                entity = await run_mtproto_operation(
                    lambda: tg_client.get_entity(input_peer),
                    "Founder profile resolve by ID"
                )
            except Exception as exc:
                resolve_errors.append(exc)
                cached_username = ""
                if isinstance(founder_profile_cache, dict):
                    cached_username = str(founder_profile_cache.get("username") or "").strip().lstrip("@")
                candidates = []
                for candidate in (cached_username, FOUNDER_FALLBACK_USERNAME):
                    if candidate and candidate not in candidates:
                        candidates.append(candidate)
                for candidate in candidates:
                    try:
                        candidate_entity = await run_mtproto_operation(
                            lambda candidate=candidate: tg_client.get_entity(candidate),
                            "Founder profile resolve by username"
                        )
                        if getattr(candidate_entity, "id", None) == FOUNDER_ID:
                            entity = candidate_entity
                            break
                        resolve_errors.append(RuntimeError("Resolved username does not match founder ID"))
                    except Exception as candidate_exc:
                        resolve_errors.append(candidate_exc)

            if entity is None or getattr(entity, "id", None) != FOUNDER_ID:
                if resolve_errors:
                    raise RuntimeError("Could not resolve founder by ID or cached username") from resolve_errors[-1]
                raise RuntimeError("Username не соответствует ID создателя")

            selected_entity = entity
            try:
                full = await run_mtproto_operation(
                    lambda: tg_client(functions.users.GetFullUserRequest(id=entity)),
                    "Founder full profile"
                )
                fresh_users = getattr(full, "users", None) or []
                fresh_entity = next(
                    (user for user in fresh_users if getattr(user, "id", None) == FOUNDER_ID),
                    None
                )
                if fresh_entity is not None:
                    # The GetFullUser response is the newest source of username,
                    # profile photo pointer and emoji_status. In particular, it
                    # must be allowed to report that the old collectible is gone.
                    selected_entity = fresh_entity
                    logging.info(
                        "Founder fresh profile loaded: id=%s username=%s status=%s",
                        FOUNDER_ID,
                        getattr(selected_entity, "username", None),
                        type(getattr(selected_entity, "emoji_status", None)).__name__
                    )
                else:
                    logging.warning("Founder not found in GetFullUserRequest.users")
            except Exception as exc:
                if is_flood_wait_error(exc):
                    register_telegram_flood_wait(exc, "Founder full profile")
                else:
                    logging.warning("GetFullUserRequest failed: %s", exc)

            founder_entity_cache = selected_entity
            founder_entity_cache_time = time.time()
            return selected_entity
        except Exception as exc:
            if is_flood_wait_error(exc):
                register_telegram_flood_wait(exc, "Founder profile refresh")
            else:
                logging.warning("Founder profile refresh failed: %s", exc)
            if founder_entity_cache is not None and getattr(founder_entity_cache, "id", None) == FOUNDER_ID:
                return founder_entity_cache
            raise

def get_collectible_status(entity):
    status = getattr(entity, "emoji_status", None)
    if status is None:
        logging.info("Founder has no emoji_status")
        return None
    if not is_collectible_status(status):
        logging.info("Founder status is not collectible: %s", type(status).__name__)
        return None
    document_id = getattr(status, "document_id", None)
    if not document_id:
        logging.warning("Collectible status has no document_id")
        return None
    result = {
        "document_id": int(document_id),
        "title": getattr(status, "title", None),
        "slug": getattr(status, "slug", None)
    }
    logging.info(
        "Founder collectible detected: document_id=%s title=%s slug=%s",
        result["document_id"], result["title"], result["slug"]
    )
    return result


def build_founder_profile(entity):
    first_name = getattr(entity, "first_name", None) or ""
    last_name = getattr(entity, "last_name", None) or ""
    username = str(getattr(entity, "username", None) or "").strip().lstrip("@")
    display_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if not display_name:
        display_name = username or str(FOUNDER_ID)
    profile_url = (
        "https://t.me/" + username if username
        else "tg://user?id=" + str(FOUNDER_ID)
    )
    collectible = get_collectible_status(entity)
    return {
        "id": FOUNDER_ID,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name,
        "username": username,
        "profile_url": profile_url,
        "has_collectible": collectible is not None,
        "gift_document_id": collectible["document_id"] if collectible else None,
        "gift_title": collectible.get("title") if collectible else None
    }

def build_founder_fallback_profile():
    username = FOUNDER_FALLBACK_USERNAME
    return {
        "id": FOUNDER_ID, "first_name": "", "last_name": "",
        "display_name": username, "username": username,
        "profile_url": "https://t.me/" + username,
        "has_collectible": False, "gift_document_id": None, "gift_title": None
    }


async def refresh_founder_avatar(entity, force=False):
    """Обновляет фото профиля из Telegram, не теряя последнюю рабочую копию при сбое."""
    global founder_avatar_cache_time
    if not tg_ready or tg_client is None or entity is None:
        return False

    avatar_path = founder_avatar_path()
    os.makedirs(os.path.dirname(avatar_path), exist_ok=True)
    async with founder_avatar_lock:
        current_time = time.time()
        if os.path.isfile(avatar_path):
            try:
                modified_at = os.path.getmtime(avatar_path)
                if not force and current_time - modified_at < FOUNDER_AVATAR_CACHE_TTL:
                    founder_avatar_cache_time = modified_at
                    return True
            except OSError:
                pass

        temporary_path = avatar_path + ".refresh.jpg"
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
            downloaded_path = await run_mtproto_operation(
                lambda: tg_client.download_profile_photo(
                    entity, file=temporary_path, download_big=True
                ),
                "Founder profile photo download"
            )
            candidate_path = downloaded_path if isinstance(downloaded_path, str) else temporary_path
            if not candidate_path or not os.path.isfile(candidate_path):
                founder_avatar_cache_time = current_time
                return False
            file_size = os.path.getsize(candidate_path)
            if file_size <= 0 or file_size > 10 * 1024 * 1024:
                raise ValueError("Founder avatar has an invalid size")

            is_same = False
            if os.path.isfile(avatar_path) and os.path.getsize(avatar_path) == file_size:
                try:
                    with open(avatar_path, "rb") as old_file, open(candidate_path, "rb") as new_file:
                        is_same = old_file.read() == new_file.read()
                except OSError:
                    is_same = False
            if is_same:
                os.remove(candidate_path)
            else:
                os.replace(candidate_path, avatar_path)
            founder_avatar_cache_time = os.path.getmtime(avatar_path) if os.path.isfile(avatar_path) else current_time
            logging.info("Founder avatar cache refreshed; changed=%s", not is_same)
            return os.path.isfile(avatar_path)
        except Exception as exc:
            logging.warning("Founder avatar refresh failed; keeping previous image: %s", exc)
            founder_avatar_cache_time = current_time
            return os.path.isfile(avatar_path)
        finally:
            for candidate in (temporary_path,):
                try:
                    if os.path.isfile(candidate):
                        os.remove(candidate)
                except OSError:
                    pass


async def get_founder_profile(force=False):
    """Возвращает кэш профиля и автоматически обновляет его по TTL."""
    global founder_profile_cache, founder_profile_cache_time

    async with founder_profile_lock:
        if founder_profile_cache is None:
            founder_profile_cache = load_founder_profile_cache_from_disk()
            if founder_profile_cache is not None:
                founder_profile_cache_time = time.time()

        cache_age = (
            time.time() - founder_profile_cache_time
            if founder_profile_cache is not None else None
        )
        refresh_due = (
            force or founder_profile_cache is None or cache_age is None
            or cache_age >= FOUNDER_PROFILE_CACHE_TTL
        )
        if not refresh_due:
            return founder_profile_cache

        remaining = telegram_flood_remaining()
        if remaining > 0:
            if founder_profile_cache is not None:
                return founder_profile_cache
            founder_profile_cache = build_founder_fallback_profile()
            founder_profile_cache_time = time.time()
            return founder_profile_cache

        try:
            entity = await get_founder_entity(force=True)
            result = build_founder_profile(entity)
            # Profile identity/status and its avatar are refreshed as one cache cycle.
            await refresh_founder_avatar(entity, force=force)
            founder_profile_cache = result
            founder_profile_cache_time = time.time()
            save_founder_profile_cache_to_disk(result)
            logging.info(
                "Founder profile refreshed: username=%s has_collectible=%s gift_document_id=%s",
                result["username"], result["has_collectible"], result["gift_document_id"]
            )
            return result
        except Exception as exc:
            if is_flood_wait_error(exc):
                register_telegram_flood_wait(exc, "Founder API profile")
            logging.warning("Founder API profile could not be refreshed: %s", exc)
            if founder_profile_cache is not None:
                founder_profile_cache_time = time.time()
                return founder_profile_cache
            if founder_entity_cache is not None and getattr(founder_entity_cache, "id", None) == FOUNDER_ID:
                result = build_founder_profile(founder_entity_cache)
                save_founder_profile_cache_to_disk(result)
            else:
                result = build_founder_fallback_profile()
            founder_profile_cache = result
            # Retry a first-time fallback sooner when Telethon has not connected yet.
            founder_profile_cache_time = time.time() - max(0, FOUNDER_PROFILE_CACHE_TTL - 30)
            return result


async def get_founder_gift_animation(document_id):
    """Читает сохранённую Lottie-анимацию или скачивает её из Telegram custom emoji API."""
    if not document_id:
        raise FileNotFoundError("Коллекционный документ не указан")
    document_id = int(document_id)
    cached = founder_gift_cache.get(document_id)
    if cached:
        return cached["data"]

    async with founder_gift_lock:
        cached = founder_gift_cache.get(document_id)
        if cached:
            return cached["data"]

        gift_dir = founder_gift_directory()
        os.makedirs(gift_dir, exist_ok=True)
        animation_path = os.path.join(gift_dir, f"{document_id}.json")
        animation_data = None

        if os.path.isfile(animation_path):
            try:
                with open(animation_path, "r", encoding="utf-8") as file:
                    candidate = json.load(file)
                if isinstance(candidate, dict) and isinstance(candidate.get("layers"), list):
                    animation_data = candidate
            except Exception as exc:
                logging.warning("Founder gift disk cache is invalid; trying Telegram again: %s", exc)

        if animation_data is None:
            if not tg_ready or tg_client is None or functions is None:
                raise FileNotFoundError("Telegram client is not ready to download the collectible animation")
            try:
                request_type = getattr(getattr(functions, "messages", None), "GetCustomEmojiDocumentsRequest", None)
                if request_type is None:
                    raise RuntimeError("Telethon does not expose GetCustomEmojiDocumentsRequest")
                documents = await run_mtproto_operation(
                    lambda: tg_client(request_type(document_id=[document_id])),
                    "Founder collectible document resolve"
                )
                document = next(iter(documents or []), None)
                if document is None:
                    raise FileNotFoundError("Telegram did not return the collectible document")
                raw_data = await run_mtproto_operation(
                    lambda: tg_client.download_media(document, bytes),
                    "Founder collectible animation download"
                )
                if not isinstance(raw_data, (bytes, bytearray)) or not raw_data:
                    raise FileNotFoundError("Telegram returned an empty collectible animation")
                if len(raw_data) > 10 * 1024 * 1024:
                    raise ValueError("Collectible animation is too large")

                try:
                    decoded_data = gzip.decompress(raw_data)
                except (OSError, EOFError):
                    decoded_data = bytes(raw_data)
                candidate = json.loads(decoded_data.decode("utf-8-sig"))
                if not isinstance(candidate, dict) or not isinstance(candidate.get("layers"), list):
                    raise ValueError("Telegram collectible document is not a valid Lottie animation")
                animation_data = candidate

                temporary_path = animation_path + ".tmp"
                try:
                    with open(temporary_path, "w", encoding="utf-8") as file:
                        json.dump(animation_data, file, ensure_ascii=False, separators=(",", ":"))
                    os.replace(temporary_path, animation_path)
                finally:
                    if os.path.exists(temporary_path):
                        os.remove(temporary_path)
                logging.info("Founder collectible animation downloaded and cached: document_id=%s", document_id)
            except Exception as exc:
                logging.warning("Could not download founder collectible animation %s: %s", document_id, exc)
                if isinstance(exc, FileNotFoundError):
                    raise
                raise FileNotFoundError("Founder collectible animation could not be downloaded") from exc

        founder_gift_cache[document_id] = {"data": animation_data, "time": time.time()}
        return animation_data


async def api_founder(request):
    try:
        profile = dict(await get_founder_profile())
        avatar_path = founder_avatar_path()
        if not os.path.isfile(avatar_path):
            legacy_path = os.path.join(APP_DIR, "founder_nifort.jpg")
            if os.path.isfile(legacy_path):
                avatar_path = legacy_path
        try:
            profile["avatar_version"] = str(os.stat(avatar_path).st_mtime_ns) if os.path.isfile(avatar_path) else "0"
        except OSError:
            profile["avatar_version"] = "0"
        headers = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
        return web.json_response(profile, headers=headers)
    except Exception as exc:
        logging.warning("Founder profile unavailable: %s", exc)
        profile = build_founder_fallback_profile()
        profile["avatar_version"] = "0"
        return web.json_response(
            profile,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}
        )


async def api_founder_gift(request):
    try:
        profile = await get_founder_profile()
        if not profile.get("has_collectible"):
            return web.json_response(
                {"error": "No active collectible status"}, status=404,
                headers={"Cache-Control": "no-store"}
            )
        document_id = profile.get("gift_document_id")
        if not document_id:
            return web.json_response(
                {"error": "Gift document unavailable"}, status=404,
                headers={"Cache-Control": "no-store"}
            )
        animation = await get_founder_gift_animation(document_id)
        return web.json_response(animation, headers={"Cache-Control": "public, max-age=3600"})
    except FileNotFoundError as exc:
        logging.warning("Founder gift not found: %s", exc)
        return web.json_response({"error": "Gift animation not found"}, status=404, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        logging.warning("Founder gift animation unavailable: %s", exc)
        return web.json_response({"error": "Gift animation temporarily unavailable"}, status=503, headers={"Cache-Control": "no-store"})


async def api_founder_avatar(request):
    avatar_path = founder_avatar_path()
    if not os.path.isfile(avatar_path):
        # Backward-compatible fallback for a previous deployment layout.
        avatar_path = os.path.join(APP_DIR, "founder_nifort.jpg")
    if os.path.isfile(avatar_path):
        try:
            etag = str(os.stat(avatar_path).st_mtime_ns)
        except OSError:
            etag = "founder"
        return web.FileResponse(
            avatar_path,
            headers={"Cache-Control": "no-cache, must-revalidate", "ETag": '"' + etag + '"'}
        )
    return web.Response(
        status=404,
        text="Cached founder avatar unavailable",
        headers={"Cache-Control": "no-store"}
    )


# ============================================================
# ПУБЛИЧНОЕ API
# ============================================================

async def api_stats(request):
    with db_connect() as conn:
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        projects_count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE moderation_status='approved'"
        ).fetchone()[0]
    return web.json_response({"users": users_count, "projects": projects_count})


async def api_catalog(request):
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT id, title, link, category, description, website, moderation_status,
                   ownership_status, is_official, telegram_verified, source_url,
                   is_seed, topic, community_type, linkcheck_badge_manual, created_at,
                   (SELECT MAX(checked_at) FROM check_history
                    WHERE check_history.project_id=projects.id) AS last_checked_at
            FROM projects WHERE moderation_status='approved'
            ORDER BY CASE
                WHEN is_official=1 OR ownership_status='verified' THEN 0
                WHEN telegram_verified=1 THEN 1 ELSE 2 END ASC,
                is_seed DESC, title COLLATE NOCASE ASC
        """).fetchall()
    return web.json_response([
        {
            **dict(row),
            **linkcheck_badge_payload(row),
            "is_official": bool(row["is_official"]),
            "telegram_verified": bool(row["telegram_verified"]),
            "is_seed": bool(row["is_seed"])
        }
        for row in rows
    ])


async def api_check(request):
    try:
        data = await read_json(request)
        link = normalize_link(data.get("link", ""))
    except (ValueError, web.HTTPBadRequest) as exc:
        return json_error(str(exc) or "Некорректный запрос")

    with db_connect() as conn:
        project = conn.execute("""
            SELECT id, title, moderation_status, category, ownership_status,
                   is_official, telegram_verified, source_url, topic, community_type,
                   linkcheck_badge_manual
            FROM projects WHERE lower(link)=lower(?) LIMIT 1
        """, (link,)).fetchone()

    if project:
        telegram = {
            "status": "catalog",
            "type": project["category"] or "unknown",
            "verified": bool(project["telegram_verified"]),
            "member_count": None,
            "reason": "",
            "retry_after": 0,
            "verification_source": "catalog"
        }
        result_source = "catalog"
    else:
        telegram = await check_telegram_resource(link, "other")
        result_source = telegram.get("verification_source") or "telegram"

    telegram_status = telegram["status"]
    response = {
        "link": link,
        "telegram_status": telegram_status,
        "telegram_type": telegram.get("type"),
        "telegram_member_count": telegram.get("member_count"),
        "telegram_verified": bool(telegram.get("verified")) if telegram_status in {"exists", "catalog"} else False,
        "telegram_reason": telegram.get("reason", ""),
        "telegram_retry_after": int(telegram.get("retry_after", 0) or 0),
        "telegram_check_available": telegram_status != "unavailable",
        "telegram_check_performed": telegram_status != "catalog",
        "telegram_verification_source": telegram.get("verification_source"),
        "result_source": result_source,
        "listed": bool(project),
        "moderation": project["moderation_status"] if project else None,
        "title": project["title"] if project else None,
        "project_id": project["id"] if project else None,
        "ownership_status": project["ownership_status"] if project else None,
        "is_official": bool(project["is_official"]) if project else False,
        "linkcheck_badge": bool(project and (project["is_official"] or project["linkcheck_badge_manual"])),
        "linkcheck_badge_source": (
            "official" if project and project["is_official"] else
            "manual" if project and project["linkcheck_badge_manual"] else None
        ),
        "catalog_verified": bool(project["telegram_verified"]) if project else False,
        "source_url": project["source_url"] if project else None,
        "topic": project["topic"] if project else None,
        "community_type": project["community_type"] if project else None
    }

    # Save private history only for requests with valid Telegram WebApp authentication.
    # A history-write failure must never turn a successful check into an API failure.
    user = authenticated_user(request)
    if user:
        checked_at = now_iso()
        try:
            with db_connect() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO check_history
                    (user_tg_id, project_id, link, title, status, result_source,
                     telegram_verified, is_official, linkcheck_badge, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user["id"], response["project_id"], response["link"],
                    response["title"] or response["link"], telegram_status,
                    response["result_source"], int(response["telegram_verified"]),
                    int(response["is_official"]), int(response["linkcheck_badge"]),
                    checked_at
                ))
                conn.execute("""
                    DELETE FROM check_history
                    WHERE user_tg_id=? AND id NOT IN (
                        SELECT id FROM check_history
                        WHERE user_tg_id=? ORDER BY id DESC LIMIT 100
                    )
                """, (user["id"], user["id"]))
        except Exception:
            logging.exception("Failed to save check history for Telegram user %s", user["id"])

    return web.json_response(response)


async def api_check_history(request):
    user = authenticated_user(request)
    if not user:
        return json_error(
            "Telegram-авторизация не прошла проверку. Открой приложение через Telegram.",
            401
        )
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT id, link, title, status, result_source, telegram_verified,
                   is_official, linkcheck_badge, checked_at
            FROM check_history
            WHERE user_tg_id=?
            ORDER BY id DESC
            LIMIT 3
        """, (user["id"],)).fetchall()
    return web.json_response([dict(row) for row in rows])

# ============================================================
# АВТОРИЗОВАННОЕ API
# ============================================================

async def api_me(request):
    user = authenticated_user(request)
    if not user:
        return json_error(
            "Telegram-авторизация не прошла проверку. Открой приложение через Telegram.",
            401
        )
    upsert_user(user["id"], user.get("username"))
    with db_connect() as conn:
        row = conn.execute(
            "SELECT is_trusted, has_requested_access FROM users WHERE tg_id=?",
            (user["id"],)
        ).fetchone()
    return web.json_response({
        "id": user["id"],
        "is_admin": user["id"] in ADMIN_IDS,
        "is_trusted": bool(row["is_trusted"]) if row else False,
        "has_requested_access": bool(row["has_requested_access"]) if row else False
    })


async def api_request_access(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Unauthorized", 401)

    upsert_user(user["id"], user.get("username"))
    should_notify = False
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT is_trusted, has_requested_access FROM users WHERE tg_id=? LIMIT 1",
            (user["id"],)
        ).fetchone()
        if row and row["is_trusted"]:
            return web.json_response({
                "status": "ok",
                "already_trusted": True,
                "message": "У вас уже есть права на добавление проектов."
            })
        if not row or not row["has_requested_access"]:
            conn.execute(
                "UPDATE users SET has_requested_access=1 WHERE tg_id=?",
                (user["id"],)
            )
            should_notify = True

    if should_notify:
        username = user.get("username") or "без username"
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🔔 <b>Новый запрос прав</b>\nПользователь: @{username}\nID: <code>{user['id']}</code>"
                )
            except Exception:
                logging.exception("Не удалось уведомить администратора %s", admin_id)

    return web.json_response({
        "status": "ok",
        "already_requested": not should_notify,
        "message": (
            "Запрос уже отправлен и ожидает рассмотрения."
            if not should_notify else
            "Запрос прав отправлен администраторам."
        )
    })



async def api_add_project(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Unauthorized", 401)
    upsert_user(user["id"], user.get("username"))
    try:
        data = await read_json(request)
        title = data.get("title", "")
        description = data.get("description", "")
        category = data.get("category", "other")
        topic = str(data.get("topic") or "").strip().lower()
        allowed_topics = {"", "news", "finance", "government", "education", "technology", "shopping", "entertainment", "other"}
        if topic not in allowed_topics:
            return json_error("Некорректная тематика проекта")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
            return json_error("Название обязательно и должно быть не длиннее 80 символов")
        if not isinstance(description, str) or len(description) > 1000:
            return json_error("Описание не должно превышать 1000 символов")
        if not isinstance(category, str) or category not in {"channel", "bot", "group", "other"}:
            return json_error("Некорректная категория")
        link = normalize_link(data.get("link", ""))
        website = valid_website(data.get("website", ""))
    except (ValueError, web.HTTPBadRequest) as exc:
        return json_error(str(exc) or "Некорректный запрос")

    with db_connect() as conn:
        row = conn.execute("SELECT is_trusted FROM users WHERE tg_id=?", (user["id"],)).fetchone()
        if user["id"] not in ADMIN_IDS and (not row or not row["is_trusted"]):
            return json_error("У вас пока нет прав на добавление проектов.", 403)
        duplicate = conn.execute("SELECT id FROM projects WHERE lower(link)=lower(?)", (link,)).fetchone()
        if duplicate:
            return json_error("Этот ресурс уже есть в базе")

    telegram = await check_telegram_resource(link, category)
    telegram_status = telegram["status"]
    if telegram_status == "not_found":
        return json_error("Ресурс не найден в Telegram. Проверьте username.", 400)
    if telegram_status == "wrong_type":
        return json_error(telegram["reason"], 400)
    # Telegram's native verified flag is stored separately from LINKCHECK officialness
    # and ownership proof. A Telegram badge alone does not prove who owns the project.
    telegram_verified = 1 if telegram_status == "exists" and telegram.get("verified") else 0
    logging.info(
        "Project verification detected: link=%s telegram_status=%s telegram_verified=%s source=%s",
        link,
        telegram_status,
        bool(telegram_verified),
        telegram.get("verification_source")
    )

    with db_connect() as conn:
        try:
            cur = conn.execute("""
                INSERT INTO projects
                (owner_tg_id, title, link, category, description, website,
                 ownership_status, moderation_status, created_at, is_official,
                 telegram_verified, source_url, is_seed, topic, community_type)
                VALUES (?, ?, ?, ?, ?, ?, 'unverified', 'pending', ?, 0, ?, NULL, 0, ?, NULL)
            """, (
                user["id"], title.strip(), link, category, description.strip(),
                website, now_iso(), telegram_verified, topic or None
            ))
        except sqlite3.IntegrityError:
            return json_error("Этот ресурс уже есть в базе")
        project_id = cur.lastrowid

    message = (
        "Telegram-ресурс найден. Проект отправлен на модерацию."
        if telegram_status == "exists"
        else "Заявка отправлена на модерацию. Проверка Telegram временно недоступна. Telegram-галочка не установлена."
    )
    return web.json_response({
        "status": "ok",
        "project_id": project_id,
        "telegram_status": telegram_status,
        "telegram_verified": bool(telegram_verified),
        "telegram_retry_after": int(telegram.get("retry_after", 0) or 0),
        "message": message
    })


async def api_my_projects(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Unauthorized", 401)
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT id, title, link, category, description, website, moderation_status,
                   ownership_status, is_official, telegram_verified, source_url, topic, community_type,
                   linkcheck_badge_manual,
                   (SELECT oc.status FROM ownership_challenges oc
                    WHERE oc.project_id=projects.id AND oc.owner_tg_id=projects.owner_tg_id
                      AND oc.method IN ('bot_manual', 'manual_admin')
                    ORDER BY oc.id DESC LIMIT 1) AS ownership_review_status,
                   (SELECT oc.id FROM ownership_challenges oc
                    WHERE oc.project_id=projects.id AND oc.owner_tg_id=projects.owner_tg_id
                      AND oc.method IN ('bot_manual', 'manual_admin')
                    ORDER BY oc.id DESC LIMIT 1) AS ownership_challenge_id,
                   (SELECT ov.status FROM official_verification_requests ov
                    WHERE ov.project_id=projects.id ORDER BY ov.id DESC LIMIT 1) AS official_request_status,
                   (SELECT ov.source_url FROM official_verification_requests ov
                    WHERE ov.project_id=projects.id ORDER BY ov.id DESC LIMIT 1) AS official_request_source_url,
                   (SELECT ov.automatic_status FROM official_verification_requests ov
                    WHERE ov.project_id=projects.id ORDER BY ov.id DESC LIMIT 1) AS official_request_auto_status,
                   (SELECT ov.automatic_detail FROM official_verification_requests ov
                    WHERE ov.project_id=projects.id ORDER BY ov.id DESC LIMIT 1) AS official_request_auto_detail,
                   (SELECT ov.review_note FROM official_verification_requests ov
                    WHERE ov.project_id=projects.id ORDER BY ov.id DESC LIMIT 1) AS official_request_review_note
            FROM projects WHERE owner_tg_id=? ORDER BY id DESC
        """, (user["id"],)).fetchall()
    return web.json_response([
        {
            "id": row["id"], "title": row["title"], "link": row["link"],
            "category": row["category"], "description": row["description"] or "",
            "website": row["website"] or "",
            "moderation": row["moderation_status"],
            "ownership_status": row["ownership_status"],
            "is_official": bool(row["is_official"]),
            **linkcheck_badge_payload(row),
            "telegram_verified": bool(row["telegram_verified"]),
            "source_url": row["source_url"], "topic": row["topic"],
            "community_type": row["community_type"],
            "ownership_review_status": row["ownership_review_status"],
            "ownership_challenge_id": row["ownership_challenge_id"],
            "official_request_status": row["official_request_status"],
            "official_request_source_url": row["official_request_source_url"] or "",
            "official_request_auto_status": row["official_request_auto_status"] or "",
            "official_request_auto_detail": row["official_request_auto_detail"] or "",
            "official_request_review_note": row["official_request_review_note"] or ""
        } for row in rows
    ])


async def api_edit_project(request):
    """Редактирование проекта его владельцем с повторной модерацией."""
    user = authenticated_user(request)
    if not user:
        return json_error("Unauthorized", 401)

    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        title = data.get("title", "")
        description = data.get("description", "")
        category = data.get("category", "other")
        topic_supplied = "topic" in data
        topic = str(data.get("topic") or "").strip().lower()
        allowed_topics = {"", "news", "finance", "government", "education", "technology", "shopping", "entertainment", "other"}
        if topic not in allowed_topics:
            return json_error("Некорректная тематика проекта")
        if project_id <= 0:
            return json_error("Некорректный ID проекта")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 80:
            return json_error("Название обязательно и должно быть не длиннее 80 символов")
        if not isinstance(description, str) or len(description) > 1000:
            return json_error("Описание не должно превышать 1000 символов")
        if not isinstance(category, str) or category not in {"channel", "bot", "group", "other"}:
            return json_error("Некорректная категория")
        link = normalize_link(data.get("link", ""))
        website = valid_website(data.get("website", ""))
    except (ValueError, TypeError, web.HTTPBadRequest) as exc:
        return json_error(str(exc) or "Некорректный запрос")

    title = title.strip()
    description = description.strip()

    with db_connect() as conn:
        project = conn.execute("""
            SELECT id, owner_tg_id, title, link, category, description, website, topic,
                   ownership_status, is_official, telegram_verified, source_url
            FROM projects WHERE id=? AND owner_tg_id=? LIMIT 1
        """, (project_id, user["id"])).fetchone()
        if not project:
            return json_error("Проект не найден или не принадлежит вам", 404)
        if not topic_supplied:
            topic = str(project["topic"] or "").strip().lower()

        duplicate = conn.execute(
            "SELECT id FROM projects WHERE lower(link)=lower(?) AND id<>? LIMIT 1",
            (link, project_id)
        ).fetchone()
        if duplicate:
            return json_error("Этот ресурс уже есть в базе")

    old_link = str(project["link"] or "")
    old_category = str(project["category"] or "other")
    old_website = str(project["website"] or "")
    link_changed = link.lower() != old_link.lower()
    category_changed = category != old_category
    website_changed = website != old_website
    topic_changed = (topic or None) != (project["topic"] or None)
    content_changed = any((
        title != str(project["title"] or ""),
        link_changed,
        category_changed,
        description != str(project["description"] or ""),
        website_changed,
        topic_changed
    ))

    if not content_changed:
        return web.json_response({
            "status": "ok", "changed": False,
            "message": "Изменений нет",
            "project_id": project_id
        })

    # Редактирование не должно зависеть от доступности Telegram/Telethon.
    # В частности, проверка Telegram может попасть под FloodWait и задержать
    # сохранение на минуты или часы. Новые данные всё равно проходят повторную
    # модерацию; при смене ссылки старый Telegram-бейдж обязательно сбрасывается.
    telegram_verified = int(project["telegram_verified"] or 0)
    telegram_status = "unchanged"
    if link_changed:
        telegram_verified = 0
        telegram_status = "recheck_after_moderation"
    elif category_changed:
        # Сам Telegram-ресурс тот же; сохраняем ранее полученный факт verified.
        telegram_status = "same_resource_category_changed"

    reset_official_proof = link_changed or website_changed
    ownership_proof_changed = link_changed or category_changed or website_changed
    ownership_status = "unverified" if ownership_proof_changed else project["ownership_status"]
    is_official = 0 if reset_official_proof else int(project["is_official"] or 0)
    source_url = None if reset_official_proof else project["source_url"]

    with db_connect() as conn:
        # Повторная проверка владельца и уникальности защищает от гонки между запросами.
        current = conn.execute(
            "SELECT id FROM projects WHERE id=? AND owner_tg_id=?",
            (project_id, user["id"])
        ).fetchone()
        if not current:
            return json_error("Проект не найден или не принадлежит вам", 404)
        duplicate = conn.execute(
            "SELECT id FROM projects WHERE lower(link)=lower(?) AND id<>? LIMIT 1",
            (link, project_id)
        ).fetchone()
        if duplicate:
            return json_error("Этот ресурс уже есть в базе")
        if ownership_proof_changed:
            conn.execute(
                "UPDATE ownership_challenges SET status='invalidated' "
                "WHERE project_id=? AND status='pending'",
                (project_id,)
            )
        conn.execute(
            "UPDATE official_verification_requests "
            "SET status='invalidated', review_note='Данные проекта изменены владельцем' "
            "WHERE project_id=? AND status='pending'",
            (project_id,)
        )
        try:
            conn.execute("""
                UPDATE projects
                SET title=?, link=?, category=?, description=?, website=?, topic=?,
                    moderation_status='pending', ownership_status=?, is_official=?,
                    telegram_verified=?, source_url=?
                WHERE id=? AND owner_tg_id=?
            """, (
                title, link, category, description, website, topic or None,
                ownership_status, is_official, telegram_verified, source_url,
                project_id, user["id"]
            ))
        except sqlite3.IntegrityError:
            return json_error("Этот ресурс уже есть в базе")

    return web.json_response({
        "status": "ok",
        "changed": True,
        "project_id": project_id,
        "moderation": "pending",
        "telegram_status": telegram_status,
        "telegram_verified": bool(telegram_verified),
        "message": "Изменения сохранены и отправлены на повторную модерацию"
    })


# ============================================================
# ПОДТВЕРЖДЕНИЕ ВЛАДЕНИЯ ПРОЕКТОМ
# ============================================================

OWNERSHIP_CHALLENGE_TTL_HOURS = 24
OWNERSHIP_MAX_CHALLENGES_PER_DAY = 5
OWNERSHIP_DOH_URL = "https://cloudflare-dns.com/dns-query"


def ownership_website_host(website):
    """Возвращает безопасное DNS-имя сайта для TXT-проверки."""
    if not website:
        return None
    parsed = urlsplit(str(website).strip())
    hostname = (parsed.hostname or "").strip(".").lower()
    if not hostname:
        return None
    try:
        hostname = hostname.encode("idna").decode("ascii")
        ipaddress.ip_address(hostname)
        return None
    except ValueError:
        pass
    if hostname in {"localhost", "localhost.localdomain"} or "." not in hostname:
        return None
    if len(hostname) > 253 or not re.fullmatch(
        r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))+$",
        hostname
    ):
        return None
    return hostname


async def ownership_dns_txt_values(record_name):
    """Получает TXT через DNS-over-HTTPS; сайт LINKCHECK не запрашивается."""
    timeout = aiohttp.ClientTimeout(total=8, connect=4)
    headers = {"accept": "application/dns-json"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(
            OWNERSHIP_DOH_URL,
            params={"name": record_name, "type": "TXT"},
            allow_redirects=False
        ) as response:
            if response.status != 200:
                raise RuntimeError("DNS-провайдер временно недоступен")
            payload = await response.json(content_type=None)

    values = []
    for answer in payload.get("Answer", []) if isinstance(payload, dict) else []:
        if int(answer.get("type", 0) or 0) != 16:
            continue
        raw = str(answer.get("data") or "").strip()
        # DNS-over-HTTPS обычно возвращает TXT как "part1" "part2".
        chunks = re.findall(r'"((?:[^"\\]|\\.)*)"', raw)
        value = "".join(chunks) if chunks else raw.strip('"')
        values.append(value)
    return values


async def api_ownership_start(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Переоткройте Mini App.", 401)

    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        if project_id <= 0:
            return json_error("Некорректный ID проекта")
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute("""
            SELECT id, owner_tg_id, title, link, category, website, ownership_status
            FROM projects
            WHERE id=? AND owner_tg_id=?
            LIMIT 1
        """, (project_id, user["id"])).fetchone()

        if not project:
            return json_error("Проект не найден или не принадлежит вам", 404)
        if project["ownership_status"] == "verified":
            return web.json_response({"status": "verified"})

        project_category = str(project["category"] or "").lower()
        telegram_available = project_category in {"channel", "group"}
        bot_manual_available = project_category == "bot"
        botfather_url = "https://t.me/BotFather" if bot_manual_available else None
        bot_manual_challenge_id = None

        if bot_manual_available:
            now_manual = datetime.now(timezone.utc)
            cutoff_manual = (now_manual - timedelta(hours=24)).isoformat()

            manual_admin_pending = conn.execute("""
                SELECT id FROM ownership_challenges
                WHERE project_id=? AND owner_tg_id=? AND method='manual_admin'
                  AND status='submitted'
                ORDER BY id DESC LIMIT 1
            """, (project_id, user["id"])).fetchone()
            if manual_admin_pending:
                return json_error(
                    "Заявка на ручное подтверждение уже ожидает решения администратора.",
                    409
                )

            active_manual = conn.execute("""
                SELECT id, status, expires_at
                FROM ownership_challenges
                WHERE project_id=? AND owner_tg_id=? AND method='bot_manual'
                  AND status IN ('awaiting_upload', 'awaiting_photo', 'submitted')
                ORDER BY id DESC LIMIT 1
            """, (project_id, user["id"])).fetchone()

            if active_manual and active_manual["status"] == "submitted":
                return json_error(
                    "Скриншот уже отправлен и ожидает решения администратора. Повторная заявка пока недоступна.",
                    409
                )

            if active_manual and active_manual["status"] in {"awaiting_upload", "awaiting_photo"}:
                try:
                    is_expired = datetime.fromisoformat(active_manual["expires_at"]) <= now_manual
                except (TypeError, ValueError):
                    is_expired = True
                if not is_expired:
                    if active_manual["status"] == "awaiting_photo":
                        conn.execute(
                            "UPDATE ownership_challenges SET status='awaiting_upload' "
                            "WHERE id=? AND status='awaiting_photo'",
                            (active_manual["id"],)
                        )
                    bot_manual_challenge_id = active_manual["id"]
                else:
                    conn.execute(
                        "UPDATE ownership_challenges SET status='expired' WHERE id=?",
                        (active_manual["id"],)
                    )

            if bot_manual_challenge_id is None:
                manual_count = conn.execute("""
                    SELECT COUNT(*) FROM ownership_challenges
                    WHERE project_id=? AND owner_tg_id=? AND method='bot_manual'
                      AND created_at>=?
                """, (project_id, user["id"], cutoff_manual)).fetchone()[0]
                if manual_count >= 5:
                    return json_error(
                        "Лимит начала подтверждения через BotFather: не более 5 заявок за 24 часа.",
                        429
                    )

                manual_expires = now_manual + timedelta(hours=24)
                cursor = conn.execute("""
                    INSERT INTO ownership_challenges
                    (project_id, owner_tg_id, method, domain, token_hash, status,
                     attempts, created_at, expires_at)
                    VALUES (?, ?, 'bot_manual', NULL, NULL, 'awaiting_upload', 0, ?, ?)
                """, (
                    project_id, user["id"],
                    now_manual.isoformat(), manual_expires.isoformat()
                ))
                bot_manual_challenge_id = cursor.lastrowid

        domain = ownership_website_host(project["website"])
        dns_available = bool(domain)

        dns_record_name = None
        dns_record_value = None
        expires_at = None

        if dns_available:
            now_dns = datetime.now(timezone.utc)
            cutoff_dns = (now_dns - timedelta(days=1)).isoformat()
            count = conn.execute("""
                SELECT COUNT(*) FROM ownership_challenges
                WHERE project_id=? AND owner_tg_id=? AND method='dns'
                  AND created_at>=?
            """, (project_id, user["id"], cutoff_dns)).fetchone()[0]
            if count >= OWNERSHIP_MAX_CHALLENGES_PER_DAY:
                return json_error("Лимит запросов DNS-подтверждения: 5 за 24 часа", 429)

            token = secrets.token_urlsafe(24)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            expires = now_dns + timedelta(hours=OWNERSHIP_CHALLENGE_TTL_HOURS)
            expires_at = expires.isoformat()
            conn.execute("""
                UPDATE ownership_challenges
                SET status='replaced'
                WHERE project_id=? AND owner_tg_id=? AND method='dns' AND status='pending'
            """, (project_id, user["id"]))
            conn.execute("""
                INSERT INTO ownership_challenges
                (project_id, owner_tg_id, method, domain, token_hash, status,
                 attempts, created_at, expires_at)
                VALUES (?, ?, 'dns', ?, ?, 'pending', 0, ?, ?)
            """, (
                project_id, user["id"], domain, token_hash,
                now_dns.isoformat(), expires_at
            ))
            dns_record_name = "_linkcheck." + domain
            dns_record_value = "linkcheck-verification=" + token

    return web.json_response({
        "status": "ok",
        "telegram_available": telegram_available,
        "telegram_bot_username": "linkcheck_verify_bot" if telegram_available else None,
        "bot_manual_available": bot_manual_available,
        "bot_manual_challenge_id": bot_manual_challenge_id,
        "botfather_url": botfather_url,
        "dns_available": dns_available,
        "dns_record_name": dns_record_name,
        "dns_record_value": dns_record_value,
        "expires_at": expires_at,
        "message": "Выберите доступный способ подтверждения"
    })



async def api_ownership_manual_review(request):
    """Создаёт запрос на ручную проверку владения администратором LINKCHECK."""
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Переоткройте Mini App.", 401)

    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        note = str(data.get("note") or "").strip()
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос", 400)

    if project_id <= 0:
        return json_error("Некорректный ID проекта", 400)
    if len(note) < 10:
        return json_error("Опишите, почему автоматическое подтверждение вам не подходит (минимум 10 символов).", 400)
    if len(note) > 1000:
        return json_error("Пояснение не должно превышать 1 000 символов.", 400)

    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    expires_text = (now + timedelta(days=7)).isoformat()
    cutoff_text = (now - timedelta(hours=24)).isoformat()

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute("""
            SELECT id, owner_tg_id, title, link, ownership_status
            FROM projects
            WHERE id=? AND owner_tg_id=?
            LIMIT 1
        """, (project_id, user["id"])).fetchone()
        if not project:
            return json_error("Проект не найден или не принадлежит вам", 404)
        if project["ownership_status"] == "verified":
            return web.json_response({"status": "verified", "message": "Владение уже подтверждено."})

        pending = conn.execute("""
            SELECT id FROM ownership_challenges
            WHERE project_id=? AND owner_tg_id=? AND status='submitted'
              AND method IN ('bot_manual', 'manual_admin')
            ORDER BY id DESC LIMIT 1
        """, (project_id, user["id"])).fetchone()
        if pending:
            return json_error("Заявка на ручную проверку или скриншот уже ожидает решения администратора.", 409)

        recent_count = conn.execute("""
            SELECT COUNT(*) FROM ownership_challenges
            WHERE project_id=? AND owner_tg_id=? AND method='manual_admin'
              AND created_at>=?
        """, (project_id, user["id"], cutoff_text)).fetchone()[0]
        if recent_count >= 3:
            return json_error("Можно отправить не более 3 запросов на ручную проверку за 24 часа.", 429)

        # Не оставляем параллельную незавершённую загрузку скриншота,
        # если пользователь выбрал ручной способ.
        conn.execute("""
            UPDATE ownership_challenges
            SET status='replaced'
            WHERE project_id=? AND owner_tg_id=? AND method='bot_manual'
              AND status IN ('pending', 'awaiting_upload', 'awaiting_photo')
        """, (project_id, user["id"]))

        cursor = conn.execute("""
            INSERT INTO ownership_challenges
                (project_id, owner_tg_id, method, domain, token_hash, status,
                 attempts, created_at, expires_at, manual_note)
            VALUES (?, ?, 'manual_admin', NULL, NULL, 'submitted', 0, ?, ?, ?)
        """, (project_id, user["id"], now_text, expires_text, note))
        challenge_id = cursor.lastrowid

    logging.info(
        "Ownership manual review requested: challenge_id=%s project_id=%s owner_tg_id=%s",
        challenge_id, project_id, user["id"]
    )
    return web.json_response({
        "status": "submitted",
        "challenge_id": challenge_id,
        "message": "Запрос отправлен администраторам LINKCHECK на ручную проверку."
    })


async def api_ownership_screenshot_upload(request):
    """Сохраняет один ограниченный по размеру скриншот BotFather из Mini App."""
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Переоткройте Mini App.", 401)
    if not request.content_type.lower().startswith("multipart/form-data"):
        return json_error("Ожидается файл изображения", 400)

    max_image_size = 8 * 1024 * 1024
    max_request_payload = 9 * 1024 * 1024
    max_field_size = 128
    max_parts = 3

    form = await request.multipart()
    values = {}
    image_data = None
    image_mime = None
    image_filename = "screenshot"
    total_payload_size = 0
    part_count = 0

    while True:
        part = await form.next()
        if part is None:
            break

        part_count += 1
        if part_count > max_parts:
            return json_error("Слишком много частей в запросе", 400)

        part_name = part.name or ""
        if part_name == "screenshot":
            if not part.filename:
                return json_error("Часть screenshot должна содержать файл изображения", 400)
            if image_data is not None:
                return json_error("Можно отправить только один скриншот", 400)

            image_mime = (
                part.headers.get(aiohttp.hdrs.CONTENT_TYPE) or ""
            ).lower().split(";", 1)[0].strip()
            if image_mime not in {"image/png", "image/jpeg", "image/webp"}:
                return json_error("Поддерживаются только PNG, JPG и WEBP", 415)

            chunks = []
            image_size = 0
            while True:
                chunk = await part.read_chunk(size=64 * 1024)
                if not chunk:
                    break
                chunk_size = len(chunk)
                total_payload_size += chunk_size
                image_size += chunk_size
                if total_payload_size > max_request_payload:
                    return json_error("Размер всего запроса превышает допустимый предел", 413)
                if image_size > max_image_size:
                    return json_error("Размер изображения не должен превышать 8 МБ", 413)
                chunks.append(chunk)

            image_data = b"".join(chunks)
            image_filename = os.path.basename(part.filename or "screenshot")[:120]

        elif part_name in {"project_id", "challenge_id"}:
            if part.filename:
                return json_error("В поле идентификатора нельзя отправлять файл", 400)
            if part_name in values:
                return json_error("Повторяющееся поле в запросе", 400)

            field_chunks = bytearray()
            while True:
                chunk = await part.read_chunk(size=1024)
                if not chunk:
                    break
                total_payload_size += len(chunk)
                if total_payload_size > max_request_payload:
                    return json_error("Размер всего запроса превышает допустимый предел", 413)
                if len(field_chunks) + len(chunk) > max_field_size:
                    return json_error("Слишком длинное поле идентификатора", 400)
                field_chunks.extend(chunk)
            try:
                values[part_name] = field_chunks.decode("utf-8").strip()
            except UnicodeDecodeError:
                return json_error("Некорректная кодировка полей запроса", 400)

        else:
            return json_error("Неизвестное поле в запросе", 400)

    try:
        project_id = int(values.get("project_id", "0"))
        challenge_id = int(values.get("challenge_id", "0"))
    except (ValueError, TypeError):
        return json_error("Не удалось определить заявку", 400)

    if project_id <= 0 or challenge_id <= 0:
        return json_error("Не удалось определить проект или заявку", 400)
    if not image_data:
        return json_error("Выберите скриншот перед отправкой", 400)

    valid_signature = (
        (image_mime == "image/png" and image_data.startswith(b"\x89PNG\r\n\x1a\n"))
        or (image_mime == "image/jpeg" and image_data.startswith(b"\xff\xd8\xff"))
        or (
            image_mime == "image/webp" and len(image_data) >= 12
            and image_data[:4] == b"RIFF" and image_data[8:12] == b"WEBP"
        )
    )
    if not valid_signature:
        return json_error("Файл не похож на корректное изображение PNG, JPG или WEBP", 415)

    now = datetime.now(timezone.utc)
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        challenge = conn.execute("""
            SELECT oc.id, oc.project_id, oc.owner_tg_id, oc.expires_at, oc.status
            FROM ownership_challenges oc
            JOIN projects p ON p.id=oc.project_id
            WHERE oc.id=? AND oc.project_id=? AND oc.owner_tg_id=?
              AND oc.method='bot_manual' AND p.category='bot'
            LIMIT 1
        """, (challenge_id, project_id, user["id"])).fetchone()
        if not challenge:
            return json_error("Заявка не найдена или не принадлежит вам", 404)
        if challenge["status"] != "awaiting_upload":
            return json_error("Эта заявка уже отправлена или заменена. Начните подтверждение заново.", 409)
        try:
            is_expired = datetime.fromisoformat(challenge["expires_at"]) <= now
        except (TypeError, ValueError):
            is_expired = True
        if is_expired:
            conn.execute(
                "UPDATE ownership_challenges SET status='expired' WHERE id=?",
                (challenge_id,)
            )
            return json_error("Срок действия заявки истёк. Создайте новую заявку.", 410)
        updated = conn.execute("""
            UPDATE ownership_challenges
            SET status='submitted', screenshot_data=?, screenshot_mime=?,
                screenshot_filename=?, screenshot_uploaded_at=?
            WHERE id=? AND status='awaiting_upload'
        """, (
            sqlite3.Binary(image_data), image_mime, image_filename,
            now.isoformat(), challenge_id
        ))
        if updated.rowcount != 1:
            return json_error("Заявка уже отправлена или заменена", 409)

    return web.json_response({
        "status": "submitted",
        "challenge_id": challenge_id,
        "message": "Скриншот отправлен на проверку модератору LINKCHECK"
    })



async def api_ownership_check(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Переоткройте Mini App.", 401)

    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        method = str(data.get("method") or "").strip().lower()
        if project_id <= 0 or method not in {"telegram", "dns"}:
            return json_error("Некорректный способ подтверждения")
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")

    with db_connect() as conn:
        project = conn.execute("""
            SELECT id, owner_tg_id, title, link, category, website, ownership_status
            FROM projects
            WHERE id=? AND owner_tg_id=?
            LIMIT 1
        """, (project_id, user["id"])).fetchone()

    if not project:
        return json_error("Проект не найден или не принадлежит вам", 404)
    if project["ownership_status"] == "verified":
        return web.json_response({"status": "verified", "method": method})

    if method == "telegram":
        if str(project["category"] or "").lower() not in {"channel", "group"}:
            return json_error("Проверка через Telegram доступна только для каналов и групп", 400)
        try:
            bot_identity = await bot.get_me()
            bot_member = await bot.get_chat_member(
                chat_id=project["link"],
                user_id=bot_identity.id
            )
            if str(getattr(bot_member, "status", "")).lower() not in {"administrator", "creator"}:
                return json_error(
                    "Сначала добавьте @%s администратором этого канала или группы, затем повторите проверку."
                    % (bot_identity.username or "linkcheck_verify_bot"),
                    409
                )
            applicant_member = await bot.get_chat_member(
                chat_id=project["link"],
                user_id=user["id"]
            )
            applicant_status = str(getattr(applicant_member, "status", "")).lower()
            if applicant_status not in {"administrator", "creator"}:
                return json_error(
                    "Ваш аккаунт не является владельцем или администратором этого Telegram-ресурса.",
                    403
                )
        except Exception as exc:
            logging.warning(
                "Ownership Telegram check failed: project_id=%s user_id=%s error=%s",
                project_id, user["id"], exc
            )
            return json_error(
                "Telegram не смог проверить участников. Убедитесь, что LINKCHECK-бот добавлен администратором в нужный канал или группу.",
                409
            )

    else:
        domain = ownership_website_host(project["website"])
        if not domain:
            return json_error("Для DNS-подтверждения укажите корректный сайт проекта", 400)

        with db_connect() as conn:
            challenge = conn.execute("""
                SELECT id, domain, token_hash, expires_at, attempts
                FROM ownership_challenges
                WHERE project_id=? AND owner_tg_id=? AND method='dns' AND status='pending'
                ORDER BY id DESC LIMIT 1
            """, (project_id, user["id"])).fetchone()
            if not challenge:
                return json_error("Сначала нажмите «Подтвердить владение» и получите DNS-запись", 409)
            if challenge["domain"] != domain:
                conn.execute(
                    "UPDATE ownership_challenges SET status='invalidated' WHERE id=?",
                    (challenge["id"],)
                )
                return json_error("Сайт проекта изменился. Запросите новую DNS-запись.", 409)
            if datetime.fromisoformat(challenge["expires_at"]) <= datetime.now(timezone.utc):
                conn.execute(
                    "UPDATE ownership_challenges SET status='expired' WHERE id=?",
                    (challenge["id"],)
                )
                return json_error("Срок действия DNS-записи истёк. Запросите новую.", 410)
            if int(challenge["attempts"] or 0) >= 20:
                return json_error("Превышен лимит проверок. Запросите новую DNS-запись.", 429)
            conn.execute(
                "UPDATE ownership_challenges SET attempts=attempts+1 WHERE id=?",
                (challenge["id"],)
            )
            token_hash = str(challenge["token_hash"] or "")
            record_name = "_linkcheck." + domain

        try:
            txt_values = await ownership_dns_txt_values(record_name)
        except Exception:
            logging.exception("Ownership DNS lookup failed for project_id=%s", project_id)
            return json_error("Не удалось проверить DNS. Попробуйте немного позже.", 503)

        expected_value = None
        for value in txt_values:
            prefix = "linkcheck-verification="
            if value.startswith(prefix):
                candidate_hash = hashlib.sha256(value[len(prefix):].encode("utf-8")).hexdigest()
                if hmac.compare_digest(candidate_hash, token_hash):
                    expected_value = value
                    break
        if expected_value is None:
            return json_error(
                "DNS-запись пока не найдена. Проверьте имя и значение TXT-записи; обновление DNS может занять время.",
                409
            )

        with db_connect() as conn:
            conn.execute(
                "UPDATE ownership_challenges SET status='verified' WHERE id=?",
                (challenge["id"],)
            )

    with db_connect() as conn:
        conn.execute(
            "UPDATE projects SET ownership_status='verified' WHERE id=? AND owner_tg_id=?",
            (project_id, user["id"])
        )
        conn.execute(
            "UPDATE ownership_challenges SET status='verified' "
            "WHERE project_id=? AND owner_tg_id=? AND method='telegram' AND status='pending'",
            (project_id, user["id"])
        )

    logging.info(
        "Project ownership verified: project_id=%s owner_tg_id=%s method=%s",
        project_id, user["id"], method
    )
    return web.json_response({"status": "verified", "method": method})


# ============================================================
# ЖАЛОБЫ НА ПРОЕКТЫ
# ============================================================

REPORT_REASONS = {
    "scam", "impersonation", "invalid_link", "spam", "other"
}


async def api_report_project(request):
    """Принимает жалобу Telegram-пользователя на опубликованный проект."""
    user = authenticated_user(request)
    if not user:
        return json_error("Unauthorized", 401)

    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        reason = data.get("reason", "")
        description = data.get("description", "")
        if project_id <= 0:
            return json_error("Некорректный ID проекта")
        if not isinstance(reason, str) or reason not in REPORT_REASONS:
            return json_error("Выберите корректную причину жалобы")
        if not isinstance(description, str) or len(description) > 1000:
            return json_error("Описание жалобы не должно превышать 1000 символов")
        description = description.strip()
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")

    upsert_user(user["id"], user.get("username"))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT id, owner_tg_id FROM projects "
            "WHERE id=? AND moderation_status='approved' LIMIT 1",
            (project_id,)
        ).fetchone()
        if not project:
            return json_error("Опубликованный проект не найден", 404)
        if project["owner_tg_id"] is not None and int(project["owner_tg_id"]) == user["id"]:
            return json_error("Нельзя отправить жалобу на собственный проект", 403)

        existing = conn.execute(
            "SELECT id FROM reports WHERE author_tg_id=? AND project_id=? "
            "AND status='pending' LIMIT 1",
            (user["id"], project_id)
        ).fetchone()
        if existing:
            return json_error("Вы уже отправили жалобу на этот проект. Она ожидает рассмотрения.", 409)

        recent_count = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE author_tg_id=? AND created_at>=?",
            (user["id"], cutoff)
        ).fetchone()[0]
        if recent_count >= 10:
            return json_error("Достигнут лимит жалоб: не более 10 за 24 часа", 429)

        conn.execute(
            "INSERT INTO reports (author_tg_id, project_id, reason, description, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (user["id"], project_id, reason, description, now_iso())
        )

    return web.json_response({"status": "ok", "message": "Жалоба отправлена на рассмотрение"})


async def api_official_verification_request(request):
    """Принимает заявку владельца на проверку официального источника."""
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Переоткройте Mini App.", 401)

    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        source_url = valid_website(data.get("source_url", ""))
        note = data.get("note", "")
        if project_id <= 0:
            return json_error("Некорректный ID проекта")
        if not source_url:
            return json_error("Укажите официальный сайт или независимый источник")
        if not isinstance(note, str) or len(note) > 500:
            return json_error("Пояснение не должно превышать 500 символов")
        note = note.strip()
    except (ValueError, TypeError, web.HTTPBadRequest) as exc:
        return json_error(str(exc) or "Некорректный запрос")

    upsert_user(user["id"], user.get("username"))
    with db_connect() as conn:
        project = conn.execute(
            "SELECT id, owner_tg_id, title, link, moderation_status, is_official "
            "FROM projects WHERE id=? LIMIT 1",
            (project_id,)
        ).fetchone()
        if not project or project["owner_tg_id"] is None or int(project["owner_tg_id"]) != user["id"]:
            return json_error("Проект не найден или не принадлежит вам", 404)
        if project["moderation_status"] != "approved":
            return json_error("Сначала проект должен пройти публикационную модерацию")
        if project["is_official"]:
            return json_error("У проекта уже подтверждён статус OFFICIAL RESOURCE", 409)
        pending = conn.execute(
            "SELECT id FROM official_verification_requests WHERE project_id=? AND status='pending' LIMIT 1",
            (project_id,)
        ).fetchone()
        if pending:
            return json_error("Заявка OFFICIAL RESOURCE уже ожидает проверки", 409)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        daily_count = conn.execute(
            "SELECT COUNT(*) FROM official_verification_requests "
            "WHERE requester_tg_id=? AND created_at>=?",
            (user["id"], cutoff)
        ).fetchone()[0]
        if daily_count >= 5:
            return json_error("Лимит заявок OFFICIAL RESOURCE: не более 5 за 24 часа", 429)

    automatic = await check_official_source_url(source_url, project["link"])
    now = now_iso()
    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT owner_tg_id, moderation_status, is_official FROM projects WHERE id=? LIMIT 1",
            (project_id,)
        ).fetchone()
        if not current or current["owner_tg_id"] is None or int(current["owner_tg_id"]) != user["id"]:
            return json_error("Проект не найден или больше не принадлежит вам", 404)
        if current["moderation_status"] != "approved":
            return json_error("Проект больше не опубликован. Заявка не создана", 409)
        if current["is_official"]:
            return json_error("У проекта уже подтверждён статус OFFICIAL RESOURCE", 409)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        daily_count = conn.execute(
            "SELECT COUNT(*) FROM official_verification_requests "
            "WHERE requester_tg_id=? AND created_at>=?",
            (user["id"], cutoff)
        ).fetchone()[0]
        if daily_count >= 5:
            return json_error("Лимит заявок OFFICIAL RESOURCE: не более 5 за 24 часа", 429)
        duplicate = conn.execute(
            "SELECT id FROM official_verification_requests WHERE project_id=? AND status='pending' LIMIT 1",
            (project_id,)
        ).fetchone()
        if duplicate:
            return json_error("Заявка OFFICIAL RESOURCE уже ожидает проверки", 409)
        try:
            cursor = conn.execute("""
                INSERT INTO official_verification_requests
                    (project_id, requester_tg_id, source_url, note, status,
                     automatic_status, automatic_detail, http_status, checked_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """, (
                project_id, user["id"], source_url, note or None,
                automatic.get("status", "unavailable"),
                automatic.get("detail", ""), automatic.get("http_status"), now, now
            ))
        except sqlite3.IntegrityError:
            return json_error("Заявка OFFICIAL RESOURCE уже ожидает проверки", 409)
        request_id = cursor.lastrowid

    logging.info(
        "OFFICIAL RESOURCE request submitted: request_id=%s project_id=%s automatic_status=%s",
        request_id, project_id, automatic.get("status")
    )
    return web.json_response({
        "status": "ok",
        "request_id": request_id,
        "automatic_status": automatic.get("status", "unavailable"),
        "automatic_detail": automatic.get("detail", ""),
        "message": "Заявка отправлена администратору. Автоматическая проверка является подсказкой и не присваивает OFFICIAL RESOURCE."
    })


# ============================================================
# АДМИН API
# ============================================================

def admin_user(request):
    user = authenticated_user(request)
    if not user:
        return None
    return user if user["id"] in ADMIN_IDS else None


async def api_admin_official_requests(request):
    if not admin_user(request):
        return json_error("Forbidden", 403)
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT ov.id, ov.project_id, ov.requester_tg_id, ov.source_url, ov.note,
                   ov.automatic_status, ov.automatic_detail, ov.http_status,
                   ov.checked_at, ov.created_at,
                   p.title AS project_title, p.link AS project_link,
                   p.category AS project_category, p.website AS project_website,
                   p.is_official, p.linkcheck_badge_manual,
                   u.username AS requester_username
            FROM official_verification_requests ov
            JOIN projects p ON p.id=ov.project_id
            LEFT JOIN users u ON u.tg_id=ov.requester_tg_id
            WHERE ov.status='pending'
            ORDER BY ov.created_at ASC, ov.id ASC
            LIMIT 100
        """).fetchall()
    return web.json_response([{
        "id": row["id"],
        "project_id": row["project_id"],
        "requester_tg_id": row["requester_tg_id"],
        "requester_username": row["requester_username"] or "",
        "project_title": row["project_title"] or "",
        "project_link": row["project_link"] or "",
        "project_category": row["project_category"] or "other",
        "project_website": row["project_website"] or "",
        "source_url": row["source_url"] or "",
        "note": row["note"] or "",
        "automatic_status": row["automatic_status"] or "unavailable",
        "automatic_detail": row["automatic_detail"] or "",
        "http_status": row["http_status"],
        "checked_at": row["checked_at"] or "",
        "created_at": row["created_at"] or "",
        **linkcheck_badge_payload(row)
    } for row in rows])


async def api_admin_official_action(request):
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)
    try:
        data = await read_json(request)
        request_id = int(data.get("request_id", 0))
        action = str(data.get("action", "")).strip().lower()
        review_note = data.get("review_note", "")
        if request_id <= 0 or action not in {"approve", "reject"}:
            return json_error("Некорректное действие или ID заявки")
        if not isinstance(review_note, str) or len(review_note) > 1000:
            return json_error("Комментарий не должен превышать 1000 символов")
        review_note = review_note.strip()
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT ov.id, ov.project_id, ov.source_url, ov.automatic_status,
                   ov.status, p.title, p.is_official
            FROM official_verification_requests ov
            JOIN projects p ON p.id=ov.project_id
            WHERE ov.id=? LIMIT 1
        """, (request_id,)).fetchone()
        if not row or row["status"] != "pending":
            return json_error("Заявка уже обработана или не найдена", 404)
        if action == "approve":
            if row["is_official"]:
                return json_error("Проект уже имеет статус OFFICIAL RESOURCE", 409)
            if row["automatic_status"] != "linked" and not review_note:
                return json_error(
                    "Автопроверка не нашла подтверждающую ссылку. Для ручного одобрения укажите основание в комментарии.",
                    400
                )
            conn.execute(
                "UPDATE projects SET is_official=1, source_url=? WHERE id=?",
                (row["source_url"], row["project_id"])
            )
            conn.execute("""
                UPDATE official_verification_requests
                SET status='approved', reviewed_by=?, reviewed_at=?, review_note=?
                WHERE id=? AND status='pending'
            """, (admin["id"], now_iso(), review_note or None, request_id))
            comment = (
                "OFFICIAL RESOURCE подтверждён. Источник: " + row["source_url"] +
                ". Автопроверка: " + str(row["automatic_status"]) +
                (". Основание: " + review_note if review_note else "")
            )
            write_moderation_log(conn, admin["id"], row["project_id"], "official_resource_approved", comment)
            message = "Статус OFFICIAL RESOURCE подтверждён"
        else:
            conn.execute("""
                UPDATE official_verification_requests
                SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=?
                WHERE id=? AND status='pending'
            """, (admin["id"], now_iso(), review_note or None, request_id))
            comment = "Запрос OFFICIAL RESOURCE отклонён" + (
                ". Причина: " + review_note if review_note else ""
            )
            write_moderation_log(conn, admin["id"], row["project_id"], "official_resource_rejected", comment)
            message = "Запрос OFFICIAL RESOURCE отклонён"

    return web.json_response({"status": "ok", "message": message})


async def api_admin_moderation_log(request):
    """Возвращает журнал действий модерации только администраторам."""
    if not admin_user(request):
        return json_error("Forbidden", 403)

    try:
        limit = int(request.query.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 200))

    with db_connect() as conn:
        rows = conn.execute("""
            SELECT ml.id, ml.admin_tg_id, ml.project_id, ml.action,
                   ml.comment, ml.created_at,
                   admin_user.username AS admin_username,
                   p.title AS project_title, p.link AS project_link
            FROM moderation_log ml
            LEFT JOIN users AS admin_user ON admin_user.tg_id=ml.admin_tg_id
            LEFT JOIN projects AS p ON p.id=ml.project_id
            ORDER BY ml.id DESC
            LIMIT ?
        """, (limit,)).fetchall()

    return web.json_response([{
        "id": int(row["id"]),
        "admin_tg_id": row["admin_tg_id"],
        "admin_username": row["admin_username"] or "",
        "project_id": row["project_id"],
        "project_title": row["project_title"] or "",
        "project_link": row["project_link"] or "",
        "action": row["action"] or "unknown",
        "comment": row["comment"] or "",
        "created_at": row["created_at"] or ""
    } for row in rows])


async def api_admin_reports(request):
    if not admin_user(request):
        return json_error("Forbidden", 403)
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT r.id, r.project_id, r.author_tg_id, r.reason, r.description,
                   r.status, r.created_at,
                   p.title AS project_title, p.link AS project_link,
                   p.is_official, p.linkcheck_badge_manual,
                   u.username AS author_username
            FROM reports r
            LEFT JOIN projects p ON p.id=r.project_id
            LEFT JOIN users u ON u.tg_id=r.author_tg_id
            WHERE r.status='pending'
            ORDER BY r.created_at ASC, r.id ASC
        """).fetchall()
    return web.json_response([
        {
            "id": row["id"],
            "project_id": row["project_id"],
            "project_title": row["project_title"] or "Проект удалён",
            "project_link": row["project_link"] or "",
            "author_tg_id": row["author_tg_id"],
            "author_username": row["author_username"] or "",
            "reason": row["reason"] or "other",
            "description": row["description"] or "",
            "created_at": row["created_at"] or "",
            **linkcheck_badge_payload(row)
        } for row in rows
    ])


async def api_admin_report_action(request):
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)
    try:
        data = await read_json(request)
        report_id = int(data.get("report_id", 0))
        action = data.get("action", "")
        if report_id <= 0 or action not in {"reviewed", "dismissed"}:
            return json_error("Некорректное действие или ID жалобы")
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        report = conn.execute(
            "SELECT id, project_id FROM reports WHERE id=? AND status='pending' LIMIT 1",
            (report_id,)
        ).fetchone()
        if not report:
            return json_error("Жалоба не найдена или уже обработана", 404)
        updated = conn.execute(
            "UPDATE reports SET status=? WHERE id=? AND status='pending'",
            (action, report_id)
        )
        if updated.rowcount != 1:
            return json_error("Жалоба уже обработана", 409)
        comment = "Жалоба рассмотрена" if action == "reviewed" else "Жалоба отклонена"
        write_moderation_log(
            conn, admin["id"], report["project_id"], f"report_{action}",
            f"Жалоба ID {report_id}: {comment}"
        )
    return web.json_response({"status": "ok", "message": "Действие выполнено"})


async def api_admin_ownership(request):
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT oc.id AS challenge_id, oc.project_id, oc.owner_tg_id,
                   oc.method, oc.manual_note, oc.created_at,
                   oc.screenshot_uploaded_at, oc.screenshot_mime,
                   p.title AS project_title, p.link AS project_link,
                   p.is_official, p.linkcheck_badge_manual,
                   u.username AS owner_username
            FROM ownership_challenges oc
            JOIN projects p ON p.id=oc.project_id
            LEFT JOIN users u ON u.tg_id=oc.owner_tg_id
            WHERE oc.status='submitted'
              AND (
                (oc.method='bot_manual' AND oc.screenshot_data IS NOT NULL)
                OR oc.method='manual_admin'
              )
            ORDER BY oc.created_at ASC, oc.id ASC
            LIMIT 100
        """).fetchall()
    return web.json_response([{
        "challenge_id": row["challenge_id"],
        "project_id": row["project_id"],
        "owner_tg_id": row["owner_tg_id"],
        "owner_username": row["owner_username"] or "",
        "project_title": row["project_title"] or "",
        "project_link": row["project_link"] or "",
        "method": row["method"],
        "manual_note": row["manual_note"] or "",
        "created_at": row["created_at"] or "",
        "screenshot_uploaded_at": row["screenshot_uploaded_at"] or "",
        "screenshot_mime": row["screenshot_mime"] or "image/jpeg",
        **linkcheck_badge_payload(row)
    } for row in rows])


async def api_admin_ownership_screenshot(request):
    if not admin_user(request):
        return json_error("Forbidden", 403)
    try:
        challenge_id = int(request.match_info.get("challenge_id", "0"))
    except (ValueError, TypeError):
        return json_error("Некорректный ID заявки", 400)
    with db_connect() as conn:
        row = conn.execute("""
            SELECT screenshot_data, screenshot_mime
            FROM ownership_challenges
            WHERE id=? AND method='bot_manual' AND status='submitted'
            LIMIT 1
        """, (challenge_id,)).fetchone()
    if not row or not row["screenshot_data"]:
        return json_error("Скриншот не найден", 404)
    return web.Response(
        body=bytes(row["screenshot_data"]),
        content_type=row["screenshot_mime"] or "image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    )


async def api_admin_ownership_action(request):
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)
    try:
        data = await read_json(request)
        challenge_id = int(data.get("challenge_id", 0))
        action = str(data.get("action", "")).strip().lower()
        review_note = str(data.get("review_note") or "").strip()[:500]
        if challenge_id <= 0 or action not in {"approve", "reject"}:
            return json_error("Некорректное действие или ID заявки")
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT oc.id, oc.project_id, oc.owner_tg_id, oc.status,
                   oc.method, oc.manual_note, p.title, p.link
            FROM ownership_challenges oc
            JOIN projects p ON p.id=oc.project_id
            WHERE oc.id=? AND oc.method IN ('bot_manual', 'manual_admin')
            LIMIT 1
        """, (challenge_id,)).fetchone()
        if not row or row["status"] != "submitted":
            return json_error("Заявка уже обработана или не найдена", 404)
        is_manual_request = row["method"] == "manual_admin"
        if action == "approve":
            conn.execute(
                "UPDATE projects SET ownership_status='verified' WHERE id=? AND owner_tg_id=?",
                (row["project_id"], row["owner_tg_id"])
            )
            conn.execute(
                "UPDATE ownership_challenges "
                "SET status='verified', reviewed_by=?, reviewed_at=?, review_note=? "
                "WHERE id=? AND status='submitted'",
                (admin["id"], now_iso(), review_note or None, challenge_id)
            )
            log_action = "ownership_manual_admin_approved" if is_manual_request else "ownership_bot_manual_approved"
            default_comment = (
                "Владение подтверждено администратором после ручной проверки"
                if is_manual_request else
                "Владение Telegram-ботом подтверждено по скриншоту BotFather"
            )
            write_moderation_log(
                conn, admin["id"], row["project_id"], log_action,
                default_comment + (". Комментарий: " + review_note if review_note else "")
            )
            result_message = "Владение подтверждено"
        else:
            conn.execute(
                "UPDATE ownership_challenges "
                "SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=? "
                "WHERE id=? AND status='submitted'",
                (admin["id"], now_iso(), review_note or None, challenge_id)
            )
            log_action = "ownership_manual_admin_rejected" if is_manual_request else "ownership_bot_manual_rejected"
            default_comment = (
                "Запрос на ручное подтверждение отклонён администратором"
                if is_manual_request else
                "Скриншот BotFather не принят модератором"
            )
            write_moderation_log(
                conn, admin["id"], row["project_id"], log_action,
                default_comment + (". Комментарий: " + review_note if review_note else "")
            )
            result_message = "Подтверждение отклонено"

    return web.json_response({
        "status": "ok",
        "action": action,
        "message": result_message,
        "project_id": row["project_id"]
    })


async def api_admin_users(request):
    if not admin_user(request):
        return json_error("Forbidden", 403)
    query = str(request.query.get("q", "")).strip()[:100]
    with db_connect() as conn:
        if query:
            pattern = f"%{query.lower()}%"
            rows = conn.execute("""
                SELECT tg_id, username, is_trusted, has_requested_access
                FROM users
                WHERE CAST(tg_id AS TEXT) LIKE ? OR lower(COALESCE(username, '')) LIKE ?
                ORDER BY has_requested_access DESC, created_at DESC
            """, (f"%{query}%", pattern)).fetchall()
        else:
            rows = conn.execute("""
                SELECT tg_id, username, is_trusted, has_requested_access
                FROM users ORDER BY has_requested_access DESC, created_at DESC
            """).fetchall()
    return web.json_response([
        {
            "id": row["tg_id"], "username": row["username"],
            "is_trusted": bool(row["is_trusted"]),
            "has_requested_access": bool(row["has_requested_access"])
        } for row in rows
    ])


async def api_admin_toggle_trust(request):
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)
    try:
        data = await read_json(request)
        target_id = int(data.get("tg_id", 0))
        action = str(data.get("action", "")).strip().lower()
        if target_id <= 0 or action not in {"trust", "untrust"}:
            return json_error("Некорректные параметры")
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")
    with db_connect() as conn:
        target = conn.execute(
            "SELECT username FROM users WHERE tg_id=? LIMIT 1", (target_id,)
        ).fetchone()
        if not target:
            return json_error("Пользователь не найден", 404)
        if action == "trust":
            conn.execute(
                "UPDATE users SET is_trusted=1, has_requested_access=0 WHERE tg_id=?",
                (target_id,)
            )
            log_action = "user_trust_granted"
            action_text = "Права на добавление проектов выданы"
        else:
            conn.execute("UPDATE users SET is_trusted=0 WHERE tg_id=?", (target_id,))
            log_action = "user_trust_revoked"
            action_text = "Права на добавление проектов отозваны"
        username = (target["username"] or "").strip()
        target_label = f"@{username}" if username else f"Telegram ID {target_id}"
        write_moderation_log(
            conn, admin["id"], None, log_action,
            f"{action_text}: {target_label} (ID {target_id})"
        )
    return web.json_response({"status": "ok"})


async def api_admin_requests(request):
    if not admin_user(request):
        return json_error("Forbidden", 403)
    try:
        with db_connect() as conn:
            rows = conn.execute("""
                SELECT id, title, link, category, description, website, moderation_status,
                       ownership_status, is_official, telegram_verified, source_url,
                       topic, community_type, created_at, linkcheck_badge_manual,
                       linkcheck_badge_manual_at, linkcheck_badge_manual_reason
                FROM projects
                WHERE moderation_status='pending'
                ORDER BY id ASC
                LIMIT 300
            """).fetchall()
    except sqlite3.Error:
        logging.exception("Не удалось загрузить заявки на модерацию из SQLite")
        return json_error("Не удалось прочитать заявки из базы данных. Повторите попытку.", 503)
    return web.json_response([
        {
            "id": row["id"], "title": row["title"], "link": row["link"],
            "category": row["category"], "description": row["description"],
            "website": row["website"],
            "mod_status": row["moderation_status"],
            "status_text": "Новая заявка на публикацию",
            "ownership_status": row["ownership_status"],
            "is_official": bool(row["is_official"]),
            "telegram_verified": bool(row["telegram_verified"]),
            "source_url": row["source_url"], "topic": row["topic"],
            "community_type": row["community_type"],
            "created_at": row["created_at"] or "",
            **linkcheck_badge_payload(row)
        } for row in rows
    ])


async def api_admin_catalog(request):
    if not admin_user(request):
        return json_error("Forbidden", 403)
    query = str(request.query.get("q", "")).strip()[:150]
    with db_connect() as conn:
        if query:
            pattern = f"%{query.lower()}%"
            rows = conn.execute("""
                SELECT id, title, link, category, description, website, moderation_status, ownership_status,
                       is_official, telegram_verified, source_url, is_seed, topic, community_type,
                       created_at, linkcheck_badge_manual, linkcheck_badge_manual_by,
                       linkcheck_badge_manual_at, linkcheck_badge_manual_reason
                FROM projects
                WHERE lower(COALESCE(title, '')) LIKE ?
                   OR lower(COALESCE(link, '')) LIKE ?
                   OR lower(COALESCE(category, '')) LIKE ?
                   OR lower(COALESCE(website, '')) LIKE ?
                   OR CAST(id AS TEXT) LIKE ?
                ORDER BY CASE WHEN is_official=1 OR ownership_status='verified' THEN 0
                              WHEN telegram_verified=1 THEN 1 ELSE 2 END ASC,
                         is_seed DESC, title COLLATE NOCASE ASC
            """, (pattern, pattern, pattern, pattern, f"%{query}%")).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, title, link, category, description, website, moderation_status, ownership_status,
                       is_official, telegram_verified, source_url, is_seed, topic, community_type,
                       created_at, linkcheck_badge_manual, linkcheck_badge_manual_by,
                       linkcheck_badge_manual_at, linkcheck_badge_manual_reason
                FROM projects
                ORDER BY CASE WHEN is_official=1 OR ownership_status='verified' THEN 0
                              WHEN telegram_verified=1 THEN 1 ELSE 2 END ASC,
                         is_seed DESC, title COLLATE NOCASE ASC
            """).fetchall()
    return web.json_response([
        {
            "id": row["id"], "title": row["title"], "link": row["link"],
            "category": row["category"], "description": row["description"] or "",
            "website": row["website"] or "", "mod_status": row["moderation_status"],
            "ownership_status": row["ownership_status"],
            "is_official": bool(row["is_official"]),
            "telegram_verified": bool(row["telegram_verified"]),
            "source_url": row["source_url"], "is_seed": bool(row["is_seed"]),
            "topic": row["topic"], "community_type": row["community_type"],
            "created_at": row["created_at"] or "",
            **linkcheck_badge_payload(row)
        } for row in rows
    ])


async def api_admin_badge_action(request):
    """Выдаёт или снимает фирменный значок LINKCHECK вручную."""
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)

    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        action = str(data.get("action") or "").strip().lower()
        reason = data.get("reason", "")
        if project_id <= 0 or action not in {"grant", "revoke"}:
            return json_error("Некорректное действие или ID проекта", 400)
        if not isinstance(reason, str) or len(reason) > 500:
            return json_error("Комментарий не должен превышать 500 символов", 400)
        reason = reason.strip()
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос", 400)

    with db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = conn.execute(
            "SELECT id, title, is_official, linkcheck_badge_manual "
            "FROM projects WHERE id=? LIMIT 1",
            (project_id,)
        ).fetchone()
        if not project:
            return json_error("Проект не найден", 404)

        if action == "grant":
            conn.execute("""
                UPDATE projects
                SET linkcheck_badge_manual=1,
                    linkcheck_badge_manual_by=?,
                    linkcheck_badge_manual_at=?,
                    linkcheck_badge_manual_reason=?
                WHERE id=?
            """, (admin["id"], now_iso(), reason or None, project_id))
            log_comment = "Фирменный значок LINKCHECK выдан вручную"
            if reason:
                log_comment += ". Основание: " + reason
            write_moderation_log(conn, admin["id"], project_id, "linkcheck_badge_manual_granted", log_comment)
        else:
            if not project["linkcheck_badge_manual"]:
                return json_error("Ручная выдача значка для этого проекта отсутствует", 409)
            conn.execute("""
                UPDATE projects
                SET linkcheck_badge_manual=0,
                    linkcheck_badge_manual_by=NULL,
                    linkcheck_badge_manual_at=NULL,
                    linkcheck_badge_manual_reason=NULL
                WHERE id=?
            """, (project_id,))
            log_comment = "Ручная выдача фирменного значка LINKCHECK снята"
            if reason:
                log_comment += ". Комментарий: " + reason
            write_moderation_log(conn, admin["id"], project_id, "linkcheck_badge_manual_revoked", log_comment)

        fresh = conn.execute(
            "SELECT is_official, linkcheck_badge_manual FROM projects WHERE id=?",
            (project_id,)
        ).fetchone()

    badge = bool(fresh["is_official"] or fresh["linkcheck_badge_manual"])
    source = "official" if fresh["is_official"] else ("manual" if fresh["linkcheck_badge_manual"] else None)
    return web.json_response({
        "status": "ok",
        "project_id": project_id,
        "linkcheck_badge": badge,
        "linkcheck_badge_source": source,
        "message": "Фирменный значок выдан" if action == "grant" else "Ручная выдача значка снята"
    })


async def api_admin_action(request):
    admin = admin_user(request)
    if not admin:
        return json_error(
            "Нет прав администратора или Telegram-авторизация устарела. Переоткройте Mini App.",
            403
        )
    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        action = data.get("action")
        if project_id <= 0 or action not in {"approve", "reject", "delete"}:
            return json_error("Некорректное действие или ID проекта")
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")

    with db_connect() as conn:
        project = conn.execute("SELECT id, title FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            return json_error("Проект не найден", 404)
        if action == "approve":
            conn.execute("UPDATE projects SET moderation_status='approved' WHERE id=?", (project_id,))
            write_moderation_log(conn, admin["id"], project_id, action, "Проект одобрен")
        elif action == "reject":
            conn.execute("UPDATE projects SET moderation_status='rejected' WHERE id=?", (project_id,))
            conn.execute(
                "UPDATE official_verification_requests "
                "SET status='invalidated', review_note='Проект отклонён при публикационной модерации' "
                "WHERE project_id=? AND status='pending'",
                (project_id,)
            )
            write_moderation_log(conn, admin["id"], project_id, action, "Проект отклонён")
        else:
            conn.execute("DELETE FROM reports WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            write_moderation_log(
                conn, admin["id"], None, action,
                f"Удалён проект ID {project_id}: {project['title']}"
            )
    return web.json_response({"status": "ok", "message": "Действие выполнено"})

# ============================================================
# LINKCHECK PLUS — WATCHLIST, ИСТОРИЯ, УВЕДОМЛЕНИЯ И АНАЛИТИКА
# ============================================================

WATCHED_PROJECT_FIELDS = (
    "title", "link", "description", "website", "topic", "moderation_status",
    "ownership_status", "is_official", "telegram_verified", "linkcheck_badge_manual"
)


def project_watch_state(row):
    """Публичный срез карточки для обнаружения реальных изменений."""
    keys = set(row.keys()) if hasattr(row, "keys") else set(row or {})
    state = {}
    for key in WATCHED_PROJECT_FIELDS:
        if key not in keys:
            continue
        value = row[key]
        if key in {"is_official", "telegram_verified", "linkcheck_badge_manual"}:
            value = bool(value)
        else:
            value = str(value or "")
        state[key] = value
    return state


def describe_project_changes(before, after):
    labels = {
        "title": "Название проекта",
        "link": "Ссылка проекта",
        "description": "Описание проекта",
        "website": "Официальный сайт",
        "topic": "Тематика каталога",
        "moderation_status": "Статус публикации",
        "ownership_status": "Статус подтверждения владения",
        "is_official": "Статус OFFICIAL RESOURCE",
        "telegram_verified": "Статус Telegram Verified",
        "linkcheck_badge_manual": "Ручной бейдж LINKCHECK",
    }
    changed = [key for key in WATCHED_PROJECT_FIELDS if before.get(key) != after.get(key)]
    if not changed:
        return [], "Изменения карточки проекта", "В карточке проекта обновились сведения."
    title = "Обновление проекта"
    if "moderation_status" in changed:
        title = "Изменился статус публикации"
    elif "ownership_status" in changed:
        title = "Изменился статус владения"
    elif "is_official" in changed:
        title = "Изменился статус OFFICIAL RESOURCE"
    elif "telegram_verified" in changed:
        title = "Изменился статус Telegram Verified"
    elif "linkcheck_badge_manual" in changed:
        title = "Изменился бейдж LINKCHECK"
    message = ", ".join(labels.get(key, key) for key in changed)
    return changed, title, message + ". Откройте карточку, чтобы посмотреть актуальные сведения."


def scan_project_changes(conn):
    """Сверяет отслеживаемые карточки и создаёт уведомления наблюдателям/владельцу."""
    rows = conn.execute("""
        SELECT p.* FROM projects p
        WHERE p.owner_tg_id IS NOT NULL
           OR EXISTS (SELECT 1 FROM project_watchlist w WHERE w.project_id=p.id)
    """).fetchall()
    for row in rows:
        project_id = int(row["id"])
        current = project_watch_state(row)
        current_json = json.dumps(current, ensure_ascii=False, sort_keys=True)
        snapshot = conn.execute(
            "SELECT snapshot_json FROM project_watch_snapshots WHERE project_id=?",
            (project_id,)
        ).fetchone()
        if not snapshot:
            conn.execute(
                "INSERT INTO project_watch_snapshots(project_id,snapshot_json,updated_at) VALUES(?,?,?)",
                (project_id, current_json, now_iso())
            )
            continue
        try:
            previous = json.loads(snapshot["snapshot_json"] or "{}")
            if not isinstance(previous, dict):
                previous = {}
        except (ValueError, TypeError):
            previous = {}
        changed, title, message = describe_project_changes(previous, current)
        if not changed:
            continue
        created_at = now_iso()
        before_json = json.dumps(previous, ensure_ascii=False, sort_keys=True)
        conn.execute("""
            INSERT INTO project_change_history
            (project_id,changed_fields,before_json,after_json,created_at)
            VALUES(?,?,?,?,?)
        """, (
            project_id, json.dumps(changed, ensure_ascii=False),
            before_json, current_json, created_at
        ))
        recipients = {
            int(item[0]) for item in conn.execute(
                "SELECT user_tg_id FROM project_watchlist WHERE project_id=?",
                (project_id,)
            ).fetchall()
        }
        if row["owner_tg_id"] is not None:
            recipients.add(int(row["owner_tg_id"]))
        for recipient_id in recipients:
            conn.execute("""
                INSERT INTO notifications
                (recipient_tg_id,project_id,event_type,title,message,created_at)
                VALUES(?,?,?,?,?,?)
            """, (
                recipient_id, project_id, ",".join(changed),
                title, message, created_at
            ))
            # Не храним бесконечную ленту: оставляем 500 последних уведомлений пользователя.
            conn.execute("""
                DELETE FROM notifications
                WHERE recipient_tg_id=? AND id NOT IN (
                    SELECT id FROM notifications
                    WHERE recipient_tg_id=? ORDER BY id DESC LIMIT 500
                )
            """, (recipient_id, recipient_id))
        conn.execute(
            "UPDATE project_watch_snapshots SET snapshot_json=?,updated_at=? WHERE project_id=?",
            (current_json, created_at, project_id)
        )
        # Ограничиваем рост служебных таблиц: оставляем 100 последних событий проекта.
        conn.execute("""
            DELETE FROM project_change_history
            WHERE project_id=? AND id NOT IN (
                SELECT id FROM project_change_history WHERE project_id=? ORDER BY id DESC LIMIT 100
            )
        """, (project_id, project_id))


async def api_watchlist(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Откройте Mini App через Telegram.", 401)
    user_id = int(user["id"])
    if request.method == "POST":
        try:
            data = await read_json(request)
            project_id = int(data.get("project_id", 0))
            action = str(data.get("action") or "add").strip().lower()
            if project_id <= 0 or action not in {"add", "remove"}:
                return json_error("Некорректный запрос избранного")
        except (ValueError, TypeError, web.HTTPBadRequest):
            return json_error("Некорректный запрос избранного")
        with db_connect() as conn:
            project = conn.execute(
                "SELECT id FROM projects WHERE id=? AND moderation_status='approved'",
                (project_id,)
            ).fetchone()
            if not project:
                return json_error("Опубликованный проект не найден", 404)
            if action == "add":
                conn.execute("""
                    INSERT OR IGNORE INTO project_watchlist(user_tg_id,project_id,created_at)
                    VALUES(?,?,?)
                """, (user_id, project_id, now_iso()))
                # Сохраняем baseline при добавлении, чтобы не присылать старые изменения.
                row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
                state_json = json.dumps(project_watch_state(row), ensure_ascii=False, sort_keys=True)
                conn.execute("""
                    INSERT OR IGNORE INTO project_watch_snapshots(project_id,snapshot_json,updated_at)
                    VALUES(?,?,?)
                """, (project_id, state_json, now_iso()))
            else:
                conn.execute(
                    "DELETE FROM project_watchlist WHERE user_tg_id=? AND project_id=?",
                    (user_id, project_id)
                )
        return web.json_response({"status": "ok", "action": action, "project_id": project_id})

    with db_connect() as conn:
        scan_project_changes(conn)
        rows = conn.execute("""
            SELECT p.id,p.title,p.link,p.category,p.description,p.website,p.source_url,
                   p.topic,p.community_type,p.is_seed,p.ownership_status,p.moderation_status,
                   p.is_official,p.telegram_verified,p.linkcheck_badge_manual,
                   p.created_at,w.created_at AS saved_at
            FROM project_watchlist w JOIN projects p ON p.id=w.project_id
            WHERE w.user_tg_id=? AND p.moderation_status='approved'
            ORDER BY w.created_at DESC
        """, (user_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item.update(linkcheck_badge_payload(row))
        item["is_official"] = bool(item.get("is_official"))
        item["telegram_verified"] = bool(item.get("telegram_verified"))
        result.append(item)
    return web.json_response({"items": result, "count": len(result)})


async def api_notifications(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Откройте Mini App через Telegram.", 401)
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 100))
    except (TypeError, ValueError):
        limit = 50
    with db_connect() as conn:
        scan_project_changes(conn)
        unread_count = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE recipient_tg_id=? AND read_at IS NULL",
            (user["id"],)
        ).fetchone()[0]
        rows = conn.execute("""
            SELECT n.id,n.project_id,n.event_type,n.title,n.message,n.created_at,n.read_at,
                   p.title AS project_title,p.link AS project_link
            FROM notifications n LEFT JOIN projects p ON p.id=n.project_id
            WHERE n.recipient_tg_id=? ORDER BY n.id DESC LIMIT ?
        """, (user["id"], limit)).fetchall()
    return web.json_response({
        "notifications": [dict(row) for row in rows],
        "unread_count": int(unread_count)
    })


async def api_notifications_read(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Откройте Mini App через Telegram.", 401)
    try:
        data = await read_json(request)
        raw_ids = data.get("ids")
        mark_all = bool(data.get("all", False))
        ids = []
        if raw_ids is not None:
            if not isinstance(raw_ids, list) or len(raw_ids) > 100:
                return json_error("Некорректный список уведомлений")
            ids = [int(value) for value in raw_ids if str(value).isdigit()]
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")
    with db_connect() as conn:
        if mark_all:
            cursor = conn.execute(
                "UPDATE notifications SET read_at=? WHERE recipient_tg_id=? AND read_at IS NULL",
                (now_iso(), user["id"])
            )
        elif ids:
            placeholders = ",".join("?" for _ in ids)
            cursor = conn.execute(
                f"UPDATE notifications SET read_at=? WHERE recipient_tg_id=? AND id IN ({placeholders}) AND read_at IS NULL",
                [now_iso(), user["id"], *ids]
            )
        else:
            return json_error("Не выбраны уведомления")
    return web.json_response({"status": "ok", "updated": int(cursor.rowcount)})


async def api_project_view(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Откройте Mini App через Telegram.", 401)
    try:
        data = await read_json(request)
        project_id = int(data.get("project_id", 0))
        if project_id <= 0:
            return json_error("Некорректный ID проекта")
    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error("Некорректный запрос")
    today = datetime.now(timezone.utc).date().isoformat()
    with db_connect() as conn:
        project = conn.execute(
            "SELECT id,owner_tg_id FROM projects WHERE id=? AND moderation_status='approved'",
            (project_id,)
        ).fetchone()
        if not project:
            return json_error("Проект не найден", 404)
        recorded = False
        if int(project["owner_tg_id"] or 0) != int(user["id"]):
            cursor = conn.execute("""
                INSERT OR IGNORE INTO project_views(project_id,viewer_tg_id,viewed_at,view_day)
                VALUES(?,?,?,?)
            """, (project_id, user["id"], now_iso(), today))
            recorded = cursor.rowcount > 0
    return web.json_response({"status": "ok", "recorded": recorded})


async def api_project_changes(request):
    try:
        project_id = int(request.query.get("project_id", "0"))
        limit = max(1, min(int(request.query.get("limit", "20")), 50))
    except (TypeError, ValueError):
        return json_error("Некорректные параметры")
    if project_id <= 0:
        return json_error("Не указан проект")
    with db_connect() as conn:
        rows = conn.execute("""
            SELECT h.changed_fields,h.created_at
            FROM project_change_history h
            JOIN projects p ON p.id=h.project_id
            WHERE h.project_id=? AND p.moderation_status='approved'
            ORDER BY h.id DESC LIMIT ?
        """, (project_id, limit)).fetchall()
    return web.json_response([dict(row) for row in rows])


async def api_owner_analytics(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку. Откройте Mini App через Telegram.", 401)
    user_id = int(user["id"])
    since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    with db_connect() as conn:
        projects = conn.execute("""
            SELECT p.id,p.title,p.link,p.moderation_status,p.ownership_status,p.is_official,
                   COALESCE(v.total_views,0) AS total_views,
                   COALESCE(v.views_7d,0) AS views_7d,
                   COALESCE(r.report_count,0) AS report_count
            FROM projects p
            LEFT JOIN (
                SELECT project_id,COUNT(*) AS total_views,
                       SUM(CASE WHEN view_day>=? THEN 1 ELSE 0 END) AS views_7d
                FROM project_views GROUP BY project_id
            ) v ON v.project_id=p.id
            LEFT JOIN (
                SELECT project_id,COUNT(*) AS report_count FROM reports GROUP BY project_id
            ) r ON r.project_id=p.id
            WHERE p.owner_tg_id=? ORDER BY p.created_at DESC,p.id DESC
        """, (since, user_id)).fetchall()
        if not projects:
            # Keep response shape stable for users who have not added projects yet.
            return web.json_response({
                "projects_count": 0, "total_views": 0, "views_7d": 0,
                "reports": 0, "pending_reports": 0, "ownership_requests": 0,
                "official_requests": 0, "daily": [], "projects": []
            }, headers={"Cache-Control": "no-store"})
        project_ids = [int(row["id"]) for row in projects]
        placeholders = ",".join("?" for _ in project_ids)
        reports = conn.execute(
            f"SELECT COUNT(*) FROM reports WHERE project_id IN ({placeholders})",
            project_ids
        ).fetchone()[0]
        pending_reports = conn.execute(
            f"SELECT COUNT(*) FROM reports WHERE status='pending' AND project_id IN ({placeholders})",
            project_ids
        ).fetchone()[0]
        ownership_requests = conn.execute(
            f"SELECT COUNT(*) FROM ownership_challenges WHERE status='pending' AND project_id IN ({placeholders})",
            project_ids
        ).fetchone()[0]
        official_requests = conn.execute(
            f"SELECT COUNT(*) FROM official_verification_requests WHERE status='pending' AND project_id IN ({placeholders})",
            project_ids
        ).fetchone()[0]
        daily_rows = conn.execute(f"""
            SELECT substr(viewed_at,1,10) AS day,COUNT(*) AS views
            FROM project_views WHERE project_id IN ({placeholders})
              AND substr(viewed_at,1,10)>=?
            GROUP BY substr(viewed_at,1,10) ORDER BY day ASC
        """, [*project_ids, since]).fetchall()
    daily_map = {row["day"]: int(row["views"]) for row in daily_rows}
    daily = []
    for offset in range(7):
        day = (datetime.now(timezone.utc).date() - timedelta(days=6-offset)).isoformat()
        daily.append({"day": day, "views": daily_map.get(day, 0)})
    project_data = [dict(row) for row in projects]
    return web.json_response({
        "projects_count": len(project_data),
        "total_views": sum(int(row["total_views"] or 0) for row in project_data),
        "views_7d": sum(int(row["views_7d"] or 0) for row in project_data),
        "reports": int(reports), "pending_reports": int(pending_reports),
        "ownership_requests": int(ownership_requests),
        "official_requests": int(official_requests),
        "daily": daily, "projects": project_data
    }, headers={"Cache-Control": "no-store"})


async def watchlist_monitor_loop():
    """Периодически фиксирует изменения проектов для владельцев и подписчиков."""
    try:
        interval = int(os.getenv("WATCHLIST_SCAN_INTERVAL", "60"))
    except (TypeError, ValueError):
        interval = 60
    interval = max(30, min(interval, 900))
    while True:
        try:
            with db_connect() as conn:
                scan_project_changes(conn)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка фоновой проверки изменений LINKCHECK")
        await asyncio.sleep(interval)


# ============================================================
# LINKCHECK PLUS — TELEGRAM STARS + TELEGRAM BUSINESS
# ============================================================

PLUS_DEFAULT_PLANS = {
    "1m": {"title": "1 месяц", "days": 30, "stars": 200, "base_stars": 200, "discount_percent": 0},
    "6m": {"title": "6 месяцев", "days": 180, "stars": 830, "base_stars": 1000, "discount_percent": 17},
    "12m": {"title": "1 год", "days": 365, "stars": 1350, "base_stars": 1800, "discount_percent": 25},
}
PLUS_MESSAGE_RETENTION_DAYS = 30
PLUS_TEST_DAYS = 3

def plus_plans():
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM plus_plan_settings ORDER BY CASE plan_code WHEN '1m' THEN 1 WHEN '6m' THEN 2 ELSE 3 END").fetchall()
    plans = {}
    for row in rows:
        item = dict(row)
        base = max(1, int(item["base_stars"]))
        discount = max(0, min(90, int(item["discount_percent"])))
        item["days"] = max(1, min(3650, int(item["days"])))
        item["base_stars"] = base
        item["discount_percent"] = discount
        item["stars"] = max(1, int(round(base * (100 - discount) / 100)))
        plans[item["plan_code"]] = item
    return plans or dict(PLUS_DEFAULT_PLANS)

def plus_subscription_for(tg_id: int):
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM plus_subscriptions WHERE tg_id=?", (tg_id,)).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        active = expires > datetime.now(timezone.utc)
    except (TypeError, ValueError):
        active = False
    result = dict(row)
    result["active"] = active
    return result

def plus_connection_count(tg_id: int) -> int:
    with db_connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM plus_business_connections WHERE owner_tg_id=? AND is_enabled=1", (tg_id,)).fetchone()
    return int(row["n"] if row else 0)

async def api_plus_status(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку", 401)
    upsert_user(user["id"], user.get("username"))
    sub = plus_subscription_for(user["id"])
    with db_connect() as conn:
        payments = conn.execute("SELECT plan_code, amount_stars, paid_at, expires_at FROM plus_payments WHERE tg_id=? ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()
        connection = conn.execute("SELECT is_enabled, connected_at, updated_at FROM plus_business_connections WHERE owner_tg_id=? ORDER BY is_enabled DESC, updated_at DESC LIMIT 1", (user["id"],)).fetchone()
        trial = conn.execute("SELECT expires_at FROM plus_trial_claims WHERE tg_id=?", (user["id"],)).fetchone()
    plans = plus_plans()
    return web.json_response({
        "payments_enabled": PLUS_PAYMENTS_ENABLED,
        "is_admin": user["id"] in ADMIN_IDS,
        "trial_available": trial is None,
        "trial_days": PLUS_TEST_DAYS,
        "plans": {key: {"title": value["title"], "days": value["days"], "stars": value["stars"], "base_stars": value["base_stars"], "discount_percent": value["discount_percent"]} for key, value in plans.items()},
        "subscription": ({"active": bool(sub["active"]), "plan": sub.get("plan_code"), "started_at": sub.get("started_at"), "expires_at": sub.get("expires_at")} if sub else None),
        "business_connection": ({"connected": bool(connection["is_enabled"]), "connected_at": connection["connected_at"]} if connection else {"connected": False}),
        "payments": [dict(row) for row in payments],
        "message_retention_days": PLUS_MESSAGE_RETENTION_DAYS
    }, headers={"Cache-Control": "no-store"})

async def api_plus_invoice(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку", 401)
    try:
        data = await read_json(request)
    except web.HTTPException:
        return json_error("Некорректный запрос")
    if not PLUS_PAYMENTS_ENABLED:
        return json_error("Оплата LINKCHECK PLUS временно отключена администратором.", 503)
    plan_code = str(data.get("plan", ""))
    plan = plus_plans().get(plan_code)
    if not plan:
        return json_error("Неизвестный тариф")
    payload = f"linkcheck_plus:{user['id']}:{plan_code}:{secrets.token_urlsafe(12)}"
    with db_connect() as conn:
        conn.execute("INSERT INTO plus_invoice_intents(payload,tg_id,plan_code,amount_stars,duration_days,created_at,status) VALUES(?,?,?,?,?,?,'pending')", (payload, user["id"], plan_code, plan["stars"], plan["days"], now_iso()))
    try:
        invoice_url = await bot.create_invoice_link(
            title=f"LINKCHECK PLUS — {plan['title']}",
            description=f"Доступ к LINKCHECK PLUS на {plan['days']} дней. Мониторинг редактирования и удаления сообщений Telegram Business в пределах данных, доставленных боту.",
            payload=payload, provider_token="", currency="XTR",
            prices=[LabeledPrice(label=f"LINKCHECK PLUS · {plan['title']}", amount=plan["stars"])],
        )
    except Exception:
        logging.exception("Не удалось создать Stars invoice для LINKCHECK PLUS")
        with db_connect() as conn:
            conn.execute("UPDATE plus_invoice_intents SET status='failed' WHERE payload=?", (payload,))
        return json_error("Не удалось создать счёт Telegram Stars. Попробуйте позже.", 502)
    return web.json_response({"invoice_url": invoice_url, "plan": plan_code, "stars": plan["stars"]})

async def api_plus_trial(request):
    user = authenticated_user(request)
    if not user:
        return json_error("Telegram-авторизация не прошла проверку", 401)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=PLUS_TEST_DAYS)
    existing_sub = plus_subscription_for(user["id"])
    if existing_sub and existing_sub["active"]:
        return json_error("У вас уже есть активная подписка LINKCHECK PLUS.", 409)
    with db_connect() as conn:
        try:
            conn.execute("INSERT INTO plus_trial_claims(tg_id,claimed_at,expires_at) VALUES(?,?,?)", (user["id"], now.isoformat(), expires.isoformat()))
        except sqlite3.IntegrityError:
            return json_error("Тестовый период уже использован на этом аккаунте.", 409)
        current = conn.execute("SELECT expires_at FROM plus_subscriptions WHERE tg_id=?", (user["id"],)).fetchone()
        try:
            current_expiry = datetime.fromisoformat(current["expires_at"]) if current else now
        except (TypeError, ValueError):
            current_expiry = now
        expiry = max(now, current_expiry) + timedelta(days=PLUS_TEST_DAYS)
        conn.execute("INSERT INTO plus_subscriptions(tg_id,plan_code,started_at,expires_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET plan_code='trial',expires_at=excluded.expires_at,updated_at=excluded.updated_at", (user["id"], "trial", now.isoformat(), expiry.isoformat(), now.isoformat()))
    try:
        await bot.send_message(user["id"], f"<b>LINKCHECK PLUS — тестовый доступ активирован</b>\nСрок: {PLUS_TEST_DAYS} дня\nДействует до: {expiry.strftime('%d.%m.%Y %H:%M UTC')}")
    except Exception:
        pass
    return web.json_response({"ok": True, "expires_at": expiry.isoformat(), "days": PLUS_TEST_DAYS})

async def api_admin_plus_settings(request):
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)
    return web.json_response({"plans": plus_plans(), "payments_enabled": PLUS_PAYMENTS_ENABLED})

async def api_admin_plus_update(request):
    admin = admin_user(request)
    if not admin:
        return json_error("Forbidden", 403)
    try:
        data = await read_json(request)
    except web.HTTPException:
        return json_error("Некорректный запрос")
    code = str(data.get("plan", ""))
    if code not in {"1m", "6m", "12m"}:
        return json_error("Неизвестный тариф")
    try:
        days = int(data.get("days")); base = int(data.get("base_stars")); discount = int(data.get("discount_percent"))
    except (TypeError, ValueError):
        return json_error("Срок, цена и скидка должны быть числами")
    if not 1 <= days <= 3650 or not 1 <= base <= 1000000 or not 0 <= discount <= 90:
        return json_error("Допустимые значения: срок 1–3650 дней, цена 1–1 000 000 Stars, скидка 0–90%")
    with db_connect() as conn:
        conn.execute("UPDATE plus_plan_settings SET days=?,base_stars=?,discount_percent=?,updated_at=? WHERE plan_code=?", (days, base, discount, now_iso(), code))
    return web.json_response({"ok": True, "plans": plus_plans()})


async def handle_plus_pre_checkout(query: PreCheckoutQuery):
    valid = False
    with db_connect() as conn:
        intent = conn.execute(
            "SELECT * FROM plus_invoice_intents WHERE payload=? AND tg_id=? AND status='pending'",
            (query.invoice_payload, query.from_user.id)
        ).fetchone()
        if intent and query.currency == "XTR" and query.total_amount == intent["amount_stars"]:
            # Atomically reserve this invoice payload for a single checkout.
            changed = conn.execute(
                "UPDATE plus_invoice_intents SET status='checkout' "
                "WHERE payload=? AND status='pending'",
                (query.invoice_payload,)
            ).rowcount
            valid = changed == 1
    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Счёт недействителен или уже использован. Создайте новый счёт в LINKCHECK.")


async def handle_plus_successful_payment(message: Message):
    payment = message.successful_payment
    if not payment or payment.currency != "XTR" or not message.from_user:
        return
    with db_connect() as conn:
        already_recorded = conn.execute(
            "SELECT 1 FROM plus_payments WHERE telegram_payment_charge_id=?",
            (payment.telegram_payment_charge_id,)
        ).fetchone()
        if already_recorded:
            return
        intent = conn.execute(
            "SELECT * FROM plus_invoice_intents WHERE payload=? AND tg_id=? AND status='checkout'",
            (payment.invoice_payload, message.from_user.id)
        ).fetchone()
        if not intent or int(payment.total_amount) != int(intent["amount_stars"]):
            logging.warning("Получен неподтверждённый LINKCHECK PLUS payment update от %s", message.from_user.id)
            return
        plan = plus_plans().get(intent["plan_code"])
        if not plan:
            return
        now = datetime.now(timezone.utc)
        current = conn.execute(
            "SELECT expires_at FROM plus_subscriptions WHERE tg_id=?", (message.from_user.id,)
        ).fetchone()
        try:
            current_expiry = datetime.fromisoformat(current["expires_at"]) if current else now
        except (TypeError, ValueError):
            current_expiry = now
        base = max(now, current_expiry)
        duration_days = int(intent["duration_days"] or plan["days"])
        expires = base + timedelta(days=duration_days)
        conn.execute(
            "INSERT INTO plus_payments(tg_id,plan_code,amount_stars,currency,telegram_payment_charge_id,provider_payment_charge_id,paid_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (message.from_user.id, intent["plan_code"], payment.total_amount, payment.currency,
             payment.telegram_payment_charge_id, payment.provider_payment_charge_id, now.isoformat(), expires.isoformat())
        )
        conn.execute(
            "INSERT INTO plus_subscriptions(tg_id,plan_code,started_at,expires_at,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(tg_id) DO UPDATE SET "
            "plan_code=excluded.plan_code, expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (message.from_user.id, intent["plan_code"], now.isoformat(), expires.isoformat(), now.isoformat())
        )
        conn.execute("UPDATE plus_invoice_intents SET status='paid' WHERE payload=?", (payment.invoice_payload,))
    await message.answer(
        "<b>LINKCHECK PLUS активирован</b>\n\n"
        f"Тариф: {html.escape(plan['title'])}\n"
        f"Оплачено: {int(payment.total_amount)} ⭐\n"
        f"Действует до: {expires.strftime('%d.%m.%Y %H:%M UTC')}\n\n"
        "Подключите LINKCHECK в Telegram Business, чтобы начать мониторинг."
    )


def business_media(message: Message):
    media_fields = (
        ("photo", "photo"), ("video", "video"), ("document", "document"),
        ("animation", "animation"), ("audio", "audio"), ("voice", "voice"),
        ("video_note", "video_note"), ("sticker", "sticker")
    )
    for field, kind in media_fields:
        value = getattr(message, field, None)
        if value:
            if field == "photo":
                return kind, value[-1].file_id
            return kind, value.file_id
    return None, None


def business_message_content(message: Message) -> str:
    """Keep a readable snapshot of non-text Telegram message payloads where available."""
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    if message.location:
        return f"Местоположение: {message.location.latitude}, {message.location.longitude}"
    if message.venue:
        venue = message.venue
        return f"Место: {venue.title}\nАдрес: {venue.address}\nКоординаты: {venue.location.latitude}, {venue.location.longitude}"
    if message.contact:
        contact = message.contact
        name = " ".join(x for x in [contact.first_name, contact.last_name] if x)
        return f"Контакт: {name}\nНомер телефона не сохраняется"
    if message.poll:
        poll = message.poll
        options = "\n".join(f"• {option.text}" for option in poll.options)
        return f"Опрос: {poll.question}\n{options}"
    if message.dice:
        return f"Эмодзи-кубик: {message.dice.emoji}, результат: {message.dice.value}"
    if message.game:
        return f"Игра: {message.game.title}\n{message.game.description or ''}".strip()
    if message.invoice:
        invoice = message.invoice
        return f"Счёт: {invoice.title}\n{invoice.description}\nСумма: {invoice.total_amount} {invoice.currency}"
    if message.web_app_data:
        return f"Данные Web App: {message.web_app_data.data[:1500]}"
    return ""


def business_sender_label(message: Message) -> str:
    sender = message.from_user
    if sender:
        name = " ".join(x for x in [sender.first_name, sender.last_name] if x)
        return (name or ("@" + sender.username if sender.username else str(sender.id)))[:160]
    if message.sender_chat:
        return (message.sender_chat.title or message.sender_chat.username or str(message.sender_chat.id))[:160]
    return "Неизвестный отправитель"


async def handle_business_connection(connection):
    owner_id = int(connection.user.id)
    rights = connection.rights.model_dump(exclude_none=True) if connection.rights else {}
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO plus_business_connections(connection_id,owner_tg_id,user_chat_id,is_enabled,rights_json,connected_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(connection_id) DO UPDATE SET "
            "owner_tg_id=excluded.owner_tg_id,user_chat_id=excluded.user_chat_id,is_enabled=excluded.is_enabled,"
            "rights_json=excluded.rights_json,updated_at=excluded.updated_at",
            (connection.id, owner_id, connection.user_chat_id, int(connection.is_enabled),
             json.dumps(rights, ensure_ascii=False), now_iso(), now_iso())
        )
        if not connection.is_enabled:
            conn.execute("DELETE FROM plus_business_messages WHERE connection_id=?", (connection.id,))
    if connection.is_enabled:
        sub = plus_subscription_for(owner_id)
        note = ("Подключение Telegram Business активно. LINKCHECK PLUS готов отслеживать доставленные боту события."
                if sub and sub["active"] else
                "Telegram Business подключён. Для начала мониторинга активируйте LINKCHECK PLUS в Mini App.")
        try:
            await bot.send_message(connection.user_chat_id, note)
        except Exception:
            logging.exception("Не удалось отправить уведомление о Business connection владельцу %s", owner_id)


async def handle_business_message(message: Message):
    connection_id = message.business_connection_id
    if not connection_id:
        return
    with db_connect() as conn:
        connection = conn.execute(
            "SELECT * FROM plus_business_connections WHERE connection_id=? AND is_enabled=1",
            (connection_id,)
        ).fetchone()
    if not connection:
        return
    sub = plus_subscription_for(int(connection["owner_tg_id"]))
    if not sub or not sub["active"]:
        return
    media_type, media_file_id = business_media(message)
    chat_title = message.chat.title or message.chat.username or str(message.chat.id)
    content = business_message_content(message)
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO plus_business_messages(connection_id,owner_tg_id,chat_id,message_id,message_date,chat_title,sender_label,text_content,caption,media_type,media_file_id,is_outgoing,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(connection_id,chat_id,message_id) DO UPDATE SET "
            "message_date=excluded.message_date,chat_title=excluded.chat_title,sender_label=excluded.sender_label,"
            "text_content=excluded.text_content,caption=excluded.caption,media_type=excluded.media_type,"
            "media_file_id=excluded.media_file_id,is_outgoing=excluded.is_outgoing,updated_at=excluded.updated_at",
            (connection_id, connection["owner_tg_id"], message.chat.id, message.message_id,
             message.date.isoformat() if message.date else None, chat_title, business_sender_label(message),
             content, message.caption or "", media_type, media_file_id, int(bool(message.out)), now_iso())
        )
        conn.execute(
            "DELETE FROM plus_business_messages WHERE updated_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=PLUS_MESSAGE_RETENTION_DAYS)).isoformat(),)
        )


async def send_business_snapshot(owner_chat_id: int, record, heading: str):
    chat = html.escape(str(record["chat_title"] or record["chat_id"]))
    sender = html.escape(str(record["sender_label"] or "Неизвестно"))
    body = str(record["text_content"] or record["caption"] or "").strip()
    safe_body = html.escape(body[:3500]) if body else "<i>Текст отсутствует</i>"
    caption = f"<b>{heading}</b>\nЧат: {chat}\nОтправитель: {sender}\n\n{safe_body}"
    media_type, file_id = record["media_type"], record["media_file_id"]
    try:
        if file_id and media_type in {"photo", "video", "document", "animation", "audio", "voice"}:
            method = getattr(bot, f"send_{media_type}")
            await method(owner_chat_id, file_id, caption=caption[:1024])
        elif file_id and media_type == "video_note":
            await bot.send_message(owner_chat_id, caption)
            await bot.send_video_note(owner_chat_id, file_id)
        elif file_id and media_type == "sticker":
            await bot.send_message(owner_chat_id, caption)
            await bot.send_sticker(owner_chat_id, file_id)
        else:
            await bot.send_message(owner_chat_id, caption)
    except Exception:
        logging.exception("Не удалось переслать сохранённое Business-сообщение")
        await bot.send_message(owner_chat_id, caption)


async def handle_edited_business_message(message: Message):
    connection_id = message.business_connection_id
    if not connection_id:
        return
    with db_connect() as conn:
        old = conn.execute(
            "SELECT * FROM plus_business_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
            (connection_id, message.chat.id, message.message_id)
        ).fetchone()
        connection = conn.execute(
            "SELECT * FROM plus_business_connections WHERE connection_id=? AND is_enabled=1",
            (connection_id,)
        ).fetchone()
    if not old or not connection:
        return
    sub = plus_subscription_for(int(connection["owner_tg_id"]))
    if not sub or not sub["active"]:
        return
    await send_business_snapshot(connection["user_chat_id"], old, "Сообщение до редактирования")
    await handle_business_message(message)
    with db_connect() as conn:
        updated = conn.execute(
            "SELECT * FROM plus_business_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
            (connection_id, message.chat.id, message.message_id)
        ).fetchone()
    if updated:
        await send_business_snapshot(connection["user_chat_id"], updated, "Новая версия сообщения")


async def handle_deleted_business_messages(event):
    with db_connect() as conn:
        connection = conn.execute(
            "SELECT * FROM plus_business_connections WHERE connection_id=? AND is_enabled=1",
            (event.business_connection_id,)
        ).fetchone()
        if not connection:
            return
        sub = plus_subscription_for(int(connection["owner_tg_id"]))
        if not sub or not sub["active"]:
            return
        rows = []
        for message_id in event.message_ids:
            row = conn.execute(
                "SELECT * FROM plus_business_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
                (event.business_connection_id, event.chat.id, message_id)
            ).fetchone()
            if row:
                rows.append(dict(row))
                conn.execute(
                    "DELETE FROM plus_business_messages WHERE connection_id=? AND chat_id=? AND message_id=?",
                    (event.business_connection_id, event.chat.id, message_id)
                )
    if not rows:
        return
    for record in rows:
        await send_business_snapshot(connection["user_chat_id"], record, "Сообщение удалено")


async def plus_retention_cleanup_loop():
    while True:
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=PLUS_MESSAGE_RETENTION_DAYS)).isoformat()
            with db_connect() as conn:
                deleted = conn.execute(
                    "DELETE FROM plus_business_messages WHERE updated_at < ?", (cutoff,)
                ).rowcount
            if deleted:
                logging.info("LINKCHECK PLUS retention cleanup: deleted %s expired message records", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("LINKCHECK PLUS retention cleanup failed")
        await asyncio.sleep(3600)


# Register Telegram update handlers. Polling below explicitly requests their update types.
dp.pre_checkout_query.register(handle_plus_pre_checkout)
dp.message.register(handle_plus_successful_payment, F.successful_payment)
dp.business_connection.register(handle_business_connection)
dp.business_message.register(handle_business_message)
dp.edited_business_message.register(handle_edited_business_message)
dp.deleted_business_messages.register(handle_deleted_business_messages)

# ============================================================
# WEB SERVER
# ============================================================

async def handle_index(request):
    return web.FileResponse(os.path.join(APP_DIR, "miniapp.html"), headers={"Cache-Control": "no-store"})


async def handle_privacy(request):
    return web.FileResponse(os.path.join(APP_DIR, "privacy.html"), headers={"Cache-Control": "public, max-age=300"})


# Sliding-window limits are a first defensive layer for this single-process
# aiohttp service. Persistent endpoint quotas (reports, DNS and official requests)
# remain enforced by SQLite as well.
API_RATE_LIMITS = {
    ("POST", "/api/check"): (30, 60),
    ("POST", "/api/request_access"): (1, 600),
    ("POST", "/api/add"): (5, 3600),
    ("POST", "/api/edit"): (10, 3600),
    ("POST", "/api/ownership/start"): (10, 3600),
    ("POST", "/api/ownership/check"): (20, 3600),
    ("POST", "/api/ownership/manual_review"): (3, 86400),
    ("POST", "/api/ownership/screenshot"): (5, 3600),
    ("POST", "/api/official/request"): (5, 86400),
    ("POST", "/api/report"): (10, 86400),
    ("POST", "/api/watchlist"): (60, 60),
    ("POST", "/api/notifications/read"): (30, 60),
    ("POST", "/api/project/view"): (120, 60),
    ("POST", "/api/plus/invoice"): (5, 3600),
}
API_RATE_LIMIT_DEFAULT = (120, 60)
API_RATE_LIMIT_HITS = {}
API_RATE_LIMIT_LOCK = asyncio.Lock()
API_RATE_LIMIT_CLEANUP_COUNTER = 0


def request_rate_limit_identity(request):
    """Use Telegram ID when signed initData is present, otherwise a client IP."""
    user = authenticated_user(request)
    if user:
        return "tg:" + str(user["id"])

    remote = str(request.remote or "").strip()
    try:
        remote_ip = ipaddress.ip_address(remote)
    except ValueError:
        return "ip:unknown"

    # Cloudflare Tunnel connects to the local origin. Trust CF-Connecting-IP only
    # when the TCP peer is loopback; ignore arbitrary X-Forwarded-For headers.
    if remote_ip.is_loopback:
        forwarded = str(request.headers.get("CF-Connecting-IP", "")).strip()
        try:
            forwarded_ip = ipaddress.ip_address(forwarded)
            return "ip:" + str(forwarded_ip)
        except ValueError:
            pass

    return "ip:" + str(remote_ip)


async def apply_api_rate_limit(request):
    if not request.path.startswith("/api/"):
        return None

    limit, window = API_RATE_LIMITS.get(
        (request.method, request.path),
        API_RATE_LIMIT_DEFAULT
    )
    identity = request_rate_limit_identity(request)
    bucket_key = (request.method, request.path, identity)
    now = time.monotonic()
    cutoff = now - window

    global API_RATE_LIMIT_CLEANUP_COUNTER
    async with API_RATE_LIMIT_LOCK:
        hits = API_RATE_LIMIT_HITS.setdefault(bucket_key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= limit:
            retry_after = max(1, math.ceil(window - (now - hits[0])))
            response = json_error(
                "Слишком много запросов. Повторите попытку позже.",
                429
            )
            response.headers["Retry-After"] = str(retry_after)
            return response

        hits.append(now)
        API_RATE_LIMIT_CLEANUP_COUNTER += 1

        if API_RATE_LIMIT_CLEANUP_COUNTER >= 256:
            API_RATE_LIMIT_CLEANUP_COUNTER = 0
            stale_before = now - 86_400
            for key, bucket in list(API_RATE_LIMIT_HITS.items()):
                while bucket and bucket[0] <= stale_before:
                    bucket.popleft()
                if not bucket:
                    API_RATE_LIMIT_HITS.pop(key, None)

            # Bound memory if many short-lived client IPs are seen.
            if len(API_RATE_LIMIT_HITS) > 10_000:
                oldest = sorted(
                    API_RATE_LIMIT_HITS.items(),
                    key=lambda item: item[1][-1] if item[1] else 0
                )
                for key, _bucket in oldest[:len(API_RATE_LIMIT_HITS) - 10_000]:
                    API_RATE_LIMIT_HITS.pop(key, None)

    return None


@web.middleware
async def security_middleware(request, handler):
    max_request_bytes = 9 * 1024 * 1024 if request.path == "/api/ownership/screenshot" else 32_768
    if request.content_length is not None and request.content_length > max_request_bytes:
        response = json_error("Слишком большой запрос", 413)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    limited_response = await apply_api_rate_limit(request)
    if limited_response is not None:
        limited_response.headers["X-Content-Type-Options"] = "nosniff"
        limited_response.headers["X-Frame-Options"] = "SAMEORIGIN"
        limited_response.headers["Referrer-Policy"] = "no-referrer"
        return limited_response

    try:
        # Content-Length may be absent on chunked requests. For ordinary API
        # payloads, read() caches the body so handlers can still parse it safely.
        # The screenshot route is parsed incrementally and has its own strict cap.
        if (
            request.content_length is None
            and request.can_read_body
            and request.path != "/api/ownership/screenshot"
        ):
            body = await request.read()
            if len(body) > max_request_bytes:
                return json_error("Слишком большой запрос", 413)

        response = await handler(request)
    except web.HTTPException as exc:
        return json_error(exc.text or "Некорректный запрос", exc.status)
    except Exception:
        logging.exception("Ошибка API: %s %s", request.method, request.path)
        return json_error("Внутренняя ошибка сервера", 500)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response



async def start_web_server():
    app = web.Application(middlewares=[security_middleware], client_max_size=9 * 1024 * 1024)
    app.router.add_get("/", handle_index)
    app.router.add_get("/privacy", handle_privacy)
    app.router.add_get("/api/founder", api_founder)
    app.router.add_get("/api/founder/avatar", api_founder_avatar)
    app.router.add_get("/api/founder/gift", api_founder_gift)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/plus/status", api_plus_status)
    app.router.add_post("/api/plus/invoice", api_plus_invoice)
    app.router.add_post("/api/plus/trial", api_plus_trial)
    app.router.add_get("/api/admin/plus/settings", api_admin_plus_settings)
    app.router.add_post("/api/admin/plus/update", api_admin_plus_update)
    app.router.add_post("/api/request_access", api_request_access)
    app.router.add_post("/api/check", api_check)
    app.router.add_get("/api/check/history", api_check_history)
    app.router.add_get("/api/watchlist", api_watchlist)
    app.router.add_post("/api/watchlist", api_watchlist)
    app.router.add_get("/api/notifications", api_notifications)
    app.router.add_post("/api/notifications/read", api_notifications_read)
    app.router.add_post("/api/project/view", api_project_view)
    app.router.add_get("/api/project/changes", api_project_changes)
    app.router.add_get("/api/owner/analytics", api_owner_analytics)
    app.router.add_post("/api/add", api_add_project)
    app.router.add_post("/api/edit", api_edit_project)
    app.router.add_post("/api/official/request", api_official_verification_request)
    app.router.add_post("/api/ownership/start", api_ownership_start)
    app.router.add_post("/api/ownership/manual_review", api_ownership_manual_review)
    app.router.add_post("/api/ownership/screenshot", api_ownership_screenshot_upload)
    app.router.add_post("/api/ownership/check", api_ownership_check)
    app.router.add_post("/api/report", api_report_project)
    app.router.add_get("/api/catalog", api_catalog)
    app.router.add_get("/api/my_projects", api_my_projects)
    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_post("/api/admin/toggle_trust", api_admin_toggle_trust)
    app.router.add_get("/api/admin/requests", api_admin_requests)
    app.router.add_get("/api/admin/official", api_admin_official_requests)
    app.router.add_post("/api/admin/official_action", api_admin_official_action)
    app.router.add_get("/api/admin/ownership", api_admin_ownership)
    app.router.add_get("/api/admin/ownership/{challenge_id}/screenshot", api_admin_ownership_screenshot)
    app.router.add_post("/api/admin/ownership_action", api_admin_ownership_action)
    app.router.add_get("/api/admin/moderation_log", api_admin_moderation_log)
    app.router.add_get("/api/admin/reports", api_admin_reports)
    app.router.add_post("/api/admin/report_action", api_admin_report_action)
    app.router.add_get("/api/admin/catalog", api_admin_catalog)
    app.router.add_post("/api/admin/badge_action", api_admin_badge_action)
    app.router.add_post("/api/admin/action", api_admin_action)
    app.router.add_get("/favicon.ico", lambda request: web.Response(status=204))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Веб-сервер запущен на порту %s", PORT)
    return runner

# ============================================================
# TELEGRAM CHECKER
# ============================================================

async def start_telegram_checker():
    global tg_client, tg_ready, tg_start_error
    if not TG_API_ID or not TG_API_HASH:
        tg_start_error = "TG_API_ID или TG_API_HASH не настроены"
        logging.warning(
            "%s. Резервный MTProto отключён; проверка открытых username через публичные страницы t.me остаётся доступной.",
            tg_start_error
        )
        return
    if TelegramClient is None:
        tg_start_error = "Telethon не установлен. Выполните pip install telethon"
        logging.warning("%s. Резервный MTProto отключён; публичная проверка через t.me остаётся доступной.", tg_start_error)
        return
    try:
        tg_client = TelegramClient(
            TG_SESSION,
            TG_API_ID,
            TG_API_HASH,
            flood_sleep_threshold=0,
            request_retries=2,
            connection_retries=3,
            retry_delay=1,
            auto_reconnect=True
        )
        await tg_client.start()
        tg_ready = True
        logging.info("Резервная проверка через MTProto подключена; основной способ проверки публичных username — страница t.me.")
    except Exception as exc:
        tg_start_error = f"{type(exc).__name__}: {exc}"
        tg_ready = False
        logging.exception(
            "Не удалось запустить Telegram-проверку. Отправка проектов на модерацию продолжит работать."
        )

# ============================================================
# MAIN
# ============================================================

async def main():
    init_db()
    await start_telegram_checker()
    if tg_ready:
        try:
            await get_founder_profile(force=True)
        except Exception as exc:
            logging.warning("Initial founder profile sync failed; cached profile will remain available: %s", exc)
    await seed_public_catalog()
    runner = await start_web_server()
    watchlist_task = asyncio.create_task(watchlist_monitor_loop(), name="linkcheck-watchlist-monitor")
    plus_cleanup_task = asyncio.create_task(plus_retention_cleanup_loop(), name="linkcheck-plus-retention-cleanup")
    logging.info("Автоматическая массовая MTProto-проверка стартовых проектов отключена.")
    try:
        logging.info("Запуск LINKCHECK...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watchlist_task.cancel()
        plus_cleanup_task.cancel()
        for task in (watchlist_task, plus_cleanup_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await runner.cleanup()
        if tg_client is not None:
            await tg_client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("LINKCHECK остановлен")
