"""
Сборщик метрик постов через MTProto (Telethon).
=================================================
Берёт последние N постов каждого канала и сохраняет в SQLite:
  * просмотры (views)
  * пересылки (forwards)
  * реакции: общее число + разбивка по эмодзи (JSON)
  * количество комментариев (replies, если подключена группа обсуждений)

Метрики пишутся СНИМКАМИ (с таймстампом), чтобы видеть динамику во времени.

Работает от ВАШЕГО аккаунта (user-сессия), не от бота.
Для приватных каналов аккаунт должен быть участником канала.

Первый запуск:
  - запросит номер телефона и код подтверждения -> создаст файл сессии (.session)
  - дальше вход не требуется

Запуск:
    python collector_mtproto.py            # разовый сбор
    python collector_mtproto.py --loop     # повтор каждые POLL_MIN минут
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION = os.getenv("SESSION_NAME", "user_session")
CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "").split(",") if c.strip()]
POSTS_LIMIT = int(os.getenv("POSTS_LIMIT", "4"))     # последние N постов
POLL_MIN = int(os.getenv("POLL_MIN", "30"))          # период для --loop
DB_PATH = os.getenv("DB_PATH", "stats.db")

# Прокси (нужен, если Telegram заблокирован в вашей сети).
# PROXY_TYPE: socks5 | socks4 | http   (пусто = без прокси)
PROXY_TYPE = os.getenv("PROXY_TYPE", "").strip().lower()
PROXY_HOST = os.getenv("PROXY_HOST", "").strip()
PROXY_PORT = int(os.getenv("PROXY_PORT", "0") or 0)
PROXY_USER = os.getenv("PROXY_USER", "").strip() or None
PROXY_PASS = os.getenv("PROXY_PASS", "").strip() or None


def build_proxy():
    """Собирает кортеж прокси для Telethon или None."""
    if not PROXY_TYPE or not PROXY_HOST or not PROXY_PORT:
        return None
    if PROXY_USER and PROXY_PASS:
        return (PROXY_TYPE, PROXY_HOST, PROXY_PORT, True, PROXY_USER, PROXY_PASS)
    return (PROXY_TYPE, PROXY_HOST, PROXY_PORT)


def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS post_metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            channel         TEXT    NOT NULL,
            message_id      INTEGER NOT NULL,
            views           INTEGER,
            forwards        INTEGER,
            reactions_total INTEGER,
            reactions_json  TEXT,
            comments        INTEGER,
            taken_at        TEXT    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_msg
            ON post_metrics(channel, message_id);
        """
    )
    con.commit()
    con.close()


def save_metric(row: dict) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO post_metrics
           (channel, message_id, views, forwards, reactions_total,
            reactions_json, comments, taken_at)
           VALUES (:channel, :message_id, :views, :forwards, :reactions_total,
                   :reactions_json, :comments, :taken_at)""",
        row,
    )
    con.commit()
    con.close()


def parse_reactions(msg) -> tuple[int, str]:
    """Возвращает (общее число реакций, JSON-разбивку по эмодзи)."""
    if not getattr(msg, "reactions", None) or not msg.reactions.results:
        return 0, "{}"
    breakdown: dict[str, int] = {}
    for r in msg.reactions.results:
        emoji = getattr(r.reaction, "emoticon", None) \
            or getattr(r.reaction, "document_id", "custom")
        breakdown[str(emoji)] = r.count
    return sum(breakdown.values()), json.dumps(breakdown, ensure_ascii=False)


def parse_comments(msg) -> int | None:
    """Число комментариев (если подключена группа обсуждений)."""
    replies = getattr(msg, "replies", None)
    return replies.replies if replies else None


async def collect_once(client: TelegramClient) -> None:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    for channel in CHANNELS:
        try:
            ch = int(channel) if channel.lstrip("-").isdigit() else channel
            async for msg in client.iter_messages(ch, limit=POSTS_LIMIT):
                if msg.action is not None:      # пропускаем служебные сообщения
                    continue
                total, breakdown = parse_reactions(msg)
                save_metric({
                    "channel": channel,
                    "message_id": msg.id,
                    "views": getattr(msg, "views", None),
                    "forwards": getattr(msg, "forwards", None),
                    "reactions_total": total,
                    "reactions_json": breakdown,
                    "comments": parse_comments(msg),
                    "taken_at": now,
                })
                print(f"  {channel} msg {msg.id}: "
                      f"views={getattr(msg, 'views', None)} "
                      f"reactions={total} comments={parse_comments(msg)}")
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Ошибка по каналу {channel}: {exc}")


async def main() -> None:
    if not API_ID or not API_HASH:
        raise SystemExit("Заполните API_ID и API_HASH в .env (my.telegram.org)")
    if not CHANNELS:
        raise SystemExit("CHANNELS пуст")

    proxy = build_proxy()
    if proxy:
        print(f"Через прокси {PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}")
    client = TelegramClient(SESSION, API_ID, API_HASH, proxy=proxy)
    await client.start()        # при первом запуске спросит телефон + код
    print(f"Авторизован. Каналы: {', '.join(CHANNELS)} | постов: {POSTS_LIMIT}")

    loop = "--loop" in sys.argv
    try:
        while True:
            print(f"--- сбор {datetime.now():%Y-%m-%d %H:%M:%S} ---")
            await collect_once(client)
            if not loop:
                break
            print(f"Следующий сбор через {POLL_MIN} мин.")
            await asyncio.sleep(POLL_MIN * 60)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
