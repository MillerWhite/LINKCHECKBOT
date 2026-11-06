# LINKCHECK — backend с проверкой публичных Telegram-ресурсов

import asyncio
import csv
import gzip
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import re
import sqlite3
import time

from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_PATH = os.getenv("DB_PATH", "linkcheck.db")
PORT = int(os.getenv("PORT", "8080"))
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "linkcheck_telegram").strip()

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

TELEGRAM_CHECK_CACHE_TTL = 300
TELEGRAM_NOT_FOUND_CACHE_TTL = 60
TELEGRAM_UNAVAILABLE_CACHE_TTL = 15

TELEGRAM_CHECK_MIN_INTERVAL = 0.8

# Публичная HTTP-проверка Telegram.
TELEGRAM_PUBLIC_PAGE_TIMEOUT = 8
TELEGRAM_PUBLIC_PAGE_MIN_INTERVAL = 0.5

# Общая блокировка MTProto после FloodWait.
telegram_flood_wait_until = 0

telegram_last_check_at = 0

telegram_check_cache = {}

telegram_check_lock = asyncio.Lock()

# Отдельная блокировка публичной HTTP-проверки.
telegram_public_page_lock = asyncio.Lock()

telegram_public_page_last_check_at = 0


# ============================================================
# НАСТРОЙКИ КЭША ПРОФИЛЯ СОЗДАТЕЛЯ
# ============================================================

FOUNDER_PROFILE_CACHE_TTL = 300
FOUNDER_AVATAR_CACHE_TTL = 300
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
founder_profile_cache = None
founder_profile_cache_time = 0

founder_profile_lock = asyncio.Lock()

founder_gift_cache = {}
founder_gift_lock = asyncio.Lock()


# ============================================================
# СТАРТОВЫЕ РЕСУРСЫ LINKCHECK
# ============================================================

SEED_PROJECTS = [
    {
        "title": "Telegram",
        "link": "@telegram",
        "category": "channel",
        "description": "Официальный канал Telegram.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "Telegram Tips",
        "link": "@TelegramTips",
        "category": "channel",
        "description": "Официальные советы и новости о возможностях Telegram.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "BotNews",
        "link": "@BotNews",
        "category": "channel",
        "description": "Новости платформы Telegram для разработчиков ботов.",
        "is_official": 1,
        "source_url": "https://core.telegram.org/bots"
    },
    {
        "title": "Telegram Premium",
        "link": "@premium",
        "category": "channel",
        "description": "Информация о возможностях Telegram Premium.",
        "is_official": 1,
        "source_url": "https://telegram.org/faq_premium"
    },
    {
        "title": "BotFather",
        "link": "@BotFather",
        "category": "bot",
        "description": "Официальный бот Telegram для создания и управления ботами.",
        "is_official": 1,
        "source_url": "https://core.telegram.org/bots"
    },
    {
        "title": "Premium Bot",
        "link": "@PremiumBot",
        "category": "bot",
        "description": "Официальный бот для оформления Telegram Premium.",
        "is_official": 1,
        "source_url": "https://telegram.org/faq_premium"
    },
    {
        "title": "Stickers",
        "link": "@Stickers",
        "category": "bot",
        "description": "Официальный бот Telegram для создания и управления стикерами.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "GIF",
        "link": "@gif",
        "category": "bot",
        "description": "Встроенный бот Telegram для поиска GIF.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "Vote",
        "link": "@vote",
        "category": "bot",
        "description": "Бот Telegram для создания опросов.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "QuizBot",
        "link": "@QuizBot",
        "category": "bot",
        "description": "Бот Telegram для создания викторин.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "DiscussBot",
        "link": "@discussbot",
        "category": "bot",
        "description": "Бот для обсуждений и комментариев в каналах.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "Binance",
        "link": "@BinanceExchange",
        "category": "group",
        "description": "Официальное англоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Announcements",
        "link": "@binance_announcements",
        "category": "channel",
        "description": "Официальный канал объявлений Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Chinese",
        "link": "@BinanceChinese",
        "category": "group",
        "description": "Официальное китайскоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance China Announcements",
        "link": "@binance_cn",
        "category": "channel",
        "description": "Китайскоязычный канал объявлений Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Russian Community",
        "link": "@BinanceRussianSpeaking",
        "category": "group",
        "description": "Официальное сообщество Binance для русскоговорящей аудитории.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Russia",
        "link": "@binance_ru",
        "category": "channel",
        "description": "Русскоязычный канал новостей Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Kazakhstan",
        "link": "@binance_kz",
        "category": "group",
        "description": "Региональное сообщество Binance для Казахстана.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Kyrgyzstan",
        "link": "@BinanceKyrgyz",
        "category": "group",
        "description": "Региональное сообщество Binance для Кыргызстана.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Armenia",
        "link": "@Binance_Armenian",
        "category": "group",
        "description": "Официальное сообщество Binance для Армении.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Balkans",
        "link": "@binancebalkans",
        "category": "group",
        "description": "Официальное сообщество Binance для Балкан.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Bangladesh",
        "link": "@BinanceBangladeshi",
        "category": "group",
        "description": "Региональное сообщество Binance для Бангладеш.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Cameroon",
        "link": "@BinanceCameroun",
        "category": "group",
        "description": "Региональное сообщество Binance для Камеруна.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Germany",
        "link": "@BinanceGerman",
        "category": "group",
        "description": "Немецкоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Australia",
        "link": "@binanceaustralia",
        "category": "group",
        "description": "Австралийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Namibia",
        "link": "@BinanceNamibia",
        "category": "group",
        "description": "Региональное сообщество Binance для Намибии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance New Zealand",
        "link": "@BinanceNewZealand",
        "category": "group",
        "description": "Региональное сообщество Binance для Новой Зеландии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Zambia",
        "link": "@BinanceZambia",
        "category": "group",
        "description": "Региональное сообщество Binance для Замбии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Spain",
        "link": "@BinanceSpanish",
        "category": "group",
        "description": "Испаноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Argentina",
        "link": "@binanceargentinaoficial",
        "category": "group",
        "description": "Региональное сообщество Binance для Аргентины.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Spain Official",
        "link": "@Binance_ESP",
        "category": "group",
        "description": "Испаноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Philippines",
        "link": "@BinanceFilipino",
        "category": "group",
        "description": "Филиппинское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance France",
        "link": "@BinanceFrench",
        "category": "group",
        "description": "Франкоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Greece",
        "link": "@BinanceGreek",
        "category": "group",
        "description": "Грекоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance India",
        "link": "@BinanceIndian",
        "category": "group",
        "description": "Индийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Italy",
        "link": "@BinanceItalian",
        "category": "group",
        "description": "Италоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Kenya",
        "link": "@BinanceKenya",
        "category": "group",
        "description": "Кенийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Nigeria",
        "link": "@BinanceNigeria",
        "category": "group",
        "description": "Нигерийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Poland",
        "link": "@Binance_Polish",
        "category": "group",
        "description": "Польскоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Portugal",
        "link": "@BinancePortuguese",
        "category": "group",
        "description": "Португалоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Portugal Community",
        "link": "@Binance_PT",
        "category": "group",
        "description": "Региональное сообщество Binance для Португалии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Romania",
        "link": "@BinanceRomanian",
        "category": "group",
        "description": "Румыноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Seychelles",
        "link": "@BinanceSeychelles",
        "category": "group",
        "description": "Региональное сообщество Binance для Сейшельских Островов.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance South Africa",
        "link": "@BinanceSouthAfrica",
        "category": "group",
        "description": "Южноафриканское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Sri Lanka",
        "link": "@binancesrilanka",
        "category": "group",
        "description": "Сообщество Binance для Шри-Ланки.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Tanzania",
        "link": "@BinanceTanzania",
        "category": "group",
        "description": "Танзанийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Vietnam",
        "link": "@BinanceVietnamese",
        "category": "group",
        "description": "Вьетнамское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Uganda",
        "link": "@BinanceUganda",
        "category": "group",
        "description": "Угандийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Czech and Slovakia",
        "link": "@BinanceCzechoSlovakia",
        "category": "group",
        "description": "Сообщество Binance для Чехии и Словакии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Mongolian",
        "link": "@BinanceMongolian",
        "category": "group",
        "description": "Монгольское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Ukraine",
        "link": "@BinanceUkrainian",
        "category": "group",
        "description": "Украиноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Hebrew",
        "link": "@BinanceHebrew",
        "category": "group",
        "description": "Ивритоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Arabic",
        "link": "@BinanceArabic",
        "category": "group",
        "description": "Арабоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Pakistan",
        "link": "@binancepakistani",
        "category": "group",
        "description": "Пакистанское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Nepal",
        "link": "@BinanceNepali",
        "category": "group",
        "description": "Непальское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Laos",
        "link": "@BinanceLaotian",
        "category": "group",
        "description": "Лаосское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Myanmar",
        "link": "@BinanceBurmese",
        "category": "group",
        "description": "Бирманское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Cambodia",
        "link": "@BinanceKhmer",
        "category": "group",
        "description": "Сообщество Binance для Камбоджи.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Flemish",
        "link": "@BinanceFlemish",
        "category": "group",
        "description": "Нидерландоязычное сообщество Binance для Фландрии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Academy",
        "link": "@BinanceAcademy",
        "category": "channel",
        "description": "Образовательные материалы Binance Academy.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/academy"
    },
    {
        "title": "Binance Academy Chat",
        "link": "@BinanceAcademyChat",
        "category": "group",
        "description": "Сообщество Binance Academy.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/academy"
    },
    {
        "title": "Binance NFT",
        "link": "@BinanceNFTs",
        "category": "channel",
        "description": "Канал Binance, посвящённый NFT.",
        "is_official": 1,
        "source_url": "https://www.binance.com/"
    },
    {
        "title": "Binance Futures",
        "link": "@BinanceFutures_Official",
        "category": "channel",
        "description": "Обновления Binance Futures.",
        "is_official": 1,
        "source_url": "https://www.binance.com/"
    },
    {
        "title": "Binance API English",
        "link": "@binance_api_english",
        "category": "group",
        "description": "Сообщество пользователей и разработчиков Binance API.",
        "is_official": 1,
        "source_url": "https://www.binance.com/"
    },
    {
        "title": "Trust Wallet",
        "link": "@trustwallet",
        "category": "channel",
        "description": "Telegram-ресурс Trust Wallet, указанный в материалах Binance.",
        "is_official": 1,
        "source_url": "https://trustwallet.com/"
    },
    {
        "title": "TON Blockchain",
        "link": "@tonblockchain",
        "category": "channel",
        "description": "Официальный информационный ресурс TON.",
        "is_official": 1,
        "source_url": "https://ton.org/"
    }
]


# ============================================================
# TELEGRAM BOT
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "👋 Добро пожаловать в <b>LINKCHECK</b>!\n\n"
        "Платформа для проверки и поиска публичных Telegram-ресурсов.\n\n"
        "Нажми кнопку ниже, чтобы открыть приложение."
    )

    if MINIAPP_URL:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть LINKCHECK",
                    web_app=WebAppInfo(url=MINIAPP_URL)
                )
            ]]
        )

        await message.answer(
            text,
            reply_markup=keyboard
        )

    else:
        await message.answer(
            text + "\n\n⚠️ Mini App пока не настроен. Проверь MINIAPP_URL в .env."
        )


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def db_connect():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10
    )

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
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


# ============================================================
# МИГРАЦИЯ СТАРОЙ ТАБЛИЦЫ PROJECTS
# ============================================================

