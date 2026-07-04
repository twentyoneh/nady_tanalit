"""
Проверка подключения к Telegram через MTProto (Telethon).
==========================================================
Запускайте ПЕРЕД collector_mtproto.py, чтобы отдельно проверить:
  * верны ли API_ID / API_HASH (в т.ч. полученные у друга),
  * работает ли прокси,
  * проходит ли вход вашим номером,
  * видит ли аккаунт нужные приватные каналы (вы должны быть их участником).

Запуск:
    python test_connect.py
"""

import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION = os.getenv("SESSION_NAME", "user_session")
CHANNELS = [c.strip() for c in os.getenv("CHANNELS", "").split(",") if c.strip()]

PROXY_TYPE = os.getenv("PROXY_TYPE", "").strip().lower()
PROXY_HOST = os.getenv("PROXY_HOST", "").strip()
PROXY_PORT = int(os.getenv("PROXY_PORT", "0") or 0)
PROXY_USER = os.getenv("PROXY_USER", "").strip() or None
PROXY_PASS = os.getenv("PROXY_PASS", "").strip() or None


def build_proxy():
    if not PROXY_TYPE or not PROXY_HOST or not PROXY_PORT:
        return None
    if PROXY_USER and PROXY_PASS:
        return (PROXY_TYPE, PROXY_HOST, PROXY_PORT, True, PROXY_USER, PROXY_PASS)
    return (PROXY_TYPE, PROXY_HOST, PROXY_PORT)


async def main() -> None:
    if not API_ID or not API_HASH:
        raise SystemExit("Заполните API_ID и API_HASH в .env")

    proxy = build_proxy()
    print(f"Прокси: {PROXY_TYPE}://{PROXY_HOST}:{PROXY_PORT}" if proxy
          else "Прокси: не используется")

    client = TelegramClient(SESSION, API_ID, API_HASH, proxy=proxy)
    await client.start()                       # спросит телефон + код (1 раз)

    me = await client.get_me()
    name = (me.first_name or "") + (f" @{me.username}" if me.username else "")
    print(f"\n[OK] Вход выполнен: {name.strip()} (id {me.id})\n")

    if not CHANNELS:
        print("CHANNELS пуст — добавьте каналы в .env")
        await client.disconnect()
        return

    print("Проверяю доступ к каналам:")
    for channel in CHANNELS:
        try:
            ch = int(channel) if channel.lstrip("-").isdigit() else channel
            ent = await client.get_entity(ch)
            title = getattr(ent, "title", str(ch))
            # пробуем прочитать последний пост и его просмотры
            views = None
            async for msg in client.iter_messages(ch, limit=1):
                views = getattr(msg, "views", None)
            print(f"  [OK] {channel} -> «{title}» | просмотры последнего поста: {views}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] {channel}: {exc}")

    await client.disconnect()
    print("\nГотово. Если у каналов виден title и просмотры — можно запускать "
          "collector_mtproto.py")


if __name__ == "__main__":
    asyncio.run(main())
