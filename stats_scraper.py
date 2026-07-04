"""
Скрапер «Статистики» канала через веб-версию (WebZ /a/) + Playwright.
====================================================================
Цель — достать разбивку НОВЫХ ПОДПИСЧИКОВ ПО ИСТОЧНИКАМ и подписки/отписки,
которые есть ТОЛЬКО в разделе «Статистика». Графики там рисуются на canvas
(чисел в HTML нет), поэтому данные перехватываются на лету: хук на JSON.parse
складывает каждый график (формат columns/names) в window.__tgGraphs.
API_ID/API_HASH НЕ нужны — работаем через ваш веб-логин (как web_scraper.py).

Два режима:
  --recon   РАЗВЕДКА (начните с него). Для каждого канала вы вручную
            открываете «Статистику», жмёте Enter — скрипт выгружает всё
            перехваченное в stats_dump_<канал>.json и DOM в stats_dom_<канал>.html.
            В базу НЕ пишет. По этим файлам донастраивается разбор источников.

  (без флага)  СБОР. То же самое, но перехваченные графики классифицируются
            (followers / growth / new_followers_by_source) и пишутся в таблицу
            channel_stats — её же понимает export_excel.py.

Логин — общий с web_scraper.py (та же папка профиля USER_DATA_DIR):
    python web_scraper.py --login     # если ещё не входили
    python stats_scraper.py --recon   # разведка
    python stats_scraper.py           # сбор
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

WEB_URL = "https://web.telegram.org/a/"
USER_DATA_DIR = os.getenv("USER_DATA_DIR", "tg_web_profile_a")
DB_PATH = os.getenv("DB_PATH", "stats.db")
CHANNEL_NAMES = [c.strip() for c in os.getenv("CHANNEL_NAMES", "").split(",")
                 if c.strip()]
PROXY_SERVER = os.getenv("WEB_PROXY", "").strip()
PROXY_USER = os.getenv("PROXY_USER", "").strip() or None
PROXY_PASS = os.getenv("PROXY_PASS", "").strip() or None

SEL_APP_READY = "#LeftColumn"
SEL_SEARCH = "#telegram-search-input"
SEL_CHAT_ITEM = ".chat-item-clickable"

# Хук ставится ДО скриптов страницы: оборачиваем JSON.parse и собираем
# всё, что похоже на график Telegram (первый столбец — ось "x").
INIT_SCRIPT = """
(() => {
  if (window.__tgHooked) return;
  window.__tgHooked = true;
  window.__tgGraphs = [];
  const orig = JSON.parse;
  JSON.parse = function () {
    const res = orig.apply(this, arguments);
    try {
      if (res && Array.isArray(res.columns) && res.columns.length &&
          Array.isArray(res.columns[0]) && res.columns[0][0] === 'x') {
        window.__tgGraphs.push(res);
      }
    } catch (e) {}
    return res;
  };
})();
"""

# Ключевые слова для классификации рядов графика (рус + англ).
JOINED_KW = ("joined", "подписал")
LEFT_KW = ("left", "отписал", "отпис")
SOURCE_KW = ("url", "ссылк", "search", "поиск", "group", "групп", "channel",
             "канал", "folder", "папк", "similar", "похож", "private", "личн",
             "other", "друг")


def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS channel_stats (
            channel  TEXT NOT NULL,
            graph    TEXT NOT NULL,
            day      TEXT NOT NULL,
            series   TEXT NOT NULL,
            value    REAL,
            taken_at TEXT NOT NULL,
            UNIQUE(channel, graph, day, series)
        );
        CREATE INDEX IF NOT EXISTS idx_cstats_lookup
            ON channel_stats(channel, graph, day);
        """
    )
    con.commit()
    con.close()


def save_rows(rows: list[dict]) -> None:
    if not rows:
        return
    con = sqlite3.connect(DB_PATH)
    con.executemany(
        """INSERT OR REPLACE INTO channel_stats
           (channel, graph, day, series, value, taken_at)
           VALUES (:channel, :graph, :day, :series, :value, :taken_at)""",
        rows,
    )
    con.commit()
    con.close()


def parse_graph(data: dict) -> list[tuple[str, str, float]]:
    """Формат графика Telegram -> [(day, series, value)]. x — epoch ms."""
    columns = data.get("columns") or []
    names = data.get("names") or {}
    x_axis: list[str] = []
    series_cols: list[tuple[str, list]] = []
    for col in columns:
        if not col:
            continue
        key, values = col[0], col[1:]
        if key == "x":
            x_axis = [
                datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                for v in values
            ]
        else:
            series_cols.append((names.get(key, key), values))
    out = []
    for series, values in series_cols:
        for day, val in zip(x_axis, values):
            if val is not None:
                out.append((day, series, float(val)))
    return out


def classify(data: dict) -> str | None:
    """Определяет тип графика по названиям рядов. None — не наш график."""
    names = [str(v).lower() for v in (data.get("names") or {}).values()]
    n = len(names)
    if not names:
        return None
    has_joined = any(any(k in nm for k in JOINED_KW) for nm in names)
    has_left = any(any(k in nm for k in LEFT_KW) for nm in names)
    if has_joined and has_left:
        return "followers"                      # подписки/отписки
    has_source = any(any(k in nm for k in SOURCE_KW) for nm in names)
    if has_source and n >= 2:
        return "new_followers_by_source"        # источники
    if n == 1:
        return "growth"                         # один ряд -> размер канала
    return None