def migrate_projects_owner_nullable(conn):
    """
    Перестраивает projects, если owner_tg_id в старой базе имеет NOT NULL.

    Сохраняет ID и все существующие записи.
    Поддерживает topic и community_type.
    """

    info = conn.execute(
        "PRAGMA table_info(projects)"
    ).fetchall()

    owner_column = next(
        (
            row
            for row in info
            if row["name"] == "owner_tg_id"
        ),
        None
    )

    if owner_column is None or owner_column["notnull"] == 0:
        return

    logging.warning(
        "Обнаружена старая схема projects: owner_tg_id NOT NULL. "
        "Выполняется безопасная миграция."
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
                FOREIGN KEY (owner_tg_id)
                    REFERENCES users(tg_id) ON DELETE CASCADE
            )
        """)

        old_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(projects)"
            ).fetchall()
        }

        defaults = {
            "id": "NULL",
            "owner_tg_id": "NULL",
            "title": "''",
            "link": "''",
            "category": "NULL",
            "description": "NULL",
            "website": "NULL",
            "ownership_status": "'unverified'",
            "moderation_status": "'pending'",
            "created_at": "''",
            "is_official": "0",
            "telegram_verified": "0",
            "source_url": "NULL",
            "is_seed": "0",
            "topic": "NULL",
            "community_type": "NULL"
        }

        target_columns = list(defaults.keys())

        select_expressions = [
            f'"{column}"'
            if column in old_columns
            else defaults[column]
            for column in target_columns
        ]

        conn.execute(
            f"""
            INSERT INTO projects_new ({", ".join(target_columns)})
            SELECT {", ".join(select_expressions)}
            FROM projects
            """
        )

        old_count = conn.execute(
            "SELECT COUNT(*) FROM projects"
        ).fetchone()[0]

        new_count = conn.execute(
            "SELECT COUNT(*) FROM projects_new"
        ).fetchone()[0]

        if old_count != new_count:
            raise RuntimeError(
                "Миграция projects прервана: количество записей не совпало"
            )

        conn.execute("DROP TABLE projects")
        conn.execute("ALTER TABLE projects_new RENAME TO projects")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_link "
            "ON projects(link)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_owner "
            "ON projects(owner_tg_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_status "
            "ON projects(moderation_status, ownership_status)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_catalog "
            "ON projects(moderation_status, is_official, telegram_verified)"
        )

        conn.commit()

        logging.info(
            "Миграция projects завершена. Сохранено записей: %s",
            new_count
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.execute("PRAGMA foreign_keys = ON")


# ============================================================
# ЖУРНАЛ МОДЕРАЦИИ
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_moderation_log(
    conn,
    admin_tg_id,
    project_id,
    action,
    comment=None
):
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(moderation_log)"
        ).fetchall()
    }

    values = {
        "admin_tg_id": admin_tg_id,
        "project_id": project_id,
        "action": action,
        "comment": comment,
        "created_at": now_iso()
    }

    usable = [
        (name, value)
        for name, value in values.items()
        if name in columns
    ]

    if usable:
        names = ", ".join(
            name for name, _ in usable
        )

        placeholders = ", ".join(
            "?" for _ in usable
        )

        conn.execute(
            f"INSERT INTO moderation_log ({names}) VALUES ({placeholders})",
            [value for _, value in usable]
        )


# ============================================================
# УСТРАНЕНИЕ ДУБЛЕЙ ПРОЕКТОВ
# ============================================================

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
    rows = conn.execute(
        """
        SELECT
            id,
            owner_tg_id,
            title,
            link,
            category,
            description,
            website,
            ownership_status,
            moderation_status,
            created_at,
            is_official,
            telegram_verified,
            source_url,
            is_seed,
            topic,
            community_type
        FROM projects
        ORDER BY id ASC
        """
    ).fetchall()

    groups = {}

    for row in rows:
        canonical_link = canonical_project_link(
            row["link"],
            row["id"]
        )

        groups.setdefault(
            canonical_link,
            []
        ).append(row)

    duplicate_groups = 0
    removed_rows = 0
    normalized_rows = 0

    for canonical_link, group in groups.items():
        keeper = group[0]
        keeper_id = keeper["id"]

        owner_tg_id = keeper["owner_tg_id"]

        if owner_tg_id is None:
            owner_tg_id = next(
                (
                    row["owner_tg_id"]
                    for row in group
                    if row["owner_tg_id"] is not None
                ),
                None
            )

        ownership_statuses = {
            str(row["ownership_status"] or "").lower()
            for row in group
        }

        if "verified" in ownership_statuses:
            ownership_status = "verified"
        else:
            ownership_status = (
                keeper["ownership_status"]
                or _first_nonempty(group, "ownership_status")
                or "unverified"
            )

        moderation_statuses = {
            str(row["moderation_status"] or "").lower()
            for row in group
        }

        if "approved" in moderation_statuses:
            moderation_status = "approved"
        elif "pending" in moderation_statuses:
            moderation_status = "pending"
        elif "rejected" in moderation_statuses:
            moderation_status = "rejected"
        else:
            moderation_status = (
                keeper["moderation_status"]
                or "pending"
            )

        is_official = max(
            int(row["is_official"] or 0)
            for row in group
        )

        telegram_verified = max(
            int(row["telegram_verified"] or 0)
            for row in group
        )

        is_seed = max(
            int(row["is_seed"] or 0)
            for row in group
        )

        title = (
            keeper["title"]
            or _first_nonempty(group, "title")
            or canonical_link
        )

        category = (
            keeper["category"]
            or _first_nonempty(group, "category")
        )

        description = (
            keeper["description"]
            or _first_nonempty(group, "description")
        )

        website = (
            keeper["website"]
            or _first_nonempty(group, "website")
            or ""
        )

        source_url = _first_nonempty(
            group,
            "source_url"
        )

        topic = _first_nonempty(
            group,
            "topic"
        )

        community_type = _first_nonempty(
            group,
            "community_type"
        )

        created_values = [
            str(row["created_at"])
            for row in group
            if row["created_at"]
        ]

        created_at = (
            min(created_values)
            if created_values
            else now_iso()
        )

        duplicate_ids = [
            row["id"]
            for row in group[1:]
        ]

        if duplicate_ids:
            duplicate_groups += 1
            removed_rows += len(duplicate_ids)

            placeholders = ", ".join(
                "?" for _ in duplicate_ids
            )

            reports_exists = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name='reports'
                """
            ).fetchone()

            if reports_exists:
                report_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(reports)"
                    ).fetchall()
                }

                if "project_id" in report_columns:
                    conn.execute(
                        f"""
                        UPDATE reports
                        SET project_id=?
                        WHERE project_id IN ({placeholders})
                        """,
                        [keeper_id, *duplicate_ids]
                    )

            log_exists = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name='moderation_log'
                """
            ).fetchone()

            if log_exists:
                log_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(moderation_log)"
                    ).fetchall()
                }

                if "project_id" in log_columns:
                    conn.execute(
                        f"""
                        UPDATE moderation_log
                        SET project_id=?
                        WHERE project_id IN ({placeholders})
                        """,
                        [keeper_id, *duplicate_ids]
                    )

            conn.execute(
                f"""
                DELETE FROM projects
                WHERE id IN ({placeholders})
                """,
                duplicate_ids
            )

        old_link = str(keeper["link"] or "")

        if old_link != canonical_link:
            normalized_rows += 1

        conn.execute(
            """
            UPDATE projects
            SET
                owner_tg_id=?,
                title=?,
                link=?,
                category=?,
                description=?,
                website=?,
                ownership_status=?,
                moderation_status=?,
                created_at=?,
                is_official=?,
                telegram_verified=?,
                source_url=?,
                is_seed=?,
                topic=?,
                community_type=?
            WHERE id=?
            """,
            (
                owner_tg_id,
                title,
                canonical_link,
                category,
                description,
                website,
                ownership_status,
                moderation_status,
                created_at,
                is_official,
                telegram_verified,
                source_url,
                is_seed,
                topic,
                community_type,
                keeper_id
            )
        )

    if duplicate_groups or normalized_rows:
        logging.warning(
            "Очистка каталога завершена: "
            "групп дублей объединено=%s, "
            "строк удалено=%s, "
            "ссылок нормализовано=%s",
            duplicate_groups,
            removed_rows,
            normalized_rows
        )
    else:
        logging.info(
            "Дубли проектов не обнаружены."
        )


# ============================================================
# СТАРТОВЫЙ КАТАЛОГ
# ============================================================

def seed_catalog(conn):
    added = 0

    for project in SEED_PROJECTS:
        link = project["link"].lower()

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO projects
            (
                owner_tg_id,
                title,
                link,
                category,
                description,
                website,
                ownership_status,
                moderation_status,
                created_at,
                is_official,
                telegram_verified,
                source_url,
                is_seed,
                topic,
                community_type
            )
            VALUES (
                NULL, ?, ?, ?, ?, '',
                'verified', 'approved', ?,
                ?, 0, ?, 1, ?, ?
            )
            """,
            (
                project["title"],
                link,
                project["category"],
                project["description"],
                now_iso(),
                project.get("is_official", 0),
                project.get("source_url"),
                project.get("topic"),
                project.get("community_type")
            )
        )

        if cursor.rowcount > 0:
            added += 1

    if added:
        logging.info(
            "В стартовый каталог добавлено проектов: %s",
            added
        )


async def seed_public_catalog():
    with db_connect() as conn:
        already_imported = conn.execute(
            """
            SELECT COUNT(*)
            FROM projects
            WHERE source_url=? AND is_seed=1
            """,
            (PUBLIC_DATASET_PAGE,)
        ).fetchone()[0]

    if already_imported >= PUBLIC_DATASET_LIMIT:
        logging.info(
            "Публичный каталог уже импортирован: %s ресурсов",
            already_imported
        )
        return

    timeout = aiohttp.ClientTimeout(
        total=90,
        connect=20
    )

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                PUBLIC_DATASET_URL,
                headers={
                    "User-Agent": "LINKCHECK/1.0",
                    "Accept": "text/csv,*/*"
                }
            ) as response:

                if response.status != 200:
                    raise RuntimeError(
                        f"Источник каталога вернул HTTP {response.status}"
                    )

                csv_text = await response.text()

        reader = csv.DictReader(
            io.StringIO(csv_text)
        )

        if not reader.fieldnames:
            raise RuntimeError(
                "В CSV отсутствуют заголовки"
            )

        normalized_headers = {
            name.strip().lower()
            for name in reader.fieldnames
            if name
        }

        if "username" not in normalized_headers:
            raise RuntimeError(
                "В CSV отсутствует обязательная колонка username"
            )

        imported = 0
        skipped = 0

        with db_connect() as conn:
            for item in reader:
                if imported >= PUBLIC_DATASET_LIMIT:
                    break

                item = {
                    str(key).strip().lower(): value
                    for key, value in item.items()
                    if key is not None
                }

                raw_username = str(
                    item.get("username") or ""
                ).strip()

                username = raw_username.lstrip("@").strip()

                if not re.fullmatch(
                    r"[A-Za-z0-9_]{5,32}",
                    username
                ):
                    skipped += 1
                    continue

                link = "@" + username.lower()

                title = str(
                    item.get("title") or ""
                ).strip()

                if not title:
                    title = username

                title = title[:80]

                topic = str(
                    item.get("topic") or ""
                ).strip() or None

                community_type = str(
                    item.get("community_type") or ""
                ).strip() or None

                if topic:
                    description = (
                        f"Публичный Telegram-канал. Тематика: {topic}."
                    )
                else:
                    description = "Публичный Telegram-канал."

                if community_type:
                    description += (
                        f" Тип сообщества: {community_type}."
                    )

                description = description[:1000]

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO projects
                    (
                        owner_tg_id,
                        title,
                        link,
                        category,
                        description,
                        website,
                        ownership_status,
                        moderation_status,
                        created_at,
                        is_official,
                        telegram_verified,
                        source_url,
                        is_seed,
                        topic,
                        community_type
                    )
                    VALUES (
                        NULL, ?, ?, 'channel', ?, '',
                        'unverified', 'approved', ?,
                        0, 0, ?, 1, ?, ?
                    )
                    """,
                    (
                        title,
                        link,
                        description,
                        now_iso(),
                        PUBLIC_DATASET_PAGE,
                        topic,
                        community_type
                    )
                )

                if cursor.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1

        logging.info(
            "Импорт публичного каталога завершён: "
            "добавлено %s, пропущено %s",
            imported,
            skipped
        )

        if imported < PUBLIC_DATASET_LIMIT:
            logging.warning(
                "Импортировано меньше 1500 новых записей. "
                "Добавлено: %s. Возможно, часть username уже была в базе.",
                imported
            )

    except Exception as exc:
        logging.exception(
            "Не удалось загрузить публичный каталог: %s",
            exc
        )


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ============================================================

def init_db():
    with db_connect() as conn:
        conn.execute(
            "PRAGMA journal_mode = WAL"
        )

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

        ensure_column(
            conn,
            "projects",
            "owner_tg_id",
            "INTEGER"
        )

        ensure_column(
            conn,
            "projects",
            "ownership_status",
            "TEXT NOT NULL DEFAULT 'unverified'"
        )

        ensure_column(
            conn,
            "projects",
            "moderation_status",
            "TEXT NOT NULL DEFAULT 'pending'"
        )

        ensure_column(
            conn,
            "projects",
            "is_official",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "projects",
            "telegram_verified",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "projects",
            "source_url",
            "TEXT"
        )

        ensure_column(
            conn,
            "projects",
            "is_seed",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "projects",
            "topic",
            "TEXT"
        )

        ensure_column(
            conn,
            "projects",
            "community_type",
            "TEXT"
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

        for col, definition in (
            ("admin_tg_id", "INTEGER"),
            ("project_id", "INTEGER"),
            ("action", "TEXT"),
            ("comment", "TEXT"),
            ("created_at", "TEXT")
        ):
            ensure_column(
                conn,
                "moderation_log",
                col,
                definition
            )

        ensure_column(
            conn,
            "users",
            "is_trusted",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "users",
            "has_requested_access",
            "INTEGER NOT NULL DEFAULT 0"
        )

        migrate_projects_owner_nullable(conn)

        deduplicate_projects(conn)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_link "
            "ON projects(link)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_owner "
            "ON projects(owner_tg_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_status "
            "ON projects(moderation_status, ownership_status)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_catalog "
            "ON projects(moderation_status, is_official, telegram_verified)"
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_link_unique
            ON projects(lower(link))
            """
        )

        seed_catalog(conn)


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

