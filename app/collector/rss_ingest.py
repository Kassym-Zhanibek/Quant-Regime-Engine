"""
Загрузка RSS-лент для общего конвейера (источники — app/sources.py, kind = "rss").

Каждая запись ленты превращается в «пост» так же, как сообщение Telegram, и дальше идёт по одному и тому же
пути: фильтр Qwen -> FinBERT -> Qwen U/D/N по категориям. Прежний RSS-конвейер (rss_parser.py -> таблица news)
этот модуль не трогает.
"""
import asyncio
import calendar
import html
import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from app import config
from app.scoring.text_clean import ANY_URL, SPACES
from app.storage.posts_db import utc_str

logger = logging.getLogger(__name__)

# Заголовки обычного браузера: часть сайтов отдаёт 401/403 «ботам»
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml,application/xml;q=0.9,text/xml;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.5",
}
TAGS = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    return SPACES.sub(" ", html.unescape(TAGS.sub(" ", s or ""))).strip()


def build_text(title: str, summary: str) -> str:
    """Заголовок + короткое описание (если оно не повторяет заголовок)."""
    title, summary = title.strip(), summary.strip()
    if summary.casefold().startswith(title.casefold()):        # описание повторяет заголовок — оставляем только хвост
        summary = summary[len(title):].lstrip(" .:-—")
    if len(summary) > config.RSS_MAX_SUMMARY_CHARS:
        summary = summary[: config.RSS_MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "…"
    if not summary or summary.casefold() == title.casefold():
        return title
    sep = "" if title.endswith((".", "!", "?", ":")) else "."
    return f"{title}{sep} {summary}"


def parse_feed_text(src: dict, xml_text: str, now: datetime | None = None) -> list[dict]:
    """Чистая функция: текст ленты -> список постов (dict'ы для posts_db.insert_posts)."""
    now = now or datetime.now(timezone.utc)
    oldest = now - timedelta(hours=config.RSS_MAX_AGE_HOURS)
    parsed = feedparser.parse(xml_text)
    key = f"rss:{src['id']}"
    rows = []
    for e in parsed.entries:
        title = strip_html(e.get("title"))
        if not title:
            continue
        summary = "" if src.get("title_only") else strip_html(e.get("summary") or e.get("description"))
        ts = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc) if ts else now
        if dt < oldest:
            continue
        if dt > now + timedelta(hours=1):          # часовые пояса в лентах иногда врут
            dt = now
        text = build_text(title, summary)
        clean = SPACES.sub(" ", ANY_URL.sub("", text)).strip()
        post_id = e.get("id") or e.get("link") or f"{title}|{utc_str(dt)}"
        rows.append({"source": key, "post_id": str(post_id)[:500], "kind": "rss", "posted_at": utc_str(dt),
                     "title": title, "text": text, "clean_text": clean, "hashtags": None, "url": e.get("link")})
    return rows


async def fetch_feed(http: httpx.AsyncClient, src: dict) -> tuple[list[dict], str | None]:
    """(посты, ошибка). Ошибка одной ленты не мешает остальным."""
    try:
        r = await http.get(src["url"], headers=HEADERS, follow_redirects=True, timeout=config.RSS_TIMEOUT)
        r.raise_for_status()
        rows = await asyncio.to_thread(parse_feed_text, src, r.text)
        return rows, None
    except Exception as e:
        logger.warning("RSS %s: %s", src["id"], e)
        return [], f"{type(e).__name__}: {e}"[:200]


async def fetch_all(http: httpx.AsyncClient, srcs: list[dict]) -> tuple[list[dict], dict]:
    """Скачивает все ленты (по 4 одновременно). Возвращает (все посты, {id: {'entries': n, 'error': str|None}})."""
    sem = asyncio.Semaphore(4)

    async def one(src):
        async with sem:
            return src, *await fetch_feed(http, src)

    results = await asyncio.gather(*(one(s) for s in srcs))
    rows, report = [], {}
    for src, r, err in results:
        rows += r
        report[src["id"]] = {"entries": len(r), "error": err}
    return rows, report

