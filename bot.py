"""
Telegram Channel Stats Bot (Bot API / aiogram)
================================================
Заготовка бота для сбора статистики со СВОИХ каналов (где бот — администратор).

Что собирает:
  * Число подписчиков канала (снимок по расписанию -> динамика во времени)
  * Новые посты канала (channel_post) -> в таблицу posts

Ограничение Bot API:
  Просмотры постов через Bot API недоступны. Для просмотров/реакций нужен MTProto
  (Telethon). Точка расширения помечена ниже как TODO.

Запуск:
  1) pip install -r requirements.txt
  2) скопировать .env.example -> .env и заполнить BOT_TOKEN, CHANNELS
  3) добавить бота администратором в каждый канал
  4) python bot.py
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# CHANNELS: список через запятую. Можно @username или числовой id (-100...).
CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "").split(",") if c.strip()]
# Как часто снимать число подписчиков (минуты)
SNAPSHOT_INTERVAL_MIN = int(os.getenv("SNAPSHOT_INTERVAL_MIN", "60"))
DB_PATH = os.getenv("DB_PATH", "stats.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("channel-stats")


# --------------------------------------------------------------------------- #
# База данных (SQLite)
# --------------------------------------------------------------------------- #
def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel      TEXT    NOT NULL,
            members      INTEGER NOT NULL,
            taken_at     TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS posts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            channel      TEXT    NOT NULL,
            message_id   INTEGER NOT NULL,
            text         TEXT,
            has_media    INTEGER NOT NULL DEFAULT 0,
            posted_at    TEXT    NOT NULL,
            UNIQUE(channel, message_id)
        );
        """
    )
    con.commit()
    con.close()
    log.info("DB готова: %s", DB_PATH)


def save_subscribers(channel: str, members: int) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO subscribers (channel, members, taken_at) VALUES (?, ?, ?)",
        (channel, members, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()


def save_post(channel: str, message_id: int, text: str, has_media: bool,
              posted_at: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT OR IGNORE INTO posts
           (channel, message_id, text, has_media, posted_at)
           VALUES (?, ?, ?, ?, ?)""",
        (channel, message_id, text, int(has_media), posted_at),
    )
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# Бот
# --------------------------------------------------------------------------- #
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.channel_post()
async def on_channel_post(message: Message) -> None:
    """Ловим каждый новый пост в каналах, где бот — админ."""
    channel = message.chat.username or str(message.chat.id)
    text = message.text or message.caption or ""
    has_media = bool(message.photo or message.video or message.document
                     or message.animation)
    posted_at = message.date.astimezone(timezone.utc).isoformat()

    save_post(channel, message.message_id, text, has_media, posted_at)
    log.info("Новый пост в %s (msg_id=%s)", channel, message.message_id)


async def snapshot_subscribers() -> None:
    """Снимок числа подписчиков по всем каналам — запускается по расписанию."""
    for channel in CHANNELS:
        try:
            count = await bot.get_chat_member_count(channel)
            save_subscribers(channel, count)
            log.info("Подписчиков в %s: %s", channel, count)
        except Exception as exc:  # noqa: BLE001
            log.error("Не удалось получить подписчиков %s: %s", channel, exc)


# --------------------------------------------------------------------------- #
# TODO (опционально): просмотры постов через MTProto / Telethon
# --------------------------------------------------------------------------- #
# Bot API не отдаёт просмотры. Чтобы их собирать, добавьте отдельный модуль
# на Telethon:
#   from telethon import TelegramClient
#   client = TelegramClient("session", api_id, api_hash)
#   async for msg in client.iter_messages(channel, limit=50):
#       print(msg.id, msg.views, msg.forwards)
# Запросите api_id / api_hash на https://my.telegram.org
# --------------------------------------------------------------------------- #


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Заполните .env")
    if not CHANNELS:
        log.warning("CHANNELS пуст — снимки подписчиков делаться не будут.")

    init_db()

    # Планировщик снимков подписчиков
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(snapshot_subscribers, "interval",
                      minutes=SNAPSHOT_INTERVAL_MIN, next_run_time=datetime.now())
    scheduler.start()
    log.info("Снимки подписчиков каждые %s мин.", SNAPSHOT_INTERVAL_MIN)

    log.info("Бот запущен. Каналы: %s", ", ".join(CHANNELS) or "(нет)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено.")