def upsert_user(tg_id: int, username: str | None):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users (tg_id, username, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id)
            DO UPDATE SET username = excluded.username
            """,
            (tg_id, username, now_iso())
        )


def validate_telegram_init_data(init_data: str):
    if not init_data or len(init_data) > 16_384:
        return None

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True
        )

        keys = [key for key, _ in pairs]

        if len(keys) != len(set(keys)):
            return None

        data = dict(pairs)

        received_hash = data.pop(
            "hash",
            None
        )

        if not received_hash or not re.fullmatch(
            r"[0-9a-fA-F]{64}",
            received_hash
        ):
            return None

        auth_date = int(
            data.get("auth_date", "0")
        )

        current = int(time.time())

        if (
            auth_date <= 0
            or auth_date > current + 60
            or current - auth_date > INIT_DATA_MAX_AGE
        ):
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(data.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash.lower()
        ):
            return None

        user_raw = data.get("user")

        if not user_raw:
            return None

        user = json.loads(user_raw)
        user_id = user.get("id")

        if not isinstance(user_id, int) or user_id <= 0:
            return None

        return {
            "id": user_id,
            "username": user.get("username")
        }

    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def authenticated_user(request):
    auth = request.headers.get(
        "Authorization",
        ""
    )

    init_data = (
        auth[4:].strip()
        if auth.startswith("tma ")
        else request.headers.get(
            "X-Telegram-Init-Data",
            ""
        )
    )

    return validate_telegram_init_data(
        init_data
    )


def json_error(message, status=400):
    return web.json_response(
        {"error": message},
        status=status
    )


async def read_json(request):
    try:
        data = await request.json()

        if not isinstance(data, dict):
            raise ValueError

        return data

    except (
        json.JSONDecodeError,
        ValueError,
        web.HTTPException
    ):
        raise web.HTTPBadRequest(
            text="Некорректный JSON"
        )


def normalize_link(raw):
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or len(raw) > 300
    ):
        raise ValueError(
            "Укажите корректную ссылку или username"
        )

    value = raw.strip()

    if re.fullmatch(
        r"[A-Za-z0-9_]{5,32}",
        value
    ):
        username = value

    elif value.startswith("@"):
        username = value[1:]

    else:
        candidate = value

        if candidate.startswith((
            "t.me/",
            "www.t.me/",
            "telegram.me/",
            "www.telegram.me/"
        )):
            candidate = "https://" + candidate

        elif not candidate.startswith((
            "http://",
            "https://"
        )):
            raise ValueError(
                "Введите username, @username или ссылку t.me/username"
            )

        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()

        if parsed.scheme not in {"http", "https"} or host not in {
            "t.me",
            "www.t.me",
            "telegram.me",
            "www.telegram.me"
        }:
            raise ValueError(
                "Поддерживаются только username и ссылки t.me"
            )

        if parsed.query or parsed.fragment:
            raise ValueError(
                "Укажите публичную ссылку без параметров"
            )

        parts = [
            part
            for part in parsed.path.strip("/").split("/")
            if part
        ]

        if len(parts) != 1:
            raise ValueError(
                "Укажите публичный username без дополнительных путей"
            )

        username = parts[0]

    username = username.strip().lstrip("@").lower()

    if not re.fullmatch(
        r"[a-z0-9_]{5,32}",
        username
    ):
        raise ValueError(
            "Некорректный Telegram username"
        )

    return "@" + username


def valid_website(value):
    if not value:
        return ""

    if not isinstance(value, str) or len(value) > 300:
        raise ValueError(
            "Некорректный адрес сайта"
        )

    parsed = urlsplit(value.strip())

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(
            "Сайт должен начинаться с https:// или http://"
        )

    return value.strip()


# ============================================================
# ОБЩАЯ ОБРАБОТКА FLOODWAIT
# ============================================================

def is_flood_wait_error(exc):
    name = type(exc).__name__

    return (
        name in {
            "FloodWaitError",
            "FloodTestPhoneWaitError"
        }
        or "FloodWait" in name
        or bool(
            re.search(
                r"FLOOD_WAIT_\d+",
                str(exc),
                re.IGNORECASE
            )
        )
    )


def get_flood_wait_seconds(exc):
    seconds = getattr(
        exc,
        "seconds",
        None
    )

    if seconds is None:
        seconds = getattr(
            exc,
            "value",
            None
        )

    if seconds is not None:
        try:
            return max(1, int(seconds))
        except (TypeError, ValueError):
            pass

    match = re.search(
        r"FLOOD_WAIT_(\d+)",
        str(exc),
        re.IGNORECASE
    )

    if match:
        return max(1, int(match.group(1)))

    return 60


def register_telegram_flood_wait(exc, source="Telegram"):
    global telegram_flood_wait_until

    if not is_flood_wait_error(exc):
        return 0

    seconds = get_flood_wait_seconds(exc)
    new_until = time.monotonic() + seconds

    telegram_flood_wait_until = max(
        telegram_flood_wait_until,
        new_until
    )

    logging.warning(
        "%s FloodWait: %s сек. Все новые MTProto-запросы "
        "будут приостановлены.",
        source,
        seconds
    )

    return seconds


def telegram_flood_remaining():
    return max(
        0,
        telegram_flood_wait_until - time.monotonic()
    )


# ============================================================
# КЭШ РЕЗУЛЬТАТОВ ПРОВЕРКИ
# ============================================================

def telegram_unavailable_result(reason=None, retry_after=0):
    retry_after = max(
        0,
        int(retry_after or 0)
    )

    if not reason:
        reason = (
            "Telegram временно недоступен. "
            "Не удалось получить достоверный результат."
        )

    return {
        "status": "unavailable",
        "verified": False,
        "reason": reason,
        "retry_after": retry_after,
        "verification_source": None
    }


def is_telegram_not_found_error(exc):
    name = type(exc).__name__

    if name in {
        "UsernameNotOccupiedError",
        "UsernameInvalidError"
    }:
        return True

    if name == "ValueError":
        message = str(exc).lower()

        not_found_phrases = (
            "no user has",
            "no entity corresponds",
            "cannot find any entity",
            "username not found",
            "could not find the input entity"
        )

        return any(
            phrase in message
            for phrase in not_found_phrases
        )

    return False


def cache_telegram_check(key, result, ttl):
    telegram_check_cache[key] = {
        "result": dict(result),
        "expires_at": time.monotonic() + ttl
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
        cache_telegram_check(
            key,
            result,
            TELEGRAM_CHECK_CACHE_TTL
        )

    elif status == "not_found":
        cache_telegram_check(
            key,
            result,
            TELEGRAM_NOT_FOUND_CACHE_TTL
        )

    elif status == "wrong_type":
        cache_telegram_check(
            key,
            result,
            TELEGRAM_CHECK_CACHE_TTL
        )

    elif status == "unavailable":
        cache_telegram_check(
            key,
            result,
            TELEGRAM_UNAVAILABLE_CACHE_TTL
        )


# ============================================================
# ОПРЕДЕЛЕНИЕ ТИПА ПО ПУБЛИЧНОЙ СТРАНИЦЕ TELEGRAM
# ============================================================

def infer_public_page_type(page_html):
    """
    Определяет тип только тогда, когда публичная страница
    содержит достаточно явные признаки.

    Если определить тип нельзя, возвращает None.
    """

    match = re.search(
        r'class=["\'][^"\']*tgme_page_extra[^"\']*["\'][^>]*>(.*?)</div>',
        page_html,
        re.IGNORECASE | re.DOTALL
    )

    if not match:
        return None

    extra = re.sub(
        r"<[^>]+>",
        " ",
        match.group(1)
    )

    extra = html.unescape(
        extra
    ).strip().lower()

    if re.search(
        r"\b(subscribers?|подписчик|подписчиков|подписчика)\b",
        extra
    ):
        return "channel"

    if re.search(
        r"\b(members?|участник|участников|участника)\b",
        extra
    ):
        return "group"

    if re.search(
        r"\b(bot|бот)\b",
        extra
    ):
        return "bot"

    return None


# ============================================================
# ПУБЛИЧНАЯ HTTP-ПРОВЕРКА TELEGRAM
# ============================================================

async def check_telegram_public_page(username):
    """
    Проверяет открытую страницу https://t.me/username.

    Эта проверка не использует MTProto и не зависит от FloodWait
    пользовательской Telethon-сессии.

    Результаты:
    exists     — публичная страница ресурса обнаружена;
    not_found  — Telegram вернул HTTP 404;
    unavailable — страницу не удалось надёжно определить.

    Если нет однозначных признаков существования, функция
    не делает ложный вывод об отсутствии ресурса.
    """

    global telegram_public_page_last_check_at

    username = str(
        username or ""
    ).strip().lstrip("@").lower()

    if not username:
        return {
            "status": "not_found",
            "type": None,
            "verified": False,
            "reason": "Username не указан.",
            "retry_after": 0,
            "verification_source": "public_page"
        }

    timeout = aiohttp.ClientTimeout(
        total=TELEGRAM_PUBLIC_PAGE_TIMEOUT,
        connect=3,
        sock_read=5
    )

    async with telegram_public_page_lock:
        elapsed = (
            time.monotonic()
            - telegram_public_page_last_check_at
        )

        if elapsed < TELEGRAM_PUBLIC_PAGE_MIN_INTERVAL:
            await asyncio.sleep(
                TELEGRAM_PUBLIC_PAGE_MIN_INTERVAL - elapsed
            )

        telegram_public_page_last_check_at = time.monotonic()

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 "
                        "(Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                    "Accept-Language": "en-US,en;q=0.9"
                }
            ) as session:

                async with session.get(
                    f"https://t.me/{username}",
                    allow_redirects=True
                ) as response:

                    status_code = response.status

                    retry_after_header = response.headers.get(
                        "Retry-After",
                        "0"
                    )

                    try:
                        retry_after = max(
                            0,
                            int(retry_after_header)
                        )
                    except (TypeError, ValueError):
                        retry_after = 0

                    raw_html = await response.content.read(
                        1_500_000
                    )

                    page_html = raw_html.decode(
                        "utf-8",
                        errors="ignore"
                    )

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError
        ) as exc:
            logging.warning(
                "Telegram public page request failed for @%s: %s",
                username,
                exc
            )

            return telegram_unavailable_result(
                "Не удалось открыть публичную страницу Telegram."
            )

    if status_code == 404:
        return {
            "status": "not_found",
            "type": None,
            "verified": False,
            "reason": (
                "Публичная страница Telegram не найдена."
            ),
            "retry_after": 0,
            "verification_source": "public_page"
        }

    if status_code == 429:
        return telegram_unavailable_result(
            "Telegram ограничил частоту обращений к публичным страницам.",
            retry_after
        )

    if status_code != 200:
        return telegram_unavailable_result(
            f"Публичная страница Telegram вернула HTTP {status_code}."
        )

    lower_html = page_html.lower()

    public_page_markers = (
        "tgme_page_title",
        "tgme_page_extra",
        "tgme_page_description",
        "tgme_page_photo",
        "tgme_page_action",
        "telegram: view @",
        "telegram: contact @"
    )

    page_found = any(
        marker in lower_html
        for marker in public_page_markers
    )

    if not page_found:
        return telegram_unavailable_result(
            "Telegram не вернул распознаваемую публичную страницу."
        )

    detected_type = infer_public_page_type(
        page_html
    )

    return {
        "status": "exists",
        "type": detected_type or "other",
        "type_confirmed": detected_type is not None,
        "verified": False,
        "reason": "",
        "retry_after": 0,
        "verification_source": "public_page"
    }


# ============================================================
# ПРОВЕРКА TELEGRAM
# ============================================================

async def check_telegram_resource(link, expected_category="other"):
    """
    Порядок проверки:

    1. Кэш.
    2. Публичная страница t.me.
    3. Telethon — только если публичной страницы недостаточно
       для определения результата или типа.

    При FloodWait поиск через публичные страницы продолжает работать.
    """

    global telegram_last_check_at

    username = str(
        link or ""
    ).strip().lstrip("@").lower()

    if not username:
        return {
            "status": "not_found",
            "verified": False,
            "reason": "Не указан Telegram username.",
            "verification_source": None
        }

    cache_key = (
        username,
        expected_category
    )

    cached = get_cached_telegram_check(
        cache_key
    )

    if cached is not None:
        return cached

    # --------------------------------------------------------
    # ПЕРВЫЙ ЭТАП — ПУБЛИЧНАЯ СТРАНИЦА TELEGRAM
    # --------------------------------------------------------

    public_result = await check_telegram_public_page(
        username
    )

    public_status = public_result.get(
        "status"
    )

    # Если Telegram вернул 404, ресурс не найден.
    if public_status == "not_found":
        cache_result_by_status(
            cache_key,
            public_result
        )

        return public_result

    # Для главного поиска достаточно обнаружить
    # публичную страницу. MTProto здесь не нужен.
    if (
        public_status == "exists"
        and expected_category == "other"
    ):
        cache_result_by_status(
            cache_key,
            public_result
        )

        return public_result

    # Если тип удалось определить по публичной странице,
    # сравниваем его с выбранной категорией.
    if (
        public_status == "exists"
        and public_result.get("type_confirmed")
        and expected_category != "other"
    ):
        detected_type = public_result.get(
            "type"
        )

        if detected_type != expected_category:
            result = {
                "status": "wrong_type",
                "type": detected_type,
                "verified": False,
                "reason": (
                    "Username существует в Telegram, но найденный тип "
                    f"ресурса не соответствует категории «{expected_category}»."
                ),
                "retry_after": 0,
                "verification_source": "public_page"
            }

            cache_result_by_status(
                cache_key,
                result
            )

            return result

        cache_result_by_status(
            cache_key,
            public_result
        )

        return public_result

    # --------------------------------------------------------
    # ВТОРОЙ ЭТАП — TELETHON
    # --------------------------------------------------------

    # Telethon нужен только как резервная проверка.
    # При FloodWait публичный результат сохраняется.
    if not tg_ready or tg_client is None:
        if public_status == "exists":
            cache_result_by_status(
                cache_key,
                public_result
            )

            return public_result

        result = telegram_unavailable_result(
            "Не удалось получить ответ от Telegram."
        )

        cache_result_by_status(
            cache_key,
            result
        )

        return result

    remaining = telegram_flood_remaining()

    if remaining > 0:
        if public_status == "exists":
            cache_result_by_status(
                cache_key,
                public_result
            )

            return public_result

        result = telegram_unavailable_result(
            "Telegram временно недоступен.",
            math.ceil(remaining)
        )

        cache_result_by_status(
            cache_key,
            result
        )

        return result

    async with telegram_check_lock:
        cached = get_cached_telegram_check(
            cache_key
        )

        if cached is not None:
            return cached

        remaining = telegram_flood_remaining()

        if remaining > 0:
            if public_status == "exists":
                cache_result_by_status(
                    cache_key,
                    public_result
                )

                return public_result

            result = telegram_unavailable_result(
                "Telegram временно недоступен.",
                math.ceil(remaining)
            )

            cache_result_by_status(
                cache_key,
                result
            )

            return result

        elapsed = (
            time.monotonic()
            - telegram_last_check_at
        )

        if elapsed < TELEGRAM_CHECK_MIN_INTERVAL:
            await asyncio.sleep(
                TELEGRAM_CHECK_MIN_INTERVAL - elapsed
            )

        telegram_last_check_at = time.monotonic()

        try:
            entity = await tg_client.get_entity(
                username
            )

            entity_type = "other"

            if User is not None and isinstance(entity, User):
                entity_type = (
                    "bot"
                    if bool(getattr(entity, "bot", False))
                    else "user"
                )

            elif Channel is not None and isinstance(entity, Channel):
                entity_type = (
                    "group"
                    if bool(getattr(entity, "megagroup", False))
                    else "channel"
                )

            elif Chat is not None and isinstance(entity, Chat):
                entity_type = "group"

            category_ok = (
                expected_category == "other"
                or (
                    expected_category == "bot"
                    and entity_type == "bot"
                )
                or (
                    expected_category == "channel"
                    and entity_type == "channel"
                )
                or (
                    expected_category == "group"
                    and entity_type == "group"
                )
            )

            if not category_ok:
                result = {
                    "status": "wrong_type",
                    "type": entity_type,
                    "verified": False,
                    "reason": (
                        "Username существует в Telegram, но найденный тип "
                        f"ресурса не соответствует категории «{expected_category}»."
                    ),
                    "retry_after": 0,
                    "verification_source": "mtproto"
                }

                cache_result_by_status(
                    cache_key,
                    result
                )

                return result

            result = {
                "status": "exists",
                "type": entity_type,
                "verified": bool(
                    getattr(entity, "verified", False)
                ),
                "reason": "",
                "retry_after": 0,
                "verification_source": "mtproto"
            }

            cache_result_by_status(
                cache_key,
                result
            )

            return result

        except Exception as exc:
            if is_flood_wait_error(exc):
                seconds = register_telegram_flood_wait(
                    exc,
                    "Telegram check"
                )

                # Если публичная страница уже открылась,
                # сохраняем результат вместо ошибки FloodWait.
                if public_status == "exists":
                    cache_result_by_status(
                        cache_key,
                        public_result
                    )

                    return public_result

                result = telegram_unavailable_result(
                    "Telegram временно недоступен.",
                    seconds
                )

                cache_result_by_status(
                    cache_key,
                    result
                )

                return result

            if is_telegram_not_found_error(exc):
                result = {
                    "status": "not_found",
                    "verified": False,
                    "reason": (
                        "Такой публичный username не найден в Telegram."
                    ),
                    "retry_after": 0,
                    "verification_source": "mtproto"
                }

                cache_result_by_status(
                    cache_key,
                    result
                )

                return result

            logging.warning(
                "Telegram username check failed for %s: %s: %s",
                link,
                type(exc).__name__,
                exc
            )

            # Если публичная страница обнаружена,
            # не теряем положительный результат из-за ошибки MTProto.
            if public_status == "exists":
                cache_result_by_status(
                    cache_key,
                    public_result
                )

                return public_result

            result = telegram_unavailable_result(
                "Не удалось получить ответ от Telegram."
            )

            cache_result_by_status(
                cache_key,
                result
            )

            return result


# ============================================================
# ПРОВЕРКА СТАРТОВЫХ РЕСУРСОВ
# ============================================================

async def verify_seed_projects():
    """
    Функция сохранена для отдельного ручного запуска.

    Автоматически при старте проекта больше не запускается:
    массовые MTProto-запросы создавали FloodWait.
    """

    if not tg_ready or tg_client is None:
        logging.warning(
            "Проверка стартовых проектов пропущена: MTProto недоступен."
        )
        return

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, link, category
            FROM projects
            WHERE is_seed=1
              AND COALESCE(source_url, '') != ?
            """,
            (PUBLIC_DATASET_PAGE,)
        ).fetchall()

    total = len(rows)
    checked = 0

    for row in rows:
        try:
            result = await check_telegram_resource(
                row["link"],
                row["category"]
            )

            if result["status"] == "exists":
                with db_connect() as conn:
                    conn.execute(
                        """
                        UPDATE projects
                        SET telegram_verified=?
                        WHERE id=? AND is_seed=1
                        """,
                        (
                            1 if result.get("verified") else 0,
                            row["id"]
                        )
                    )

                checked += 1
# LINKCHECK — backend с проверкой публичных Telegram-ресурсов

import asyncio
import csv
import gzip
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import sqlite3
import time

from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from aiohttp import web

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MINIAPP_URL = os.getenv("MINIAPP_URL", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DB_PATH = os.getenv("DB_PATH", "linkcheck.db")
PORT = int(os.getenv("PORT", "8080"))
INIT_DATA_MAX_AGE = int(os.getenv("INIT_DATA_MAX_AGE", "86400"))

TG_API_ID = int(os.getenv("TG_API_ID", "0") or "0")
TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "linkcheck_telegram").strip()

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

TELEGRAM_CHECK_CACHE_TTL = 300
TELEGRAM_NOT_FOUND_CACHE_TTL = 60
TELEGRAM_UNAVAILABLE_CACHE_TTL = 15

TELEGRAM_CHECK_MIN_INTERVAL = 0.8

# Общая блокировка MTProto после FloodWait.
telegram_flood_wait_until = 0

telegram_last_check_at = 0

telegram_check_cache = {}

telegram_check_lock = asyncio.Lock()


# ============================================================
# НАСТРОЙКИ КЭША ПРОФИЛЯ СОЗДАТЕЛЯ
# ============================================================

FOUNDER_PROFILE_CACHE_TTL = 300
FOUNDER_AVATAR_CACHE_TTL = 300
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
founder_profile_cache = None
founder_profile_cache_time = 0

founder_profile_lock = asyncio.Lock()

founder_gift_cache = {}
founder_gift_lock = asyncio.Lock()


# ============================================================
# СТАРТОВЫЕ РЕСУРСЫ LINKCHECK
# ============================================================

