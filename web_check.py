"""
Проверка окружения для веб-скрапера (WebZ /a/, web_scraper.py).
===============================================================
Запускайте ПЕРЕД сбором. По шагам проверяет:
  1) установлен Playwright и браузер Chromium;
  2) работает сеть/VPN и открывается web.telegram.org/a/;
  3) вы залогинены (приложение загрузилось);
  4) канал из CHANNEL_IDS открывается по id.

Запуск:
    python web_check.py
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

WEB_URL = "https://web.telegram.org/a/"
USER_DATA_DIR = os.getenv("USER_DATA_DIR", "tg_web_profile_a")
CHANNEL_IDS = [c.strip() for c in os.getenv("CHANNEL_IDS", "").split(",")
               if c.strip()]
PROXY_SERVER = os.getenv("WEB_PROXY", "").strip()
PROXY_USER = os.getenv("PROXY_USER", "").strip() or None
PROXY_PASS = os.getenv("PROXY_PASS", "").strip() or None

SEL_APP_READY = "#LeftColumn, .chat-list, .MessageList"
SEL_MESSAGE_LIST = ".MessageList"


def _proxy_conf():
    if not PROXY_SERVER:
        return None
    conf = {"server": PROXY_SERVER}
    if PROXY_USER and PROXY_PASS:
        conf.update(username=PROXY_USER, password=PROXY_PASS)
    return conf


def ok(msg):
    print(f"  [OK] {msg}")


def bad(msg):
    print(f"  [!]  {msg}")


async def main() -> None:
    print("== Проверка веб-скрапера (WebZ /a/) ==\n")

    try:
        import playwright.async_api  # noqa: F401
        ok("Playwright установлен")
    except ImportError:
        bad("Playwright не установлен: python -m pip install playwright")
        sys.exit(1)

    from playwright.async_api import async_playwright

    print(f"\nПрокси: {PROXY_SERVER or 'не задан (при VPN это норма)'}")

    async with async_playwright() as pw:
        try:
            ctx = await pw.chromium.launch_persistent_context(
                USER_DATA_DIR, headless=True, proxy=_proxy_conf(),
            )
        except Exception as exc:  # noqa: BLE001
            bad(f"Не удалось запустить Chromium: {exc}")
            bad("Выполните: python -m playwright install chromium")
            sys.exit(1)
        ok("Chromium запущен")

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            await page.goto(WEB_URL, timeout=45_000)
            ok("web.telegram.org/a/ открылся (сеть/VPN работает)")
        except Exception as exc:  # noqa: BLE001
            bad(f"Не открылся web.telegram.org: {exc}")
            bad("Включите VPN или проверьте WEB_PROXY в .env")
            await ctx.close()
            sys.exit(1)

        try:
            await page.wait_for_selector(SEL_APP_READY, timeout=15_000)
            ok("Вы залогинены (приложение загрузилось)")
        except Exception:  # noqa: BLE001
            bad("Приложение не загрузилось — вы НЕ залогинены.")
            bad("Выполните вход: python web_scraper.py --login")
            await ctx.close()
            sys.exit(1)

        print("\nПроверка каналов из CHANNEL_IDS:")
        if not CHANNEL_IDS:
            bad("CHANNEL_IDS пуст — впишите id канала в .env")
        for cid in CHANNEL_IDS:
            try:
                await page.goto(f"{WEB_URL}#{cid}")
                await page.wait_for_selector(SEL_MESSAGE_LIST, timeout=20_000)
                ok(f"{cid}: канал открылся, список сообщений виден")
            except Exception:  # noqa: BLE001
                bad(f"{cid}: не удалось открыть (проверьте id и что вы участник)")

        await ctx.close()
    print("\nЕсли все пункты [OK] — сначала настройте селекторы:\n"
          "  python web_scraper.py --discover\n"
          "затем запускайте сбор:\n"
          "  python web_scraper.py")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
