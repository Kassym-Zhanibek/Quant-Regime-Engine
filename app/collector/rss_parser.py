import feedparser
import asyncio
import logging
import httpx
from datetime import datetime, timezone
from app.model.finbert_model import get_finbert
from app.model.qwen_analyst import verify_and_analyze_gold_sentiment
from app.storage.db import is_news_processed, save_news_item

logger = logging.getLogger(__name__)

gatekeeper = get_finbert()   # общий экземпляр FinBERT (его же использует Telegram-конвейер)

# Оставил только реальные RSS-ленты. Обычные HTML-сайты парсер не читает.
RSS_FEEDS = [
    {"url": "https://www.forexlive.com/feed/commodities", "category": "DIRECT_GOLD"},
    {"url": "https://www.forexlive.com/feed/centralbank", "category": "MACRO_FED"},
    {"url": "https://www.investing.com/rss/news_11.rss", "category": "DIRECT_GOLD"},
    {"url": "https://www.investing.com/rss/central_banks.rss", "category": "MACRO_FED"},
    {"url": "https://www.cnbc.com/id/10000664/device/rss/rss.html", "category": "MACRO_FED"},
    {"url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml", "category": "MACRO_FED"},
    {"url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000839", "category": "DIRECT_GOLD"}
]

# Фейковые заголовки браузера, чтобы нас не блокировали сайты (ошибки 401/403)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5"
}

async def fetch_rss_content(url: str) -> str:
    """Асинхронное скачивание RSS с имитацией браузера и таймаутом"""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except Exception as e:
        logger.warning(f"Не удалось скачать {url}: {e}")
        return ""

async def fetch_and_process_rss():
    """
    Конвейер с ансамблевым сочетанием моделей:
    Final Score = 30% FinBERT + 70% Qwen
    """
    for feed_info in RSS_FEEDS:
        rss_text = await fetch_rss_content(feed_info["url"])
        if not rss_text:
            continue

        # Парсим текст локально в отдельном потоке
        parsed_feed = await asyncio.to_thread(feedparser.parse, rss_text)

        for entry in parsed_feed.entries:
            news_id = getattr(entry, "id", entry.link)

            # Проверяем в БД асинхронно
            if await asyncio.to_thread(is_news_processed, news_id):
                continue

            title = entry.title
            summary = getattr(entry, "summary", "")
            full_text = f"{title} {summary}".strip()
            published_at = getattr(entry, "published", datetime.now(timezone.utc).isoformat())

            # --- ЭТАП 1: FinBERT (Вероятности) ---
            finbert_res = await asyncio.to_thread(gatekeeper.analyze_text, full_text)

            item = {
                "id": news_id,
                "title": title,
                "summary": summary,
                "source": feed_info["url"],
                "published_at": published_at,
                "category": feed_info["category"]
            }

            if not finbert_res["is_relevant"]:
                await asyncio.to_thread(save_news_item, item, finbert_res, 0.0, 0.0)
                continue

            # --- ЭТАП 2: Qwen Валидатор ---
            logger.info(f"Анализ через Qwen: {title[:50]}...")
            qwen_score = await verify_and_analyze_gold_sentiment(full_text, finbert_res)

            # --- ЭТАП 3: Ансамбль (30% FinBERT + 70% Qwen) ---
            finbert_score = finbert_res["finbert_score"]
            ensemble_score = round(finbert_score * 0.30 + qwen_score * 0.70, 4)

            # Сохраняем в БД (асинхронно)
            await asyncio.to_thread(save_news_item, item, finbert_res, qwen_score, ensemble_score)