SEED_PROJECTS = [
    {
        "title": "Telegram",
        "link": "@telegram",
        "category": "channel",
        "description": "Официальный канал Telegram.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "Telegram Tips",
        "link": "@TelegramTips",
        "category": "channel",
        "description": "Официальные советы и новости о возможностях Telegram.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "BotNews",
        "link": "@BotNews",
        "category": "channel",
        "description": "Новости платформы Telegram для разработчиков ботов.",
        "is_official": 1,
        "source_url": "https://core.telegram.org/bots"
    },
    {
        "title": "Telegram Premium",
        "link": "@premium",
        "category": "channel",
        "description": "Информация о возможностях Telegram Premium.",
        "is_official": 1,
        "source_url": "https://telegram.org/faq_premium"
    },
    {
        "title": "BotFather",
        "link": "@BotFather",
        "category": "bot",
        "description": "Официальный бот Telegram для создания и управления ботами.",
        "is_official": 1,
        "source_url": "https://core.telegram.org/bots"
    },
    {
        "title": "Premium Bot",
        "link": "@PremiumBot",
        "category": "bot",
        "description": "Официальный бот для оформления Telegram Premium.",
        "is_official": 1,
        "source_url": "https://telegram.org/faq_premium"
    },
    {
        "title": "Stickers",
        "link": "@Stickers",
        "category": "bot",
        "description": "Официальный бот Telegram для создания и управления стикерами.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "GIF",
        "link": "@gif",
        "category": "bot",
        "description": "Встроенный бот Telegram для поиска GIF.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "Vote",
        "link": "@vote",
        "category": "bot",
        "description": "Бот Telegram для создания опросов.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "QuizBot",
        "link": "@QuizBot",
        "category": "bot",
        "description": "Бот Telegram для создания викторин.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "DiscussBot",
        "link": "@discussbot",
        "category": "bot",
        "description": "Бот для обсуждений и комментариев в каналах.",
        "is_official": 1,
        "source_url": "https://telegram.org/"
    },
    {
        "title": "Binance",
        "link": "@BinanceExchange",
        "category": "group",
        "description": "Официальное англоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Announcements",
        "link": "@binance_announcements",
        "category": "channel",
        "description": "Официальный канал объявлений Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Chinese",
        "link": "@BinanceChinese",
        "category": "group",
        "description": "Официальное китайскоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance China Announcements",
        "link": "@binance_cn",
        "category": "channel",
        "description": "Китайскоязычный канал объявлений Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Russian Community",
        "link": "@BinanceRussianSpeaking",
        "category": "group",
        "description": "Официальное сообщество Binance для русскоговорящей аудитории.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Russia",
        "link": "@binance_ru",
        "category": "channel",
        "description": "Русскоязычный канал новостей Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Kazakhstan",
        "link": "@binance_kz",
        "category": "group",
        "description": "Региональное сообщество Binance для Казахстана.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Kyrgyzstan",
        "link": "@BinanceKyrgyz",
        "category": "group",
        "description": "Региональное сообщество Binance для Кыргызстана.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Armenia",
        "link": "@Binance_Armenian",
        "category": "group",
        "description": "Официальное сообщество Binance для Армении.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/community"
    },
    {
        "title": "Binance Balkans",
        "link": "@binancebalkans",
        "category": "group",
        "description": "Официальное сообщество Binance для Балкан.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Bangladesh",
        "link": "@BinanceBangladeshi",
        "category": "group",
        "description": "Региональное сообщество Binance для Бангладеш.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Cameroon",
        "link": "@BinanceCameroun",
        "category": "group",
        "description": "Региональное сообщество Binance для Камеруна.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Germany",
        "link": "@BinanceGerman",
        "category": "group",
        "description": "Немецкоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Australia",
        "link": "@binanceaustralia",
        "category": "group",
        "description": "Австралийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Namibia",
        "link": "@BinanceNamibia",
        "category": "group",
        "description": "Региональное сообщество Binance для Намибии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance New Zealand",
        "link": "@BinanceNewZealand",
        "category": "group",
        "description": "Региональное сообщество Binance для Новой Зеландии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Zambia",
        "link": "@BinanceZambia",
        "category": "group",
        "description": "Региональное сообщество Binance для Замбии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Spain",
        "link": "@BinanceSpanish",
        "category": "group",
        "description": "Испаноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Argentina",
        "link": "@binanceargentinaoficial",
        "category": "group",
        "description": "Региональное сообщество Binance для Аргентины.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Spain Official",
        "link": "@Binance_ESP",
        "category": "group",
        "description": "Испаноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Philippines",
        "link": "@BinanceFilipino",
        "category": "group",
        "description": "Филиппинское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance France",
        "link": "@BinanceFrench",
        "category": "group",
        "description": "Франкоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Greece",
        "link": "@BinanceGreek",
        "category": "group",
        "description": "Грекоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance India",
        "link": "@BinanceIndian",
        "category": "group",
        "description": "Индийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Italy",
        "link": "@BinanceItalian",
        "category": "group",
        "description": "Италоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Kenya",
        "link": "@BinanceKenya",
        "category": "group",
        "description": "Кенийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Nigeria",
        "link": "@BinanceNigeria",
        "category": "group",
        "description": "Нигерийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Poland",
        "link": "@Binance_Polish",
        "category": "group",
        "description": "Польскоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Portugal",
        "link": "@BinancePortuguese",
        "category": "group",
        "description": "Португалоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Portugal Community",
        "link": "@Binance_PT",
        "category": "group",
        "description": "Региональное сообщество Binance для Португалии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Romania",
        "link": "@BinanceRomanian",
        "category": "group",
        "description": "Румыноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Seychelles",
        "link": "@BinanceSeychelles",
        "category": "group",
        "description": "Региональное сообщество Binance для Сейшельских Островов.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance South Africa",
        "link": "@BinanceSouthAfrica",
        "category": "group",
        "description": "Южноафриканское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Sri Lanka",
        "link": "@binancesrilanka",
        "category": "group",
        "description": "Сообщество Binance для Шри-Ланки.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Tanzania",
        "link": "@BinanceTanzania",
        "category": "group",
        "description": "Танзанийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Vietnam",
        "link": "@BinanceVietnamese",
        "category": "group",
        "description": "Вьетнамское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Uganda",
        "link": "@BinanceUganda",
        "category": "group",
        "description": "Угандийское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Czech and Slovakia",
        "link": "@BinanceCzechoSlovakia",
        "category": "group",
        "description": "Сообщество Binance для Чехии и Словакии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Mongolian",
        "link": "@BinanceMongolian",
        "category": "group",
        "description": "Монгольское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Ukraine",
        "link": "@BinanceUkrainian",
        "category": "group",
        "description": "Украиноязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Hebrew",
        "link": "@BinanceHebrew",
        "category": "group",
        "description": "Ивритоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Arabic",
        "link": "@BinanceArabic",
        "category": "group",
        "description": "Арабоязычное сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Pakistan",
        "link": "@binancepakistani",
        "category": "group",
        "description": "Пакистанское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Nepal",
        "link": "@BinanceNepali",
        "category": "group",
        "description": "Непальское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Laos",
        "link": "@BinanceLaotian",
        "category": "group",
        "description": "Лаосское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Myanmar",
        "link": "@BinanceBurmese",
        "category": "group",
        "description": "Бирманское сообщество Binance.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Cambodia",
        "link": "@BinanceKhmer",
        "category": "group",
        "description": "Сообщество Binance для Камбоджи.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Flemish",
        "link": "@BinanceFlemish",
        "category": "group",
        "description": "Нидерландоязычное сообщество Binance для Фландрии.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/blog/community/421499824684903053"
    },
    {
        "title": "Binance Academy",
        "link": "@BinanceAcademy",
        "category": "channel",
        "description": "Образовательные материалы Binance Academy.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/academy"
    },
    {
        "title": "Binance Academy Chat",
        "link": "@BinanceAcademyChat",
        "category": "group",
        "description": "Сообщество Binance Academy.",
        "is_official": 1,
        "source_url": "https://www.binance.com/en/academy"
    },
    {
        "title": "Binance NFT",
        "link": "@BinanceNFTs",
        "category": "channel",
        "description": "Канал Binance, посвящённый NFT.",
        "is_official": 1,
        "source_url": "https://www.binance.com/"
    },
    {
        "title": "Binance Futures",
        "link": "@BinanceFutures_Official",
        "category": "channel",
        "description": "Обновления Binance Futures.",
        "is_official": 1,
        "source_url": "https://www.binance.com/"
    },
    {
        "title": "Binance API English",
        "link": "@binance_api_english",
        "category": "group",
        "description": "Сообщество пользователей и разработчиков Binance API.",
        "is_official": 1,
        "source_url": "https://www.binance.com/"
    },
    {
        "title": "Trust Wallet",
        "link": "@trustwallet",
        "category": "channel",
        "description": "Telegram-ресурс Trust Wallet, указанный в материалах Binance.",
        "is_official": 1,
        "source_url": "https://trustwallet.com/"
    },
    {
        "title": "TON Blockchain",
        "link": "@tonblockchain",
        "category": "channel",
        "description": "Официальный информационный ресурс TON.",
        "is_official": 1,
        "source_url": "https://ton.org/"
    }
]


# ============================================================
# TELEGRAM BOT
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    text = (
        "👋 Добро пожаловать в <b>LINKCHECK</b>!\n\n"
        "Платформа для проверки и поиска публичных Telegram-ресурсов.\n\n"
        "Нажми кнопку ниже, чтобы открыть приложение."
    )

    if MINIAPP_URL:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Открыть LINKCHECK",
                    web_app=WebAppInfo(url=MINIAPP_URL)
                )
            ]]
        )

        await message.answer(
            text,
            reply_markup=keyboard
        )

    else:
        await message.answer(
            text + "\n\n⚠️ Mini App пока не настроен. Проверь MINIAPP_URL в .env."
        )


# ============================================================
# DATABASE
# ============================================================

@contextmanager
def db_connect():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10
    )

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
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }

    if column not in columns:
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


# ============================================================
# МИГРАЦИЯ СТАРОЙ ТАБЛИЦЫ PROJECTS
# ============================================================

def migrate_projects_owner_nullable(conn):
    """
    Перестраивает projects, если owner_tg_id в старой базе имеет NOT NULL.

    Сохраняет ID и все существующие записи.
    Поддерживает topic и community_type.
    """

    info = conn.execute(
        "PRAGMA table_info(projects)"
    ).fetchall()

    owner_column = next(
        (
            row
            for row in info
            if row["name"] == "owner_tg_id"
        ),
        None
    )

    if owner_column is None or owner_column["notnull"] == 0:
        return

    logging.warning(
        "Обнаружена старая схема projects: owner_tg_id NOT NULL. "
        "Выполняется безопасная миграция."
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
                FOREIGN KEY (owner_tg_id)
                    REFERENCES users(tg_id) ON DELETE CASCADE
            )
        """)

        old_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(projects)"
            ).fetchall()
        }

        defaults = {
            "id": "NULL",
            "owner_tg_id": "NULL",
            "title": "''",
            "link": "''",
            "category": "NULL",
            "description": "NULL",
            "website": "NULL",
            "ownership_status": "'unverified'",
            "moderation_status": "'pending'",
            "created_at": "''",
            "is_official": "0",
            "telegram_verified": "0",
            "source_url": "NULL",
            "is_seed": "0",
            "topic": "NULL",
            "community_type": "NULL"
        }

        target_columns = list(defaults.keys())

        select_expressions = [
            f'"{column}"'
            if column in old_columns
            else defaults[column]
            for column in target_columns
        ]

        conn.execute(
            f"""
            INSERT INTO projects_new ({", ".join(target_columns)})
            SELECT {", ".join(select_expressions)}
            FROM projects
            """
        )

        old_count = conn.execute(
            "SELECT COUNT(*) FROM projects"
        ).fetchone()[0]

        new_count = conn.execute(
            "SELECT COUNT(*) FROM projects_new"
        ).fetchone()[0]

        if old_count != new_count:
            raise RuntimeError(
                "Миграция projects прервана: количество записей не совпало"
            )

        conn.execute("DROP TABLE projects")
        conn.execute("ALTER TABLE projects_new RENAME TO projects")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_link "
            "ON projects(link)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_owner "
            "ON projects(owner_tg_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_status "
            "ON projects(moderation_status, ownership_status)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_catalog "
            "ON projects(moderation_status, is_official, telegram_verified)"
        )

        conn.commit()

        logging.info(
            "Миграция projects завершена. Сохранено записей: %s",
            new_count
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.execute("PRAGMA foreign_keys = ON")


# ============================================================
# ЖУРНАЛ МОДЕРАЦИИ
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def write_moderation_log(
    conn,
    admin_tg_id,
    project_id,
    action,
    comment=None
):
    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(moderation_log)"
        ).fetchall()
    }

    values = {
        "admin_tg_id": admin_tg_id,
        "project_id": project_id,
        "action": action,
        "comment": comment,
        "created_at": now_iso()
    }

    usable = [
        (name, value)
        for name, value in values.items()
        if name in columns
    ]

    if usable:
        names = ", ".join(
            name for name, _ in usable
        )

        placeholders = ", ".join(
            "?" for _ in usable
        )

        conn.execute(
            f"INSERT INTO moderation_log ({names}) VALUES ({placeholders})",
            [value for _, value in usable]
        )


# ============================================================
# УСТРАНЕНИЕ ДУБЛЕЙ ПРОЕКТОВ
# ============================================================

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
    rows = conn.execute(
        """
        SELECT
            id,
            owner_tg_id,
            title,
            link,
            category,
            description,
            website,
            ownership_status,
            moderation_status,
            created_at,
            is_official,
            telegram_verified,
            source_url,
            is_seed,
            topic,
            community_type
        FROM projects
        ORDER BY id ASC
        """
    ).fetchall()

    groups = {}

    for row in rows:
        canonical_link = canonical_project_link(
            row["link"],
            row["id"]
        )

        groups.setdefault(
            canonical_link,
            []
        ).append(row)

    duplicate_groups = 0
    removed_rows = 0
    normalized_rows = 0

    for canonical_link, group in groups.items():
        keeper = group[0]
        keeper_id = keeper["id"]

        owner_tg_id = keeper["owner_tg_id"]

        if owner_tg_id is None:
            owner_tg_id = next(
                (
                    row["owner_tg_id"]
                    for row in group
                    if row["owner_tg_id"] is not None
                ),
                None
            )

        ownership_statuses = {
            str(row["ownership_status"] or "").lower()
            for row in group
        }

        if "verified" in ownership_statuses:
            ownership_status = "verified"
        else:
            ownership_status = (
                keeper["ownership_status"]
                or _first_nonempty(group, "ownership_status")
                or "unverified"
            )

        moderation_statuses = {
            str(row["moderation_status"] or "").lower()
            for row in group
        }

        if "approved" in moderation_statuses:
            moderation_status = "approved"
        elif "pending" in moderation_statuses:
            moderation_status = "pending"
        elif "rejected" in moderation_statuses:
            moderation_status = "rejected"
        else:
            moderation_status = (
                keeper["moderation_status"]
                or "pending"
            )

        is_official = max(
            int(row["is_official"] or 0)
            for row in group
        )

        telegram_verified = max(
            int(row["telegram_verified"] or 0)
            for row in group
        )

        is_seed = max(
            int(row["is_seed"] or 0)
            for row in group
        )

        title = (
            keeper["title"]
            or _first_nonempty(group, "title")
            or canonical_link
        )

        category = (
            keeper["category"]
            or _first_nonempty(group, "category")
        )

        description = (
            keeper["description"]
            or _first_nonempty(group, "description")
        )

        website = (
            keeper["website"]
            or _first_nonempty(group, "website")
            or ""
        )

        source_url = _first_nonempty(
            group,
            "source_url"
        )

        topic = _first_nonempty(
            group,
            "topic"
        )

        community_type = _first_nonempty(
            group,
            "community_type"
        )

        created_values = [
            str(row["created_at"])
            for row in group
            if row["created_at"]
        ]

        created_at = (
            min(created_values)
            if created_values
            else now_iso()
        )

        duplicate_ids = [
            row["id"]
            for row in group[1:]
        ]

        if duplicate_ids:
            duplicate_groups += 1
            removed_rows += len(duplicate_ids)

            placeholders = ", ".join(
                "?" for _ in duplicate_ids
            )

            reports_exists = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name='reports'
                """
            ).fetchone()

            if reports_exists:
                report_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(reports)"
                    ).fetchall()
                }

                if "project_id" in report_columns:
                    conn.execute(
                        f"""
                        UPDATE reports
                        SET project_id=?
                        WHERE project_id IN ({placeholders})
                        """,
                        [keeper_id, *duplicate_ids]
                    )

            log_exists = conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type='table'
                  AND name='moderation_log'
                """
            ).fetchone()

            if log_exists:
                log_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(moderation_log)"
                    ).fetchall()
                }

                if "project_id" in log_columns:
                    conn.execute(
                        f"""
                        UPDATE moderation_log
                        SET project_id=?
                        WHERE project_id IN ({placeholders})
                        """,
                        [keeper_id, *duplicate_ids]
                    )

            conn.execute(
                f"""
                DELETE FROM projects
                WHERE id IN ({placeholders})
                """,
                duplicate_ids
            )

        old_link = str(keeper["link"] or "")

        if old_link != canonical_link:
            normalized_rows += 1

        conn.execute(
            """
            UPDATE projects
            SET
                owner_tg_id=?,
                title=?,
                link=?,
                category=?,
                description=?,
                website=?,
                ownership_status=?,
                moderation_status=?,
                created_at=?,
                is_official=?,
                telegram_verified=?,
                source_url=?,
                is_seed=?,
                topic=?,
                community_type=?
            WHERE id=?
            """,
            (
                owner_tg_id,
                title,
                canonical_link,
                category,
                description,
                website,
                ownership_status,
                moderation_status,
                created_at,
                is_official,
                telegram_verified,
                source_url,
                is_seed,
                topic,
                community_type,
                keeper_id
            )
        )

    if duplicate_groups or normalized_rows:
        logging.warning(
            "Очистка каталога завершена: "
            "групп дублей объединено=%s, "
            "строк удалено=%s, "
            "ссылок нормализовано=%s",
            duplicate_groups,
            removed_rows,
            normalized_rows
        )
    else:
        logging.info(
            "Дубли проектов не обнаружены."
        )


# ============================================================
# СТАРТОВЫЙ КАТАЛОГ
# ============================================================

def seed_catalog(conn):
    added = 0

    for project in SEED_PROJECTS:
        link = project["link"].lower()

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO projects
            (
                owner_tg_id,
                title,
                link,
                category,
                description,
                website,
                ownership_status,
                moderation_status,
                created_at,
                is_official,
                telegram_verified,
                source_url,
                is_seed,
                topic,
                community_type
            )
            VALUES (
                NULL, ?, ?, ?, ?, '',
                'verified', 'approved', ?,
                ?, 0, ?, 1, ?, ?
            )
            """,
            (
                project["title"],
                link,
                project["category"],
                project["description"],
                now_iso(),
                project.get("is_official", 0),
                project.get("source_url"),
                project.get("topic"),
                project.get("community_type")
            )
        )

        if cursor.rowcount > 0:
            added += 1

    if added:
        logging.info(
            "В стартовый каталог добавлено проектов: %s",
            added
        )


