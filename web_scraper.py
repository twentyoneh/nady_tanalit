"""
Скрапер веб-версии Telegram — клиент WebZ (/a/) — через Playwright.
==================================================================
Обходит my.telegram.org: веб-клиент это MTProto-клиент со СВОИМ api_id,
вы просто логинитесь номером+кодом.

Каналы открываются АВТОМАТИЧЕСКИ через встроенный поиск WebZ по названию,
поэтому можно указать сразу несколько. По последним N постам собирается:
просмотры, число реакций, число комментариев -> в базу (post_metrics),
которую понимает export_excel.py.

Каналы задаются НАЗВАНИЯМИ в .env (через запятую, как видно в Telegram):
    CHANNEL_NAMES=test,Мой второй канал

Порядок:
    python web_scraper.py --login      # вход один раз (откроется /a/)
    python web_scraper.py              # сбор по всем каналам из CHANNEL_NAMES
    python export_excel.py             # выгрузка в Excel

Служебное:
    python web_scraper.py --dump       # сохранить HTML для отладки селекторов

При VPN блок WEB_PROXY в .env оставляйте пустым.
"""

import asyncio
import os
import re
import sys
from datetime import datetime, timezone

import sqlite3

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

WEB_URL = "https://web.telegram.org/a/"
USER_DATA_DIR = os.getenv("USER_DATA_DIR", "tg_web_profile_a")
DB_PATH = os.getenv("DB_PATH", "stats.db")
POSTS_LIMIT = int(os.getenv("POSTS_LIMIT", "4"))
CHANNEL_NAMES = [c.strip() for c in os.getenv("CHANNEL_NAMES", "").split(",")
                 if c.strip()]

PROXY_SERVER = os.getenv("WEB_PROXY", "").strip()
PROXY_USER = os.getenv("PROXY_USER", "").strip() or None
PROXY_PASS = os.getenv("PROXY_PASS", "").strip() or None

# --- Селекторы WebZ (/a/), сняты с реальной страницы ------------------------
SEL_APP_READY = "#LeftColumn"
SEL_SEARCH = "#telegram-search-input"
SEL_CHAT_ITEM = ".chat-item-clickable"
SEL_MESSAGE_LIST = ".MessageList"
SEL_MESSAGE = ".Message.message-list-item"
SEL_VIEWS = ".message-views"
SEL_REACTION = ".message-reaction"
SEL_COMMENTS = ".CommentButton .label"