async def _text_or_none(locator):
    try:
        if await locator.count():
            return (await locator.first.inner_text()).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


async def open_channel(page, name: str) -> bool:
    """Открывает канал через поиск WebZ (как в web_scraper.py)."""
    try:
        await page.click(SEL_SEARCH)
        await page.fill(SEL_SEARCH, "")
        await page.type(SEL_SEARCH, name, delay=40)
        await page.wait_for_timeout(2500)
        items = page.locator(SEL_CHAT_ITEM)
        cands = []
        for i in range(await items.count()):
            it = items.nth(i)
            try:
                if not await it.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            title = await _text_or_none(it.locator(".fullName, .title")) \
                or await _text_or_none(it) or ""
            if name.lower() in title.lower():
                cands.append((title, it))
        if not cands:
            print(f"  [!] '{name}': нет видимых результатов поиска")
            return False
        cands.sort(key=lambda c: (c[0].strip().lower() != name.strip().lower(),
                                  len(c[0])))
        title, target = cands[0]
        await target.scroll_into_view_if_needed()
        await target.click()
        await page.wait_for_timeout(2500)
        print(f"    открыт: «{title}»")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] не удалось открыть '{name}': {exc}")
        return False


async def collect_graphs(page) -> list[dict]:
    """Забирает всё, что перехватил хук JSON.parse."""
    try:
        return await page.evaluate("() => window.__tgGraphs || []")
    except Exception:  # noqa: BLE001
        return []


async def clear_graphs(page) -> None:
    try:
        await page.evaluate("() => { window.__tgGraphs = []; }")
    except Exception:  # noqa: BLE001
        pass


async def wait_enter(prompt: str) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, prompt)


async def process_channel(page, name: str, recon: bool) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await clear_graphs(page)

    if not await open_channel(page, name):
        return

    # Открыть «Статистику» в WebZ автоматически по разным версиям верстки трудно,
    # поэтому просим сделать это руками — надёжно в любой версии клиента.
    await wait_enter(
        f"\n[{name}] Откройте раздел «Статистика» этого канала в окне браузера,\n"
        "дождитесь отрисовки графиков и нажмите Enter здесь...")
    await page.wait_for_timeout(1500)

    graphs = await collect_graphs(page)
    print(f"  перехвачено графиков: {len(graphs)}")

    if recon:
        safe = name.replace("/", "_").replace(" ", "_")
        with open(f"stats_dump_{safe}.json", "w", encoding="utf-8") as f:
            json.dump(graphs, f, ensure_ascii=False, indent=2)
        try:
            dom = await page.evaluate(
                "() => (document.querySelector('#MiddleColumn')||document.body).innerHTML")
        except Exception:  # noqa: BLE001
            dom = ""
        with open(f"stats_dom_{safe}.html", "w", encoding="utf-8") as f:
            f.write(dom)
        for i, g in enumerate(graphs):
            names = list((g.get("names") or {}).values())
            print(f"    #{i}: ряды={names} тип={classify(g)}")
        print(f"  [OK] сохранено: stats_dump_{safe}.json, stats_dom_{safe}.html")
        return

    rows: list[dict] = []
    for g in graphs:
        gtype = classify(g)
        if not gtype:
            continue
        for day, series, value in parse_graph(g):
            rows.append({"channel": name, "graph": gtype, "day": day,
                         "series": series, "value": value, "taken_at": now})
    save_rows(rows)
    kinds = {r["graph"] for r in rows}
    print(f"  сохранено точек: {len(rows)} | типы: {', '.join(sorted(kinds)) or '—'}")


def _proxy_conf():
    if not PROXY_SERVER:
        return None
    conf = {"server": PROXY_SERVER}
    if PROXY_USER and PROXY_PASS:
        conf.update(username=PROXY_USER, password=PROXY_PASS)
    return conf


async def main() -> None:
    recon = "--recon" in sys.argv
    if not CHANNEL_NAMES:
        raise SystemExit("CHANNEL_NAMES пуст — впишите названия каналов в .env")
    init_db()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=False, proxy=_proxy_conf(),
        )
        await ctx.add_init_script(INIT_SCRIPT)      # хук ДО загрузки страницы
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(WEB_URL)
        try:
            await page.wait_for_selector(SEL_APP_READY, timeout=60_000)
        except Exception:  # noqa: BLE001
            print("[!] Приложение не загрузилось — вы залогинены на /a/?")
            print("    Войдите один раз: python web_scraper.py --login")
            await ctx.close()
            return

        mode = "РАЗВЕДКА (в базу не пишем)" if recon else "СБОР"
        print(f"Режим: {mode}. Каналы: {', '.join(CHANNEL_NAMES)}")
        for name in CHANNEL_NAMES:
            print(f"\nКанал: {name}")
            await process_channel(page, name, recon)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)

        await ctx.close()
        if recon:
            print("\nГотово. Пришлите файлы stats_dump_*.json — по ним настрою "
                  "точный разбор источников.")
        else:
            print("\nГотово. Экспорт: python export_excel.py")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