async def seed_public_catalog():
    with db_connect() as conn:
        already_imported = conn.execute(
            """
            SELECT COUNT(*)
            FROM projects
            WHERE source_url=? AND is_seed=1
            """,
            (PUBLIC_DATASET_PAGE,)
        ).fetchone()[0]

    if already_imported >= PUBLIC_DATASET_LIMIT:
        logging.info(
            "Публичный каталог уже импортирован: %s ресурсов",
            already_imported
        )
        return

    timeout = aiohttp.ClientTimeout(
        total=90,
        connect=20
    )

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                PUBLIC_DATASET_URL,
                headers={
                    "User-Agent": "LINKCHECK/1.0",
                    "Accept": "text/csv,*/*"
                }
            ) as response:

                if response.status != 200:
                    raise RuntimeError(
                        f"Источник каталога вернул HTTP {response.status}"
                    )

                csv_text = await response.text()

        reader = csv.DictReader(
            io.StringIO(csv_text)
        )

        if not reader.fieldnames:
            raise RuntimeError(
                "В CSV отсутствуют заголовки"
            )

        normalized_headers = {
            name.strip().lower()
            for name in reader.fieldnames
            if name
        }

        if "username" not in normalized_headers:
            raise RuntimeError(
                "В CSV отсутствует обязательная колонка username"
            )

        imported = 0
        skipped = 0

        with db_connect() as conn:
            for item in reader:
                if imported >= PUBLIC_DATASET_LIMIT:
                    break

                item = {
                    str(key).strip().lower(): value
                    for key, value in item.items()
                    if key is not None
                }

                raw_username = str(
                    item.get("username") or ""
                ).strip()

                username = raw_username.lstrip("@").strip()

                if not re.fullmatch(
                    r"[A-Za-z0-9_]{5,32}",
                    username
                ):
                    skipped += 1
                    continue

                link = "@" + username.lower()

                title = str(
                    item.get("title") or ""
                ).strip()

                if not title:
                    title = username

                title = title[:80]

                topic = str(
                    item.get("topic") or ""
                ).strip() or None

                community_type = str(
                    item.get("community_type") or ""
                ).strip() or None

                if topic:
                    description = (
                        f"Публичный Telegram-канал. Тематика: {topic}."
                    )
                else:
                    description = "Публичный Telegram-канал."

                if community_type:
                    description += (
                        f" Тип сообщества: {community_type}."
                    )

                description = description[:1000]

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO projects
                    (
                        owner_tg_id,
                        title,
                        link,
                        category,
                        description,
                        website,
                        ownership_status,
                        moderation_status,
                        created_at,
                        is_official,
                        telegram_verified,
                        source_url,
                        is_seed,
                        topic,
                        community_type
                    )
                    VALUES (
                        NULL, ?, ?, 'channel', ?, '',
                        'unverified', 'approved', ?,
                        0, 0, ?, 1, ?, ?
                    )
                    """,
                    (
                        title,
                        link,
                        description,
                        now_iso(),
                        PUBLIC_DATASET_PAGE,
                        topic,
                        community_type
                    )
                )

                if cursor.rowcount > 0:
                    imported += 1
                else:
                    skipped += 1

        logging.info(
            "Импорт публичного каталога завершён: "
            "добавлено %s, пропущено %s",
            imported,
            skipped
        )

        if imported < PUBLIC_DATASET_LIMIT:
            logging.warning(
                "Импортировано меньше 1500 новых записей. "
                "Добавлено: %s. Возможно, часть username уже была в базе.",
                imported
            )

    except Exception as exc:
        logging.exception(
            "Не удалось загрузить публичный каталог: %s",
            exc
        )


# ============================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ============================================================

def init_db():
    with db_connect() as conn:
        conn.execute(
            "PRAGMA journal_mode = WAL"
        )

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

        ensure_column(
            conn,
            "projects",
            "owner_tg_id",
            "INTEGER"
        )

        ensure_column(
            conn,
            "projects",
            "ownership_status",
            "TEXT NOT NULL DEFAULT 'unverified'"
        )

        ensure_column(
            conn,
            "projects",
            "moderation_status",
            "TEXT NOT NULL DEFAULT 'pending'"
        )

        ensure_column(
            conn,
            "projects",
            "is_official",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "projects",
            "telegram_verified",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "projects",
            "source_url",
            "TEXT"
        )

        ensure_column(
            conn,
            "projects",
            "is_seed",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "projects",
            "topic",
            "TEXT"
        )

        ensure_column(
            conn,
            "projects",
            "community_type",
            "TEXT"
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

        for col, definition in (
            ("admin_tg_id", "INTEGER"),
            ("project_id", "INTEGER"),
            ("action", "TEXT"),
            ("comment", "TEXT"),
            ("created_at", "TEXT")
        ):
            ensure_column(
                conn,
                "moderation_log",
                col,
                definition
            )

        ensure_column(
            conn,
            "users",
            "is_trusted",
            "INTEGER NOT NULL DEFAULT 0"
        )

        ensure_column(
            conn,
            "users",
            "has_requested_access",
            "INTEGER NOT NULL DEFAULT 0"
        )

        migrate_projects_owner_nullable(conn)

        deduplicate_projects(conn)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_link "
            "ON projects(link)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_owner "
            "ON projects(owner_tg_id)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_status "
            "ON projects(moderation_status, ownership_status)"
        )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_projects_catalog "
            "ON projects(moderation_status, is_official, telegram_verified)"
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_link_unique
            ON projects(lower(link))
            """
        )

        seed_catalog(conn)


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