def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS post_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT, message_id INTEGER, views INTEGER, forwards INTEGER,
            reactions_total INTEGER, reactions_json TEXT, comments INTEGER,
            link TEXT, taken_at TEXT NOT NULL
        );
        """
    )
    # миграция для баз, созданных до появления столбца link
    try:
        con.execute("ALTER TABLE post_metrics ADD COLUMN link TEXT")
    except sqlite3.OperationalError:
        pass
    con.commit()
    con.close()


def save_metric(row: dict) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """INSERT INTO post_metrics
           (channel, message_id, views, forwards, reactions_total,
            reactions_json, comments, link, taken_at)
           VALUES (:channel,:message_id,:views,:forwards,:reactions_total,
                   :reactions_json,:comments,:link,:taken_at)""",
        row,
    )
    con.commit()
    con.close()


def _num(text):
    """'1.2K' / '3,4 тыс.' / '1 comment' / '512' -> int.
    Суффикс (k/тыс/m/млн) учитывается ТОЛЬКО если стоит сразу после числа."""
    if not text:
        return None
    t = str(text).strip().lower().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k|к|тыс|m|млн)?", t)
    if not m:
        return None
    val = float(m.group(1))
    suf = m.group(2)
    if suf in ("k", "к", "тыс"):
        val *= 1000
    elif suf in ("m", "млн"):
        val *= 1_000_000
    return int(val)


async def _text_or_none(locator):
    try:
        if await locator.count():
            return (await locator.first.inner_text()).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


async def _item_title(item) -> str:
    t = item.locator(".fullName, .title")
    try:
        if await t.count():
            return (await t.first.inner_text()).strip()
        return (await item.inner_text()).strip()
    except Exception:  # noqa: BLE001
        return ""


async def open_channel(page, name: str) -> bool:
    """Открывает канал через поиск WebZ: кликает ВИДИМЫЙ результат,
    предпочитая точное совпадение названия (канал, а не группу обсуждения)."""
    try:
        await page.click(SEL_SEARCH)
        await page.fill(SEL_SEARCH, "")
        await page.type(SEL_SEARCH, name, delay=40)
        await page.wait_for_timeout(2500)          # дать поиску отработать

        items = page.locator(SEL_CHAT_ITEM)
        cands = []
        for i in range(await items.count()):
            it = items.nth(i)
            try:
                if not await it.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            title = await _item_title(it)
            if name.lower() in title.lower():
                cands.append((title, it))

        if not cands:
            print(f"  [!] '{name}': нет видимых результатов поиска")
            return False

        # точное совпадение вперёд, затем самое короткое название
        cands.sort(key=lambda c: (c[0].strip().lower() != name.strip().lower(),
                                  len(c[0])))
        title, target = cands[0]
        await target.scroll_into_view_if_needed()
        await target.click()
        await page.wait_for_selector(SEL_MESSAGE_LIST, timeout=30_000)
        await page.wait_for_timeout(2500)          # дать дорисоваться метрикам
        print(f"    открыт: «{title}»")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] не удалось открыть '{name}': {exc}")
        return False


def _channel_internal_id(url: str):
    """Из URL WebZ (#-1004437187157) -> внутренний id для ссылки t.me/c/ (4437187157)."""
    m = re.search(r"#(-?\d+)", url or "")
    if not m:
        return None
    cid = m.group(1)
    if cid.startswith("-100"):
        return cid[4:]
    return cid.lstrip("-")


async def scrape_channel(page, name: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    internal = _channel_internal_id(page.url)      # id канала для ссылок
    msgs = page.locator(SEL_MESSAGE)
    total = await msgs.count()
    if total == 0:
        print(f"  [!] {name}: посты не найдены")
        return
    take = min(POSTS_LIMIT, total)
    for i in range(total - take, total):           # последние N (внизу списка)
        m = msgs.nth(i)
        try:
            mid = await m.get_attribute("data-message-id")
            views = _num(await _text_or_none(m.locator(SEL_VIEWS)))
            reactions = 0
            rc = m.locator(SEL_REACTION)
            for j in range(await rc.count()):
                reactions += _num(await rc.nth(j).inner_text()) or 0
            comments = _num(await _text_or_none(m.locator(SEL_COMMENTS)))
            link = f"https://t.me/c/{internal}/{mid}" \
                if internal and mid else None
            save_metric({
                "channel": name, "message_id": _num(mid) or i,
                "views": views, "forwards": None,
                "reactions_total": reactions, "reactions_json": "{}",
                "comments": comments, "link": link, "taken_at": now,
            })
            print(f"  {name} #{mid}: views={views} "
                  f"reactions={reactions} comments={comments} {link or ''}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [!] пост {i}: {exc}")


async def dump(page) -> None:
    """Сохраняет HTML для отладки. Канал откройте ВРУЧНУЮ, затем Enter."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, input,
        "\nОткройте нужный канал в окне браузера, затем нажмите Enter...")
    data = await page.evaluate(
        """() => {
            const pick = (sels) => {
                for (const s of sels) {
                    const e = document.querySelector(s);
                    if (e) return e.innerHTML;
                }
                return '';
            };
            return {
                left: pick(['#LeftColumn']),
                middle: pick(['#MiddleColumn', '.MessageList'])
            };
        }"""
    )
    for fname, key in (("dump_left.html", "left"), ("dump_middle.html", "middle")):
        with open(fname, "w", encoding="utf-8") as f:
            f.write(data.get(key, ""))
    print("[OK] Сохранены dump_left.html и dump_middle.html")


def _proxy_conf():
    if not PROXY_SERVER:
        return None
    conf = {"server": PROXY_SERVER}
    if PROXY_USER and PROXY_PASS:
        conf.update(username=PROXY_USER, password=PROXY_PASS)
    return conf


async def main() -> None:
    mode = "collect"
    if "--login" in sys.argv:
        mode = "login"
    elif "--dump" in sys.argv:
        mode = "dump"

    init_db()
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=(mode == "collect"),
            proxy=_proxy_conf(),
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(WEB_URL)

        if mode == "login":
            print("Войдите в открывшемся окне (QR или номер+код). "
                  "Жду загрузки приложения (до 5 минут)...")
            try:
                await page.wait_for_selector(SEL_APP_READY, timeout=300_000)
                print("[OK] Вход выполнен, сессия сохранена. Можно закрывать.")
            except Exception:  # noqa: BLE001
                print("[!] Не дождался загрузки. Если вошли — повторите позже.")
            await ctx.close()
            return

        if mode == "dump":
            try:
                await page.wait_for_selector(SEL_APP_READY, timeout=60_000)
            except Exception:  # noqa: BLE001
                pass
            await dump(page)
            await ctx.close()
            return

        # --- сбор ---
        if not CHANNEL_NAMES:
            print("[!] CHANNEL_NAMES пуст — впишите названия каналов в .env")
            await ctx.close()
            return
        try:
            await page.wait_for_selector(SEL_APP_READY, timeout=60_000)
        except Exception:  # noqa: BLE001
            print("[!] Приложение не загрузилось — вы залогинены на /a/?")
            await ctx.close()
            return

        for name in CHANNEL_NAMES:
            print(f"Канал: {name}")
            if await open_channel(page, name):
                await scrape_channel(page, name)
            await page.keyboard.press("Escape")     # выйти из поиска
            await page.wait_for_timeout(800)

        await ctx.close()
        print("Готово. Экспорт: python export_excel.py")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