def upsert_user(tg_id: int, username: str | None):
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO users (tg_id, username, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id)
            DO UPDATE SET username = excluded.username
            """,
            (tg_id, username, now_iso())
        )


def validate_telegram_init_data(init_data: str):
    if not init_data or len(init_data) > 16_384:
        return None

    try:
        pairs = parse_qsl(
            init_data,
            keep_blank_values=True,
            strict_parsing=True
        )

        keys = [key for key, _ in pairs]

        if len(keys) != len(set(keys)):
            return None

        data = dict(pairs)

        received_hash = data.pop(
            "hash",
            None
        )

        if not received_hash or not re.fullmatch(
            r"[0-9a-fA-F]{64}",
            received_hash
        ):
            return None

        auth_date = int(
            data.get("auth_date", "0")
        )

        current = int(time.time())

        if (
            auth_date <= 0
            or auth_date > current + 60
            or current - auth_date > INIT_DATA_MAX_AGE
        ):
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(data.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode("utf-8"),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(
            calculated_hash,
            received_hash.lower()
        ):
            return None

        user_raw = data.get("user")

        if not user_raw:
            return None

        user = json.loads(user_raw)
        user_id = user.get("id")

        if not isinstance(user_id, int) or user_id <= 0:
            return None

        return {
            "id": user_id,
            "username": user.get("username")
        }

    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def authenticated_user(request):
    auth = request.headers.get(
        "Authorization",
        ""
    )

    init_data = (
        auth[4:].strip()
        if auth.startswith("tma ")
        else request.headers.get(
            "X-Telegram-Init-Data",
            ""
        )
    )

    return validate_telegram_init_data(
        init_data
    )


def json_error(message, status=400):
    return web.json_response(
        {"error": message},
        status=status
    )


async def read_json(request):
    try:
        data = await request.json()

        if not isinstance(data, dict):
            raise ValueError

        return data

    except (
        json.JSONDecodeError,
        ValueError,
        web.HTTPException
    ):
        raise web.HTTPBadRequest(
            text="Некорректный JSON"
        )


def normalize_link(raw):
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or len(raw) > 300
    ):
        raise ValueError(
            "Укажите корректную ссылку или username"
        )

    value = raw.strip()

    if re.fullmatch(
        r"[A-Za-z0-9_]{5,32}",
        value
    ):
        username = value

    elif value.startswith("@"):
        username = value[1:]

    else:
        candidate = value

        if candidate.startswith((
            "t.me/",
            "www.t.me/",
            "telegram.me/",
            "www.telegram.me/"
        )):
            candidate = "https://" + candidate

        elif not candidate.startswith((
            "http://",
            "https://"
        )):
            raise ValueError(
                "Введите username, @username или ссылку t.me/username"
            )

        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()

        if parsed.scheme not in {"http", "https"} or host not in {
            "t.me",
            "www.t.me",
            "telegram.me",
            "www.telegram.me"
        }:
            raise ValueError(
                "Поддерживаются только username и ссылки t.me"
            )

        if parsed.query or parsed.fragment:
            raise ValueError(
                "Укажите публичную ссылку без параметров"
            )

        parts = [
            part
            for part in parsed.path.strip("/").split("/")
            if part
        ]

        if len(parts) != 1:
            raise ValueError(
                "Укажите публичный username без дополнительных путей"
            )

        username = parts[0]

    username = username.strip().lstrip("@").lower()

    if not re.fullmatch(
        r"[a-z0-9_]{5,32}",
        username
    ):
        raise ValueError(
            "Некорректный Telegram username"
        )

    return "@" + username


def valid_website(value):
    if not value:
        return ""

    if not isinstance(value, str) or len(value) > 300:
        raise ValueError(
            "Некорректный адрес сайта"
        )

    parsed = urlsplit(value.strip())

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(
            "Сайт должен начинаться с https:// или http://"
        )

    return value.strip()


# ============================================================
# ОБЩАЯ ОБРАБОТКА FLOODWAIT
# ============================================================

def is_flood_wait_error(exc):
    name = type(exc).__name__

    return (
        name in {
            "FloodWaitError",
            "FloodTestPhoneWaitError"
        }
        or "FloodWait" in name
        or bool(
            re.search(
                r"FLOOD_WAIT_\d+",
                str(exc),
                re.IGNORECASE
            )
        )
    )


def get_flood_wait_seconds(exc):
    seconds = getattr(
        exc,
        "seconds",
        None
    )

    if seconds is None:
        seconds = getattr(
            exc,
            "value",
            None
        )

    if seconds is not None:
        try:
            return max(1, int(seconds))
        except (TypeError, ValueError):
            pass

    match = re.search(
        r"FLOOD_WAIT_(\d+)",
        str(exc),
        re.IGNORECASE
    )

    if match:
        return max(1, int(match.group(1)))

    return 60


def register_telegram_flood_wait(exc, source="Telegram"):
    global telegram_flood_wait_until

    if not is_flood_wait_error(exc):
        return 0

    seconds = get_flood_wait_seconds(exc)
    new_until = time.monotonic() + seconds

    telegram_flood_wait_until = max(
        telegram_flood_wait_until,
        new_until
    )

    logging.warning(
        "%s FloodWait: %s сек. Все новые MTProto-запросы "
        "будут приостановлены.",
        source,
        seconds
    )

    return seconds


def telegram_flood_remaining():
    return max(
        0,
        telegram_flood_wait_until - time.monotonic()
    )


# ============================================================
# КЭШ РЕЗУЛЬТАТОВ ПРОВЕРКИ
# ============================================================

def telegram_unavailable_result(reason=None, retry_after=0):
    retry_after = max(
        0,
        int(retry_after or 0)
    )

    if not reason:
        reason = (
            "Telegram временно недоступен. "
            "Не удалось получить достоверный результат."
        )

    return {
        "status": "unavailable",
        "verified": False,
        "member_count": None,
        "reason": reason,
        "retry_after": retry_after,
        "verification_source": None
    }


def is_telegram_not_found_error(exc):
    name = type(exc).__name__

    if name in {
        "UsernameNotOccupiedError",
        "UsernameInvalidError",
        "UsernamePurchaseAvailableError"
    }:
        return True

    error_text = str(exc).upper()

    if (
        "USERNAME_NOT_OCCUPIED" in error_text
        or "USERNAME_INVALID" in error_text
        or "USERNAME_PURCHASE_AVAILABLE" in error_text
    ):
        return True

    if name == "ValueError":
        message = str(exc).lower()

        not_found_phrases = (
            "no user has",
            "no entity corresponds",
            "cannot find any entity",
            "username not found",
            "could not find the input entity"
        )

        return any(
            phrase in message
            for phrase in not_found_phrases
        )

    return False


def cache_telegram_check(key, result, ttl):
    telegram_check_cache[key] = {
        "result": dict(result),
        "expires_at": time.monotonic() + ttl
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
        cache_telegram_check(
            key,
            result,
            TELEGRAM_CHECK_CACHE_TTL
        )

    elif status == "not_found":
        cache_telegram_check(
            key,
            result,
            TELEGRAM_NOT_FOUND_CACHE_TTL
        )

    elif status == "wrong_type":
        cache_telegram_check(
            key,
            result,
            TELEGRAM_CHECK_CACHE_TTL
        )

    elif status == "unavailable":
        cache_telegram_check(
            key,
            result,
            TELEGRAM_UNAVAILABLE_CACHE_TTL
        )


# ============================================================
# ПОЛУЧЕНИЕ РЕАЛЬНОГО КОЛИЧЕСТВА УЧАСТНИКОВ
# ============================================================

async def get_telegram_member_count(entity, entity_type):
    """
    Возвращает количество участников/подписчиков,
    полученное непосредственно из Telegram.

    Если Telegram не предоставил число, возвращается None.
    Никакие приблизительные значения не подставляются.
    """

    if entity_type not in {"channel", "group"}:
        return None

    # Сначала используем значение, которое Telegram уже вернул
    # в результате contacts.resolveUsername.
    count = getattr(
        entity,
        "participants_count",
        None
    )

    if count is not None:
        try:
            count = int(count)

            if count >= 0:
                return count

        except (TypeError, ValueError):
            pass

    if not tg_ready or tg_client is None or functions is None:
        return None

    try:
        if Channel is not None and isinstance(entity, Channel):
            full = await tg_client(
                functions.channels.GetFullChannelRequest(
                    channel=entity
                )
            )

            full_chat = getattr(
                full,
                "full_chat",
                None
            )

            count = getattr(
                full_chat,
                "participants_count",
                None
            )

        elif Chat is not None and isinstance(entity, Chat):
            full = await tg_client(
                functions.messages.GetFullChatRequest(
                    chat_id=entity.id
                )
            )

            full_chat = getattr(
                full,
                "full_chat",
                None
            )

            count = getattr(
                full_chat,
                "participants_count",
                None
            )

        else:
            return None

        if count is None:
            return None

        count = int(count)

        return count if count >= 0 else None

    except Exception as exc:
        if is_flood_wait_error(exc):
            register_telegram_flood_wait(
                exc,
                "Telegram member count"
            )

        else:
            logging.warning(
                "Не удалось получить количество участников: %s: %s",
                type(exc).__name__,
                exc
            )

        return None


# ============================================================
# ОПРЕДЕЛЕНИЕ TELEGRAM ENTITY ПО ОТВЕТУ RESOLVE USERNAME
# ============================================================

def extract_resolved_telegram_entity(resolved):
    """
    contacts.resolveUsername возвращает:
    - peer — PeerUser / PeerChat / PeerChannel;
    - users — соответствующие User;
    - chats — соответствующие Chat или Channel.

    Сущность выбирается по ID из самого ответа Telegram.
    """

    peer = getattr(
        resolved,
        "peer",
        None
    )

    if peer is None:
        return None

    user_id = getattr(
        peer,
        "user_id",
        None
    )

    if user_id is not None:
        return next(
            (
                entity
                for entity in (getattr(resolved, "users", None) or [])
                if getattr(entity, "id", None) == user_id
            ),
            None
        )

    channel_id = getattr(
        peer,
        "channel_id",
        None
    )

    if channel_id is not None:
        return next(
            (
                entity
                for entity in (getattr(resolved, "chats", None) or [])
                if getattr(entity, "id", None) == channel_id
            ),
            None
        )

    chat_id = getattr(
        peer,
        "chat_id",
        None
    )

    if chat_id is not None:
        return next(
            (
                entity
                for entity in (getattr(resolved, "chats", None) or [])
                if getattr(entity, "id", None) == chat_id
            ),
            None
        )

    return None


# ============================================================
# ОПРЕДЕЛЕНИЕ ТИПА TELEGRAM-РЕСУРСА
# ============================================================

def detect_telegram_entity_type(entity):
    """
    Тип определяется по объекту, который вернул Telegram API.

    Возможные результаты:
    bot
    user
    group
    channel
    unknown
    """

    if User is not None and isinstance(entity, User):
        if bool(getattr(entity, "bot", False)):
            return "bot"

        return "user"

    if Channel is not None and isinstance(entity, Channel):
        if (
            bool(getattr(entity, "megagroup", False))
            or bool(getattr(entity, "gigagroup", False))
        ):
            return "group"

        if bool(getattr(entity, "broadcast", False)):
            return "channel"

        return "unknown"

    if Chat is not None and isinstance(entity, Chat):
        return "group"

    return "unknown"


# ============================================================
# ПРОВЕРКА TELEGRAM ЧЕРЕЗ MTProto
# ============================================================

async def check_telegram_resource(link, expected_category="other"):
    """
    Проверяет username непосредственно через Telegram MTProto API.

    ВАЖНО:
    - HTTP 200 от t.me не считается подтверждением существования.
    - Публичная HTML-страница не используется как источник статуса.
    - contacts.resolveUsername выполняет реальное разрешение username.
    - USERNAME_NOT_OCCUPIED означает, что username не занят.
    - FloodWait возвращает unavailable.
    - Счётчики берутся только из ответов Telegram.

    Статусы:
    exists      — Telegram вернул существующий entity;
    not_found   — Telegram вернул USERNAME_NOT_OCCUPIED / USERNAME_INVALID;
    wrong_type  — ресурс найден, но категория не совпадает;
    unavailable — достоверно проверить не удалось.
    """

    global telegram_last_check_at

    username = str(
        link or ""
    ).strip().lstrip("@").lower()

    if not username:
        return {
            "status": "not_found",
            "type": None,
            "verified": False,
            "member_count": None,
            "reason": "Не указан Telegram username.",
            "retry_after": 0,
            "verification_source": None
        }

    if not re.fullmatch(
        r"[a-z0-9_]{5,32}",
        username
    ):
        return {
            "status": "not_found",
            "type": None,
            "verified": False,
            "member_count": None,
            "reason": "Некорректный Telegram username.",
            "retry_after": 0,
            "verification_source": None
        }

    cache_key = (
        username,
        expected_category
    )

    cached = get_cached_telegram_check(
        cache_key
    )

    if cached is not None:
        return cached

    async with telegram_check_lock:
        # Повторная проверка кэша после получения блокировки.
        cached = get_cached_telegram_check(
            cache_key
        )

        if cached is not None:
            return cached

        # Telegram-клиент должен быть подключён.
        if (
            not tg_ready
            or tg_client is None
            or functions is None
        ):
            result = telegram_unavailable_result(
                "Проверка Telegram сейчас недоступна."
            )

            cache_result_by_status(
                cache_key,
                result
            )

            return result

        # Не отправляем новые запросы во время FloodWait.
        remaining = telegram_flood_remaining()

        if remaining > 0:
            result = telegram_unavailable_result(
                "Telegram временно ограничил проверки.",
                math.ceil(remaining)
            )

            cache_result_by_status(
                cache_key,
                result
            )

            return result

        # Ограничение частоты запросов.
        elapsed = (
            time.monotonic()
            - telegram_last_check_at
        )

        if elapsed < TELEGRAM_CHECK_MIN_INTERVAL:
            await asyncio.sleep(
                TELEGRAM_CHECK_MIN_INTERVAL - elapsed
            )

        telegram_last_check_at = time.monotonic()

        try:
            # Главный запрос: реальное разрешение username в Telegram.
            resolved = await tg_client(
                functions.contacts.ResolveUsernameRequest(
                    username=username
                )
            )

            # Получаем entity именно из ответа resolveUsername.
            entity = extract_resolved_telegram_entity(
                resolved
            )

            if entity is None:
                result = telegram_unavailable_result(
                    "Telegram вернул неполный ответ при проверке username."
                )

                cache_result_by_status(
                    cache_key,
                    result
                )

                return result

            # Определяем тип по реальной структуре entity.
            entity_type = detect_telegram_entity_type(
                entity
            )

            # Сравниваем с выбранной категорией.
            category_ok = (
                expected_category == "other"
                or expected_category == entity_type
            )

            if not category_ok:
                result = {
                    "status": "wrong_type",
                    "type": entity_type,
                    "verified": bool(
                        getattr(entity, "verified", False)
                    ),
                    "member_count": None,
                    "reason": (
                        "Username существует в Telegram, "
                        "но найденный тип ресурса не соответствует "
                        f"категории «{expected_category}»."
                    ),
                    "retry_after": 0,
                    "verification_source": "mtproto"
                }

                cache_result_by_status(
                    cache_key,
                    result
                )

                return result

            # Получаем реальное количество участников/подписчиков.
            # Если Telegram не предоставит число, останется None.
            member_count = await get_telegram_member_count(
                entity,
                entity_type
            )

            result = {
                "status": "exists",
                "type": entity_type,
                "verified": bool(
                    getattr(entity, "verified", False)
                ),
                "member_count": member_count,
                "reason": "",
                "retry_after": 0,
                "verification_source": "mtproto"
            }

            cache_result_by_status(
                cache_key,
                result
            )

            return result

        except Exception as exc:
            if is_flood_wait_error(exc):
                seconds = register_telegram_flood_wait(
                    exc,
                    "Telegram username resolve"
                )

                result = telegram_unavailable_result(
                    "Telegram временно ограничил проверки.",
                    seconds
                )

                cache_result_by_status(
                    cache_key,
                    result
                )

                return result

            if is_telegram_not_found_error(exc):
                result = {
                    "status": "not_found",
                    "type": None,
                    "verified": False,
                    "member_count": None,
                    "reason": (
                        "Такой публичный username не найден в Telegram."
                    ),
                    "retry_after": 0,
                    "verification_source": "mtproto"
                }

                cache_result_by_status(
                    cache_key,
                    result
                )

                return result

            logging.warning(
                "Telegram username check failed for @%s: %s: %s",
                username,
                type(exc).__name__,
                exc
            )

            result = telegram_unavailable_result(
                "Не удалось получить достоверный ответ от Telegram."
            )

            cache_result_by_status(
                cache_key,
                result
            )

            return result


# ============================================================
# ПРОВЕРКА СТАРТОВЫХ РЕСУРСОВ
# ============================================================

async def verify_seed_projects():
    """
    Функция сохранена для отдельного ручного запуска.

    Автоматически при старте проекта больше не запускается:
    массовые MTProto-запросы создавали FloodWait.
    """

    if not tg_ready or tg_client is None:
        logging.warning(
            "Проверка стартовых проектов пропущена: MTProto недоступен."
        )
        return

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id, link, category
            FROM projects
            WHERE is_seed=1
              AND COALESCE(source_url, '') != ?
            """,
            (PUBLIC_DATASET_PAGE,)
        ).fetchall()

    total = len(rows)
    checked = 0

    for row in rows:
        try:
            result = await check_telegram_resource(
                row["link"],
                row["category"]
            )

            if result["status"] == "exists":
                with db_connect() as conn:
                    conn.execute(
                        """
                        UPDATE projects
                        SET telegram_verified=?
                        WHERE id=? AND is_seed=1
                        """,
                        (
                            1 if result.get("verified") else 0,
                            row["id"]
                        )
                    )

                checked += 1

            elif result["status"] == "not_found":
                logging.warning(
                    "Seed resource not found: %s",
                    row["link"]
                )

            elif result["status"] == "unavailable":
                logging.warning(
                    "Seed verification остановлена: Telegram временно "
                    "недоступен. Проверено %s из %s.",
                    checked,
                    total
                )
                break

            await asyncio.sleep(2)

        except Exception:
            logging.exception(
                "Seed verification failed for %s",
                row["link"]
            )

    logging.info(
        "Проверка стартового каталога завершена: "
        "успешно проверено %s из %s",
        checked,
        total
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

    return (
        type(status).__name__ == "EmojiStatusCollectible"
    )


async def get_founder_entity():
    global founder_entity_cache

    if (
        not tg_ready
        or tg_client is None
        or functions is None
    ):
        if (
            founder_entity_cache is not None
            and getattr(founder_entity_cache, "id", None) == FOUNDER_ID
        ):
            return founder_entity_cache

        raise RuntimeError(
            "Telegram client is not ready"
        )

    remaining = telegram_flood_remaining()

    if remaining > 0:
        if (
            founder_entity_cache is not None
            and getattr(founder_entity_cache, "id", None) == FOUNDER_ID
        ):
            return founder_entity_cache

        raise RuntimeError(
            f"Telegram FloodWait active: {math.ceil(remaining)} seconds"
        )

    try:
        entity = await tg_client.get_entity(
            FOUNDER_FALLBACK_USERNAME
        )

        if getattr(entity, "id", None) != FOUNDER_ID:
            raise RuntimeError(
                "Username не соответствует ID создателя"
            )

        original_entity = entity

        original_status = getattr(
            original_entity,
            "emoji_status",
            None
        )

        selected_entity = original_entity

        try:
            full = await tg_client(
                functions.users.GetFullUserRequest(id=entity)
            )

            fresh_users = getattr(
                full,
                "users",
                None
            ) or []

            fresh_entity = next(
                (
                    user
                    for user in fresh_users
                    if getattr(user, "id", None) == FOUNDER_ID
                ),
                None
            )

            if fresh_entity is not None:
                fresh_status = getattr(
                    fresh_entity,
                    "emoji_status",
                    None
                )

                if is_collectible_status(original_status):
                    selected_entity = original_entity

                elif is_collectible_status(fresh_status):
                    selected_entity = fresh_entity

                elif fresh_status is not None:
                    selected_entity = fresh_entity

                else:
                    selected_entity = original_entity

                logging.info(
                    "Founder profile loaded: id=%s username=%s "
                    "original_status=%s fresh_status=%s selected_status=%s",
                    FOUNDER_ID,
                    getattr(selected_entity, "username", None),
                    type(original_status).__name__,
                    type(fresh_status).__name__,
                    type(
                        getattr(
                            selected_entity,
                            "emoji_status",
                            None
                        )
                    ).__name__
                )

            else:
                logging.warning(
                    "Founder not found in GetFullUserRequest.users"
                )

        except Exception as exc:
            if is_flood_wait_error(exc):
                register_telegram_flood_wait(
                    exc,
                    "Founder full profile"
                )
            else:
                logging.warning(
                    "GetFullUserRequest failed: %s",
                    exc
                )

        founder_entity_cache = selected_entity

        return selected_entity

    except Exception as exc:
        if is_flood_wait_error(exc):
            register_telegram_flood_wait(
                exc,
                "Founder profile refresh"
            )
        else:
            logging.warning(
                "Founder profile refresh failed: %s",
                exc
            )

        if (
            founder_entity_cache is not None
            and getattr(founder_entity_cache, "id", None) == FOUNDER_ID
        ):
            return founder_entity_cache

        raise


def get_collectible_status(entity):
    status = getattr(
        entity,
        "emoji_status",
        None
    )

    if status is None:
        logging.info(
            "Founder has no emoji_status"
        )
        return None

    if not is_collectible_status(status):
        logging.info(
            "Founder status is not collectible: %s",
            type(status).__name__
        )
        return None

    document_id = getattr(
        status,
        "document_id",
        None
    )

    if not document_id:
        logging.warning(
            "Collectible status has no document_id"
        )
        return None

    result = {
        "document_id": int(document_id),
        "title": getattr(status, "title", None),
        "slug": getattr(status, "slug", None)
    }

    logging.info(
        "Founder collectible detected: document_id=%s title=%s slug=%s",
        result["document_id"],
        result["title"],
        result["slug"]
    )

    return result


def build_founder_profile(entity):
    first_name = getattr(
        entity,
        "first_name",
        None
    ) or ""

    last_name = getattr(
        entity,
        "last_name",
        None
    ) or ""

    username = (
        getattr(entity, "username", None)
        or FOUNDER_FALLBACK_USERNAME
    )

    display_name = " ".join(
        part
        for part in (first_name, last_name)
        if part
    ).strip()

    if not display_name:
        display_name = username or str(FOUNDER_ID)

    profile_url = (
        "https://t.me/" + username
        if username
        else "tg://user?id=" + str(FOUNDER_ID)
    )

    collectible = get_collectible_status(
        entity
    )

    return {
        "id": FOUNDER_ID,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name,
        "username": username,
        "profile_url": profile_url,
        "has_collectible": collectible is not None,
        "gift_document_id": (
            collectible["document_id"]
            if collectible
            else None
        ),
        "gift_title": (
            collectible.get("title")
            if collectible
            else None
        )
    }


def build_founder_fallback_profile():
    username = FOUNDER_FALLBACK_USERNAME

    return {
        "id": FOUNDER_ID,
        "first_name": "",
        "last_name": "",
        "display_name": username,
        "username": username,
        "profile_url": "https://t.me/" + username,
        "has_collectible": False,
        "gift_document_id": None,
        "gift_title": None
    }


async def get_founder_profile(force=False):
    global founder_profile_cache
    global founder_profile_cache_time

    async with founder_profile_lock:
        cache_age = (
            time.time() - founder_profile_cache_time
            if founder_profile_cache is not None
            else None
        )

        remaining = telegram_flood_remaining()

        if (
            founder_profile_cache is not None
            and (
                not force
                and cache_age < FOUNDER_PROFILE_CACHE_TTL
                or remaining > 0
            )
        ):
            return founder_profile_cache

        if remaining > 0:
            if founder_profile_cache is not None:
                return founder_profile_cache

            if (
                founder_entity_cache is not None
                and getattr(founder_entity_cache, "id", None) == FOUNDER_ID
            ):
                result = build_founder_profile(
                    founder_entity_cache
                )
            else:
                result = build_founder_fallback_profile()

            founder_profile_cache = result
            founder_profile_cache_time = time.time()

            return result

        try:
            entity = await get_founder_entity()

            result = build_founder_profile(
                entity
            )

            founder_profile_cache = result
            founder_profile_cache_time = time.time()

            logging.info(
                "Founder API profile: username=%s has_collectible=%s "
                "gift_document_id=%s",
                result["username"],
                result["has_collectible"],
                result["gift_document_id"]
            )

            return result

        except Exception as exc:
            if is_flood_wait_error(exc):
                register_telegram_flood_wait(
                    exc,
                    "Founder API profile"
                )

            logging.warning(
                "Founder API profile could not be refreshed: %s",
                exc
            )

            if founder_profile_cache is not None:
                return founder_profile_cache

            if (
                founder_entity_cache is not None
                and getattr(founder_entity_cache, "id", None) == FOUNDER_ID
            ):
                result = build_founder_profile(
                    founder_entity_cache
                )
            else:
                result = build_founder_fallback_profile()

            founder_profile_cache = result
            founder_profile_cache_time = time.time()

            return result


async def get_founder_gift_animation(document_id):
    if not document_id:
        raise FileNotFoundError(
            "Коллекционный документ не указан"
        )

    document_id = int(document_id)

    cached = founder_gift_cache.get(
        document_id
    )

    if (
        cached
        and time.time() - cached["time"] < FOUNDER_GIFT_CACHE_TTL
    ):
        return cached["data"]

    async with founder_gift_lock:
        cached = founder_gift_cache.get(
            document_id
        )

        if (
            cached
            and time.time() - cached["time"] < FOUNDER_GIFT_CACHE_TTL
        ):
            return cached["data"]

        base_dir = os.path.dirname(
            os.path.abspath(__file__)
        )

        gift_dir = os.path.join(
            base_dir,
            "founder_gifts"
        )

        os.makedirs(
            gift_dir,
            exist_ok=True
        )

        animation_path = os.path.join(
            gift_dir,
            f"{document_id}.json"
        )

        temp_path = animation_path + ".tmp"

        if os.path.exists(animation_path):
            try:
                with open(
                    animation_path,
                    "r",
                    encoding="utf-8"
                ) as file:
                    animation_data = json.load(file)

                founder_gift_cache[document_id] = {
                    "data": animation_data,
                    "time": time.time()
                }

                return animation_data

            except Exception as exc:
                logging.warning(
                    "Founder gift disk cache is invalid: %s",
                    exc
                )

        remaining = telegram_flood_remaining()

        if remaining > 0:
            raise RuntimeError(
                f"Telegram FloodWait active: {math.ceil(remaining)} seconds"
            )

        if (
            not tg_ready
            or tg_client is None
            or functions is None
        ):
            raise RuntimeError(
                "Telegram client is not ready"
            )

        try:
            documents = await tg_client(
                functions.messages.GetCustomEmojiDocumentsRequest(
                    document_id=[document_id]
                )
            )

            document = next(
                (
                    item
                    for item in documents
                    if getattr(item, "id", None) == document_id
                ),
                None
            )

            if document is None:
                raise FileNotFoundError(
                    "Коллекционный документ не найден"
                )

            downloaded_path = await tg_client.download_media(
                document,
                file=temp_path
            )

            actual_path = (
                downloaded_path
                if isinstance(downloaded_path, str)
                else temp_path
            )

            if (
                not actual_path
                or not os.path.exists(actual_path)
            ):
                raise RuntimeError(
                    "Не удалось скачать анимацию подарка"
                )

            with open(
                actual_path,
                "rb"
            ) as file:
                raw_data = file.read()

            decoded = (
                gzip.decompress(raw_data)
                if raw_data[:2] == b"\x1f\x8b"
                else raw_data
            )

            animation_data = json.loads(
                decoded.decode("utf-8")
            )

            with open(
                animation_path,
                "w",
                encoding="utf-8"
            ) as file:
                json.dump(
                    animation_data,
                    file,
                    ensure_ascii=False,
                    separators=(",", ":")
                )

            try:
                os.remove(actual_path)
            except OSError:
                pass

            founder_gift_cache[document_id] = {
                "data": animation_data,
                "time": time.time()
            }

            return animation_data

        except Exception as exc:
            if is_flood_wait_error(exc):
                register_telegram_flood_wait(
                    exc,
                    "Founder gift animation"
                )

            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            raise


async def api_founder(request):
    try:
        profile = await get_founder_profile()

        return web.json_response(
            profile,
            headers={
                "Cache-Control": "public, max-age=60"
            }
        )

    except Exception as exc:
        logging.warning(
            "Founder profile unavailable: %s",
            exc
        )

        return web.json_response(
            build_founder_fallback_profile(),
            headers={
                "Cache-Control": "public, max-age=60"
            }
        )


async def api_founder_gift(request):
    try:
        profile = await get_founder_profile()

        if not profile.get("has_collectible"):
            return web.json_response(
                {"error": "No active collectible status"},
                status=404
            )

        document_id = profile.get(
            "gift_document_id"
        )

        if not document_id:
            return web.json_response(
                {"error": "Gift document unavailable"},
                status=404
            )

        animation = await get_founder_gift_animation(
            document_id
        )

        return web.json_response(
            animation,
            headers={
                "Cache-Control": "public, max-age=3600"
            }
        )

    except FileNotFoundError as exc:
        logging.warning(
            "Founder gift not found: %s",
            exc
        )

        return web.json_response(
            {"error": "Gift animation not found"},
            status=404
        )

    except Exception as exc:
        logging.warning(
            "Founder gift animation unavailable: %s",
            exc
        )

        return web.json_response(
            {"error": "Gift animation temporarily unavailable"},
            status=503
        )


async def api_founder_avatar(request):
    avatar_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "founder_nifort.jpg"
    )

    temp_path = avatar_path + ".tmp.jpg"

    avatar_exists = os.path.exists(
        avatar_path
    )

    avatar_age = (
        time.time() - os.path.getmtime(avatar_path)
        if avatar_exists
        else None
    )

    if (
        avatar_exists
        and avatar_age < FOUNDER_AVATAR_CACHE_TTL
    ):
        return web.FileResponse(
            avatar_path,
            headers={
                "Cache-Control": "public, max-age=300"
            }
        )

    remaining = telegram_flood_remaining()

    if remaining > 0:
        if avatar_exists:
            return web.FileResponse(
                avatar_path,
                headers={
                    "Cache-Control": "public, max-age=300"
                }
            )

        return web.Response(
            status=503,
            text="Avatar temporarily unavailable",
            headers={
                "Retry-After": str(math.ceil(remaining))
            }
        )

    if not tg_ready or tg_client is None:
        if avatar_exists:
            return web.FileResponse(
                avatar_path,
                headers={
                    "Cache-Control": "public, max-age=300"
                }
            )

        return web.Response(
            status=503,
            text="Telegram profile unavailable"
        )

    try:
        entity = await get_founder_entity()

        downloaded = await tg_client.download_profile_photo(
            entity,
            file=temp_path
        )

        if downloaded and os.path.exists(temp_path):
            os.replace(
                temp_path,
                avatar_path
            )

        elif os.path.exists(temp_path):
            os.remove(temp_path)

        if not os.path.exists(avatar_path):
            return web.Response(
                status=404,
                text="Profile photo not found"
            )

        return web.FileResponse(
            avatar_path,
            headers={
                "Cache-Control": "public, max-age=300"
            }
        )

    except Exception as exc:
        if is_flood_wait_error(exc):
            register_telegram_flood_wait(
                exc,
                "Founder avatar"
            )

        logging.warning(
            "Founder avatar unavailable: %s",
            exc
        )

        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        if os.path.exists(avatar_path):
            return web.FileResponse(
                avatar_path,
                headers={
                    "Cache-Control": "public, max-age=300"
                }
            )

        remaining = telegram_flood_remaining()

        return web.Response(
            status=503,
            text="Avatar temporarily unavailable",
            headers={
                "Retry-After": str(math.ceil(remaining))
            } if remaining > 0 else {}
        )


# ============================================================
# ПУБЛИЧНОЕ API
# ============================================================

async def api_stats(request):
    with db_connect() as conn:
        users_count = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        projects_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM projects
            WHERE moderation_status='approved'
            """
        ).fetchone()[0]

    return web.json_response({
        "users": users_count,
        "projects": projects_count
    })


async def api_catalog(request):
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                title,
                link,
                category,
                description,
                moderation_status,
                ownership_status,
                is_official,
                telegram_verified,
                source_url,
                is_seed,
                topic,
                community_type
            FROM projects
            WHERE moderation_status='approved'
            ORDER BY
                CASE
                    WHEN is_official=1
                         OR ownership_status='verified'
                    THEN 0
                    WHEN telegram_verified=1
                    THEN 1
                    ELSE 2
                END ASC,
                is_seed DESC,
                title COLLATE NOCASE ASC
            """
        ).fetchall()

    return web.json_response([
        {
            **dict(row),
            "is_official": bool(row["is_official"]),
            "telegram_verified": bool(row["telegram_verified"]),
            "is_seed": bool(row["is_seed"])
        }
        for row in rows
    ])


# ============================================================
# ПРОВЕРКА НА ГЛАВНОЙ
# ============================================================

async def api_check(request):
    """
    Сначала ищет ресурс в локальной базе LINKCHECK.

    Если ресурс найден:
    - Telethon не вызывается;
    - результат берётся из SQLite.

    Если ресурса нет:
    - username разрешается непосредственно через Telegram API;
    - HTTP 200 от t.me не используется;
    - тип и счётчик берутся из ответа Telegram.
    """

    try:
        data = await read_json(request)

        link = normalize_link(
            data.get("link", "")
        )

    except (ValueError, web.HTTPBadRequest) as exc:
        return json_error(
            str(exc) or "Некорректный запрос"
        )

    # 1. Локальный поиск.
    with db_connect() as conn:
        project = conn.execute(
            """
            SELECT
                id,
                title,
                moderation_status,
                category,
                ownership_status,
                is_official,
                telegram_verified,
                source_url,
                topic,
                community_type
            FROM projects
            WHERE lower(link)=lower(?)
            LIMIT 1
            """,
            (link,)
        ).fetchone()

    # 2. Если проект найден, Telegram не вызываем.
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
        # 3. Прямая проверка через Telegram MTProto.
        telegram = await check_telegram_resource(
            link,
            "other"
        )

        result_source = telegram.get(
            "verification_source"
        ) or "telegram"

    telegram_status = telegram["status"]

    response = {
        "link": link,

        "telegram_status": telegram_status,

        "telegram_type": telegram.get("type"),

        "telegram_member_count": telegram.get("member_count"),

        "telegram_verified": (
            bool(telegram.get("verified"))
            if telegram_status in {"exists", "catalog"}
            else False
        ),

        "telegram_reason": telegram.get("reason", ""),

        "telegram_retry_after": int(
            telegram.get("retry_after", 0) or 0
        ),

        "telegram_check_available": (
            telegram_status != "unavailable"
        ),

        "telegram_check_performed": (
            telegram_status != "catalog"
        ),

        "telegram_verification_source": telegram.get(
            "verification_source"
        ),

        "result_source": result_source,

        "listed": bool(project),

        "moderation": (
            project["moderation_status"]
            if project else None
        ),

        "title": (
            project["title"]
            if project else None
        ),

        "project_id": (
            project["id"]
            if project else None
        ),

        "ownership_status": (
            project["ownership_status"]
            if project else None
        ),

        "is_official": (
            bool(project["is_official"])
            if project else False
        ),

        "catalog_verified": (
            bool(project["telegram_verified"])
            if project else False
        ),

        "source_url": (
            project["source_url"]
            if project else None
        ),

        "topic": (
            project["topic"]
            if project else None
        ),

        "community_type": (
            project["community_type"]
            if project else None
        )
    }

    return web.json_response(response)


# ============================================================
# АВТОРИЗОВАННОЕ API
# ============================================================

async def api_me(request):
    user = authenticated_user(request)

    if not user:
        return json_error(
            "Telegram-авторизация не прошла проверку. "
            "Открой приложение через Telegram.",
            401
        )

    upsert_user(
        user["id"],
        user.get("username")
    )

    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT is_trusted, has_requested_access
            FROM users
            WHERE tg_id=?
            """,
            (user["id"],)
        ).fetchone()

    return web.json_response({
        "id": user["id"],
        "is_admin": user["id"] in ADMIN_IDS,

        "is_trusted": (
            bool(row["is_trusted"])
            if row else False
        ),

        "has_requested_access": (
            bool(row["has_requested_access"])
            if row else False
        )
    })


async def api_request_access(request):
    user = authenticated_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    upsert_user(
        user["id"],
        user.get("username")
    )

    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET has_requested_access=1
            WHERE tg_id=?
            """,
            (user["id"],)
        )

    username = (
        user.get("username")
        or "без username"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔔 <b>Новый запрос прав</b>\n"
                f"Пользователь: @{username}\n"
                f"ID: <code>{user['id']}</code>"
            )

        except Exception:
            logging.exception(
                "Не удалось уведомить администратора %s",
                admin_id
            )

    return web.json_response({
        "status": "ok"
    })


async def api_add_project(request):
    """
    Ручное добавление проекта.

    Если Telegram временно недоступен, заявка всё равно отправляется
    на модерацию. При этом telegram_verified остаётся равным 0.

    Если ресурс достоверно не найден или имеет неправильный тип,
    заявка отклоняется.
    """

    user = authenticated_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    upsert_user(
        user["id"],
        user.get("username")
    )

    try:
        data = await read_json(request)

        title = data.get("title", "")
        description = data.get("description", "")
        category = data.get("category", "other")

        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title.strip()) > 80
        ):
            return json_error(
                "Название обязательно и должно быть не длиннее 80 символов"
            )

        if (
            not isinstance(description, str)
            or len(description) > 1000
        ):
            return json_error(
                "Описание не должно превышать 1000 символов"
            )

        if (
            not isinstance(category, str)
            or category not in {
                "channel",
                "bot",
                "group",
                "other"
            }
        ):
            return json_error(
                "Некорректная категория"
            )

        link = normalize_link(
            data.get("link", "")
        )

        website = valid_website(
            data.get("website", "")
        )

    except (ValueError, web.HTTPBadRequest) as exc:
        return json_error(
            str(exc) or "Некорректный запрос"
        )

    with db_connect() as conn:
        row = conn.execute(
            """
            SELECT is_trusted
            FROM users
            WHERE tg_id=?
            """,
            (user["id"],)
        ).fetchone()

        if user["id"] not in ADMIN_IDS and (
            not row or not row["is_trusted"]
        ):
            return json_error(
                "У вас пока нет прав на добавление проектов.",
                403
            )

        duplicate = conn.execute(
            """
            SELECT id
            FROM projects
            WHERE lower(link)=lower(?)
            """,
            (link,)
        ).fetchone()

        if duplicate:
            return json_error(
                "Этот ресурс уже есть в базе"
            )

    telegram = await check_telegram_resource(
        link,
        category
    )

    telegram_status = telegram["status"]

    if telegram_status == "not_found":
        return json_error(
            "Ресурс не найден в Telegram. Проверьте username.",
            400
        )

    if telegram_status == "wrong_type":
        return json_error(
            telegram["reason"],
            400
        )

    telegram_verified = (
        1
        if (
            telegram_status == "exists"
            and telegram.get("verified")
        )
        else 0
    )

    with db_connect() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO projects
                (
                    owner_tg_id,
                    title,
                    link,
                    category,
                    description,
                    website,
                    ownership_status,
                    moderation_status,
                    created_at,
                    is_official,
                    telegram_verified,
                    source_url,
                    is_seed,
                    topic,
                    community_type
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?,
                    'unverified',
                    'pending',
                    ?,
                    0,
                    ?,
                    NULL,
                    0,
                    NULL,
                    NULL
                )
                """,
                (
                    user["id"],
                    title.strip(),
                    link,
                    category,
                    description.strip(),
                    website,
                    now_iso(),
                    telegram_verified
                )
            )

        except sqlite3.IntegrityError:
            return json_error(
                "Этот ресурс уже есть в базе"
            )

        project_id = cur.lastrowid

    if telegram_status == "exists":
        message = (
            "Telegram-ресурс найден. "
            "Проект отправлен на модерацию."
        )
    else:
        message = (
            "Заявка отправлена на модерацию. "
            "Проверка Telegram временно недоступна. "
            "Telegram-галочка не установлена."
        )

    return web.json_response({
        "status": "ok",
        "project_id": project_id,
        "telegram_status": telegram_status,
        "telegram_verified": bool(telegram_verified),
        "telegram_retry_after": int(
            telegram.get("retry_after", 0) or 0
        ),
        "message": message
    })


async def api_my_projects(request):
    user = authenticated_user(request)

    if not user:
        return json_error(
            "Unauthorized",
            401
        )

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                title,
                link,
                moderation_status,
                ownership_status,
                is_official,
                telegram_verified,
                source_url,
                topic,
                community_type
            FROM projects
            WHERE owner_tg_id=?
            ORDER BY id DESC
            """,
            (user["id"],)
        ).fetchall()

    return web.json_response([
        {
            "id": row["id"],
            "title": row["title"],
            "link": row["link"],
            "moderation": row["moderation_status"],
            "ownership_status": row["ownership_status"],
            "is_official": bool(row["is_official"]),
            "telegram_verified": bool(row["telegram_verified"]),
            "source_url": row["source_url"],
            "topic": row["topic"],
            "community_type": row["community_type"]
        }
        for row in rows
    ])


# ============================================================
# АДМИН API
# ============================================================

def admin_user(request):
    user = authenticated_user(request)

    if not user:
        return None

    return (
        user
        if user["id"] in ADMIN_IDS
        else None
    )


async def api_admin_users(request):
    if not admin_user(request):
        return json_error(
            "Forbidden",
            403
        )

    query = str(
        request.query.get("q", "")
    ).strip()[:100]

    with db_connect() as conn:
        if query:
            pattern = f"%{query.lower()}%"

            rows = conn.execute(
                """
                SELECT
                    tg_id,
                    username,
                    is_trusted,
                    has_requested_access
                FROM users
                WHERE
                    CAST(tg_id AS TEXT) LIKE ?
                    OR lower(COALESCE(username, '')) LIKE ?
                ORDER BY
                    has_requested_access DESC,
                    created_at DESC
                """,
                (
                    f"%{query}%",
                    pattern
                )
            ).fetchall()

        else:
            rows = conn.execute(
                """
                SELECT
                    tg_id,
                    username,
                    is_trusted,
                    has_requested_access
                FROM users
                ORDER BY
                    has_requested_access DESC,
                    created_at DESC
                """
            ).fetchall()

    return web.json_response([
        {
            "id": row["tg_id"],
            "username": row["username"],
            "is_trusted": bool(row["is_trusted"]),
            "has_requested_access": bool(
                row["has_requested_access"]
            )
        }
        for row in rows
    ])


async def api_admin_toggle_trust(request):
    if not admin_user(request):
        return json_error(
            "Forbidden",
            403
        )

    try:
        data = await read_json(request)

        target_id = int(
            data.get("tg_id", 0)
        )

        action = data.get("action")

        if (
            target_id <= 0
            or action not in {"trust", "untrust"}
        ):
            return json_error(
                "Некорректные параметры"
            )

    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error(
            "Некорректный запрос"
        )

    with db_connect() as conn:
        if action == "trust":
            conn.execute(
                """
                UPDATE users
                SET is_trusted=1,
                    has_requested_access=0
                WHERE tg_id=?
                """,
                (target_id,)
            )

        else:
            conn.execute(
                """
                UPDATE users
                SET is_trusted=0
                WHERE tg_id=?
                """,
                (target_id,)
            )

    return web.json_response({
        "status": "ok"
    })


async def api_admin_requests(request):
    if not admin_user(request):
        return json_error(
            "Forbidden",
            403
        )

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                title,
                link,
                category,
                description,
                moderation_status,
                ownership_status,
                is_official,
                telegram_verified,
                source_url,
                topic,
                community_type
            FROM projects
            WHERE moderation_status='pending'
            ORDER BY id ASC
            """
        ).fetchall()

    return web.json_response([
        {
            "id": row["id"],
            "title": row["title"],
            "link": row["link"],
            "category": row["category"],
            "description": row["description"],
            "mod_status": row["moderation_status"],
            "status_text": "Новая заявка на публикацию",
            "ownership_status": row["ownership_status"],
            "is_official": bool(row["is_official"]),
            "telegram_verified": bool(row["telegram_verified"]),
            "source_url": row["source_url"],
            "topic": row["topic"],
            "community_type": row["community_type"]
        }
        for row in rows
    ])


async def api_admin_catalog(request):
    if not admin_user(request):
        return json_error(
            "Forbidden",
            403
        )

    query = str(
        request.query.get("q", "")
    ).strip()[:150]

    with db_connect() as conn:
        if query:
            pattern = f"%{query.lower()}%"

            rows = conn.execute(
                """
                SELECT
                    id,
                    title,
                    link,
                    category,
                    moderation_status,
                    ownership_status,
                    is_official,
                    telegram_verified,
                    source_url,
                    is_seed,
                    topic,
                    community_type
                FROM projects
                WHERE
                    lower(COALESCE(title, '')) LIKE ?
                    OR lower(COALESCE(link, '')) LIKE ?
                    OR lower(COALESCE(category, '')) LIKE ?
                    OR CAST(id AS TEXT) LIKE ?
                ORDER BY
                    CASE
                        WHEN is_official=1
                             OR ownership_status='verified'
                        THEN 0
                        WHEN telegram_verified=1
                        THEN 1
                        ELSE 2
                    END ASC,
                    is_seed DESC,
                    title COLLATE NOCASE ASC
                """,
                (
                    pattern,
                    pattern,
                    pattern,
                    f"%{query}%"
                )
            ).fetchall()

        else:
            rows = conn.execute(
                """
                SELECT
                    id,
                    title,
                    link,
                    category,
                    moderation_status,
                    ownership_status,
                    is_official,
                    telegram_verified,
                    source_url,
                    is_seed,
                    topic,
                    community_type
                FROM projects
                ORDER BY
                    CASE
                        WHEN is_official=1
                             OR ownership_status='verified'
                        THEN 0
                        WHEN telegram_verified=1
                        THEN 1
                        ELSE 2
                    END ASC,
                    is_seed DESC,
                    title COLLATE NOCASE ASC
                """
            ).fetchall()

    return web.json_response([
        {
            "id": row["id"],
            "title": row["title"],
            "link": row["link"],
            "category": row["category"],
            "mod_status": row["moderation_status"],
            "ownership_status": row["ownership_status"],
            "is_official": bool(row["is_official"]),
            "telegram_verified": bool(row["telegram_verified"]),
            "source_url": row["source_url"],
            "is_seed": bool(row["is_seed"]),
            "topic": row["topic"],
            "community_type": row["community_type"]
        }
        for row in rows
    ])


async def api_admin_action(request):
    admin = admin_user(request)

    if not admin:
        return json_error(
            "Нет прав администратора или Telegram-авторизация устарела. "
            "Переоткройте Mini App.",
            403
        )

    try:
        data = await read_json(request)

        project_id = int(
            data.get("project_id", 0)
        )

        action = data.get("action")

        if (
            project_id <= 0
            or action not in {
                "approve",
                "reject",
                "delete"
            }
        ):
            return json_error(
                "Некорректное действие или ID проекта"
            )

    except (ValueError, TypeError, web.HTTPBadRequest):
        return json_error(
            "Некорректный запрос"
        )

    with db_connect() as conn:
        project = conn.execute(
            """
            SELECT id, title
            FROM projects
            WHERE id=?
            """,
            (project_id,)
        ).fetchone()

        if not project:
            return json_error(
                "Проект не найден",
                404
            )

        if action == "approve":
            conn.execute(
                """
                UPDATE projects
                SET moderation_status='approved'
                WHERE id=?
                """,
                (project_id,)
            )

            write_moderation_log(
                conn,
                admin["id"],
                project_id,
                action,
                "Проект одобрен"
            )

        elif action == "reject":
            conn.execute(
                """
                UPDATE projects
                SET moderation_status='rejected'
                WHERE id=?
                """,
                (project_id,)
            )

            write_moderation_log(
                conn,
                admin["id"],
                project_id,
                action,
                "Проект отклонён"
            )

        else:
            conn.execute(
                "DELETE FROM reports WHERE project_id=?",
                (project_id,)
            )

            conn.execute(
                "DELETE FROM projects WHERE id=?",
                (project_id,)
            )

            write_moderation_log(
                conn,
                admin["id"],
                None,
                action,
                f"Удалён проект ID {project_id}: {project['title']}"
            )

    return web.json_response({
        "status": "ok",
        "message": "Действие выполнено"
    })


# ============================================================
# WEB SERVER
# ============================================================

async def handle_index(request):
    return web.FileResponse(
        "miniapp.html",
        headers={"Cache-Control": "no-store"}
    )


@web.middleware
async def security_middleware(request, handler):
    if (
        request.content_length
        and request.content_length > 32_768
    ):
        return json_error(
            "Слишком большой запрос",
            413
        )

    try:
        response = await handler(request)

    except web.HTTPException as exc:
        return json_error(
            exc.text or "Некорректный запрос",
            exc.status
        )

    except Exception:
        logging.exception(
            "Ошибка API: %s %s",
            request.method,
            request.path
        )

        return json_error(
            "Внутренняя ошибка сервера",
            500
        )

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"

    return response


async def start_web_server():
    app = web.Application(
        middlewares=[security_middleware],
        client_max_size=32_768
    )

    app.router.add_get(
        "/",
        handle_index
    )

    app.router.add_get(
        "/api/founder",
        api_founder
    )

    app.router.add_get(
        "/api/founder/avatar",
        api_founder_avatar
    )

    app.router.add_get(
        "/api/founder/gift",
        api_founder_gift
    )

    app.router.add_get(
        "/api/stats",
        api_stats
    )

    app.router.add_get(
        "/api/me",
        api_me
    )

    app.router.add_post(
        "/api/request_access",
        api_request_access
    )

    app.router.add_post(
        "/api/check",
        api_check
    )

    app.router.add_post(
        "/api/add",
        api_add_project
    )

    app.router.add_get(
        "/api/catalog",
        api_catalog
    )

    app.router.add_get(
        "/api/my_projects",
        api_my_projects
    )

    app.router.add_get(
        "/api/admin/users",
        api_admin_users
    )

    app.router.add_post(
        "/api/admin/toggle_trust",
        api_admin_toggle_trust
    )

    app.router.add_get(
        "/api/admin/requests",
        api_admin_requests
    )

    app.router.add_get(
        "/api/admin/catalog",
        api_admin_catalog
    )

    app.router.add_post(
        "/api/admin/action",
        api_admin_action
    )

    app.router.add_get(
        "/favicon.ico",
        lambda request: web.Response(status=204)
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logging.info(
        "Веб-сервер запущен на порту %s",
        PORT
    )

    return runner


# ============================================================
# TELEGRAM CHECKER
# ============================================================

async def start_telegram_checker():
    global tg_client
    global tg_ready
    global tg_start_error

    if not TG_API_ID or not TG_API_HASH:
        tg_start_error = (
            "TG_API_ID или TG_API_HASH не настроены"
        )

        logging.error(
            "%s. Проверка Telegram будет недоступна. "
            "Отправка проектов на модерацию продолжит работать.",
            tg_start_error
        )

        return

    if TelegramClient is None:
        tg_start_error = (
            "Telethon не установлен. Выполните pip install telethon"
        )

        logging.error(
            "%s. Отправка проектов на модерацию продолжит работать.",
            tg_start_error
        )

        return

    try:
        tg_client = TelegramClient(
            TG_SESSION,
            TG_API_ID,
            TG_API_HASH
        )

        await tg_client.start()

        tg_ready = True

        logging.info(
            "Проверка Telegram через MTProto подключена."
        )

    except Exception as exc:
        tg_start_error = (
            f"{type(exc).__name__}: {exc}"
        )

        tg_ready = False

        logging.exception(
            "Не удалось запустить Telegram-проверку. "
            "Отправка проектов на модерацию продолжит работать."
        )


# ============================================================
# MAIN
# ============================================================

async def main():
    init_db()

    await start_telegram_checker()

    await seed_public_catalog()

    runner = await start_web_server()

    # Массовая MTProto-проверка стартовых проектов не запускается.
    # Для индивидуальных запросов используется contacts.resolveUsername.

    logging.info(
        "Автоматическая массовая MTProto-проверка стартовых проектов отключена."
    )

    try:
        logging.info(
            "Запуск LINKCHECK..."
        )

        await dp.start_polling(bot)

    finally:
        await runner.cleanup()

        if tg_client is not None:
            await tg_client.disconnect()

        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):
        logging.info(
            "LINKCHECK остановлен"
        )