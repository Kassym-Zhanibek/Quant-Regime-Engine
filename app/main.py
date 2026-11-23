import os
import asyncio
import math
import joblib
import pandas as pd
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"

from app.collector.rss_parser import fetch_and_process_rss
from app.collector.macro_fetcher import fetch_macro_indicators
from app.storage.db import init_db, get_connection, get_recent_news, save_sentiment_snapshot, get_sentiment_history
from app.collector.macro_fetcher import get_markets, get_stock_quotes
from app.storage import posts_db
from app.scoring import service as bg_service
from app.scoring.pipeline import pipeline as tg_pipeline
from app.categories import CATEGORIES
from app import config as app_config

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models_cache", "gold_rf_calibrated.pkl")
try:
    rf_model = joblib.load(MODEL_PATH)
    print(f"✅ Модель успешно загружена из {MODEL_PATH}")
except Exception as e:
    rf_model = None
    print(f"❌ Модель не найдена по пути {MODEL_PATH}. Используется fallback-логика.")


async def process_new_rss_feeds():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Обновление новостей...")
    await fetch_and_process_rss()


async def scheduled_rss_worker():
    while True:
        try:
            await process_new_rss_feeds()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Ошибка воркера: {e}")
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    posts_db.init_db()                       # новые таблицы + разовый перенос tg_posts прошлой версии
    # Прежний RSS-конвейер (таблица news) кормит модель Market Bias — см. LEGACY_RSS_ENABLED в app/config.py
    worker_task = asyncio.create_task(scheduled_rss_worker()) if app_config.LEGACY_RSS_ENABLED else None
    # Общий конвейер: Telegram + RSS -> фильтр Qwen -> FinBERT + Qwen по категориям -> скоры
    tg_task = asyncio.create_task(tg_pipeline.worker()) if app_config.PIPELINE_ENABLED else None
    yield
    for task in (worker_task, tg_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Quant Regime Engine", lifespan=lifespan)


class MacroIndicatorData(BaseModel):
    price: float
    change_pct: float


class YieldsData(BaseModel):
    yield_pct: float
    change_bps: float


class FullNlpPipelineResponse(BaseModel):
    asset: str
    market_bias_score: float
    latest_sentiment_score: float
    bias_label: str
    confidence: float
    active_news_count: int
    dxy: MacroIndicatorData
    us10y_yields: YieldsData
    macro_score: float
    last_updated: str


def get_yesterdays_decayed_sentiment():
    conn = get_connection()
    yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d 00:00:00')
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d 00:00:00')

    cursor = conn.cursor()
    cursor.execute("""
        SELECT finbert_score, qwen_score, finbert_neu, category 
        FROM news WHERE published_at >= ? AND published_at < ?
    """, (yesterday_str, today_str))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return 0.0, 0.0

    fb_num, qwen_num, den = 0.0, 0.0, 0.0
    for r in rows:
        w_cat = 1.5 if r["category"] == "DIRECT_GOLD" else 1.0
        w_impact = w_cat * (1.0 - (r["finbert_neu"] or 0.0))
        fb_num += (r["finbert_score"] or 0.0) * w_impact
        qwen_num += (r["qwen_score"] or 0.0) * w_impact
        den += w_impact

    avg_fb = fb_num / den if den > 0 else 0.0
    avg_qwen = qwen_num / den if den > 0 else 0.0
    decay_factor = math.exp(-0.693)
    return avg_fb * decay_factor, avg_qwen * decay_factor


# --- HTML РОУТЫ ---

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
NAV_ON = "bg-blue-600 text-white shadow-lg shadow-blue-600/20"
NAV_OFF = "text-gray-300 hover:bg-gray-800"


def _render_page(name: str, active: str) -> HTMLResponse:
    """Читает шаблон и подставляет в <!--NAV--> общую навигацию (_nav.html), подсвечивая активную вкладку."""
    path = os.path.join(TEMPLATES_DIR, name)
    if not os.path.exists(path):
        return HTMLResponse(f"<h1>{name} не найден в app/templates</h1>", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()
    nav_path = os.path.join(TEMPLATES_DIR, "_nav.html")
    if os.path.exists(nav_path):
        with open(nav_path, "r", encoding="utf-8") as f:
            nav = (f.read().replace("{{GOLD_CLS}}", NAV_ON if active == "gold" else NAV_OFF)
                           .replace("{{STOCKS_CLS}}", NAV_ON if active == "stocks" else NAV_OFF))
        html = html.replace("<!--NAV-->", nav)
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
@app.get("/gold", response_class=HTMLResponse)
async def read_dashboard():
    return _render_page("index.html", "gold")


@app.get("/stocks", response_class=HTMLResponse)
async def read_stocks():
    return _render_page("stocks.html", "stocks")


@app.get("/stock/{key}", response_class=HTMLResponse)
async def read_stock_page(key: str):
    key = _resolve_category(key)
    if CATEGORIES[key].get("group") != "stocks":
        raise HTTPException(status_code=404, detail=f"{key} — не акция")
    return _render_page("category.html", "stocks")


@app.get("/news", response_class=HTMLResponse)
@app.get("/news.html", response_class=HTMLResponse)
@app.get("/news1.html", response_class=HTMLResponse)
async def read_news_page():
    templates_dir = TEMPLATES_DIR
    # Проверяем возможные варианты названия файла на диске
    for filename in ["news.html", "news1.html"]:
        path = os.path.join(templates_dir, filename)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return HTMLResponse("<h1>Файл новостей (news.html) не найден в app/templates</h1>", status_code=404)


# --- API РОУТЫ ---

@app.get("/api/v1/sentiment/gold", response_model=FullNlpPipelineResponse)
async def get_gold_sentiment():
    macro_data = await asyncio.to_thread(fetch_macro_indicators)

    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT finbert_score, qwen_score, finbert_neu, category, raw_score 
        FROM news WHERE published_at >= ?
    """, (cutoff_time.strftime('%Y-%m-%d %H:%M:%S'),))
    news_rows = cursor.fetchall()
    conn.close()

    if not news_rows:
        avg_fb, avg_qwen = await asyncio.to_thread(get_yesterdays_decayed_sentiment)
        active_count = 0
        latest_score = avg_fb
    else:
        fb_num, qwen_num, den = 0.0, 0.0, 0.0
        for r in news_rows:
            w_cat = 1.5 if r["category"] == "DIRECT_GOLD" else 1.0
            w_impact = w_cat * (1.0 - (r["finbert_neu"] or 0.0))
            fb_num += (r["finbert_score"] or 0.0) * w_impact
            qwen_num += (r["qwen_score"] or 0.0) * w_impact
            den += w_impact

        avg_fb = fb_num / den if den > 0 else 0.0
        avg_qwen = qwen_num / den if den > 0 else 0.0
        active_count = len(news_rows)
        latest_score = news_rows[0]["raw_score"]

    dxy_delta = macro_data["dxy"]["change_pct"]
    us10y_delta_bps = macro_data["us10y_yields"]["change_bps"]

    if rf_model:
        features = pd.DataFrame({
            'finbert_score': [avg_fb],
            'qwen_score': [avg_qwen],
            'dxy_change': [dxy_delta],
            'fed_change': [us10y_delta_bps]
        })
        probs = rf_model.predict_proba(features)[0]
        prob_down, prob_up = probs[0], probs[1]

        composite_bias = round(prob_up - prob_down, 4)
        confidence = round(abs(prob_up - 0.5) / 0.5, 2)
    else:
        nlp_score = math.tanh(((avg_fb * 0.3) + (avg_qwen * 0.7)) / 2.0)
        composite_bias = round(nlp_score * 0.7 + macro_data["macro_score"] * 0.3, 4)
        confidence = round(abs(composite_bias), 2)

    label = "BULLISH" if composite_bias >= 0.20 else "BEARISH" if composite_bias <= -0.20 else "NEUTRAL"

    save_sentiment_snapshot(composite_bias, round(latest_score, 4), macro_data["macro_score"])

    return {
        "asset": "GC (Gold)",
        "market_bias_score": composite_bias,
        "latest_sentiment_score": round(latest_score, 4),
        "bias_label": label,
        "confidence": confidence,
        "active_news_count": active_count,
        "dxy": macro_data["dxy"],
        "us10y_yields": macro_data["us10y_yields"],
        "macro_score": macro_data["macro_score"],
        "last_updated": datetime.now(timezone.utc).isoformat()
    }


@app.get("/api/v1/sentiment/history")
async def get_history_series():
    return get_sentiment_history(limit=50)


@app.get("/api/v1/news")
async def get_news_feed():
    return get_recent_news()


@app.post("/api/v1/refresh")
async def force_refresh(background_tasks: BackgroundTasks):
    background_tasks.add_task(process_new_rss_feeds)
    return {"status": "Refresh triggered"}


# --- КАТЕГОРИИ: золото (общий сентимент / нефть / облигации / доллар) и акции (тикеры + «Рынок США») ---

_CAT_LOOKUP = {k.lower(): k for k in CATEGORIES}


def _resolve_category(key: str) -> str:
    """'nvda' / 'NVDA' / 'SPX' -> канонический ключ категории; 404, если такой нет."""
    canon = _CAT_LOOKUP.get(key.lower())
    if canon is None:
        raise HTTPException(status_code=404, detail=f"Неизвестная категория: {key}")
    return canon


def _pipeline_info() -> dict:
    return {**tg_pipeline.status, "poll_seconds": app_config.TG_POLL_SECONDS, "rss_poll_seconds": app_config.RSS_POLL_SECONDS}


@app.get("/category/{key}", response_class=HTMLResponse)
async def read_category_page(key: str):
    key = _resolve_category(key)
    return _render_page("category.html", "stocks" if CATEGORIES[key].get("group") == "stocks" else "gold")


@app.get("/api/v1/overview")
async def get_overview():
    """Главная: по каждой из 4 категорий — котировки + скор сентимента (основной полупериод) + статус конвейера."""
    markets = await asyncio.to_thread(get_markets)
    overview = await asyncio.to_thread(bg_service.build_overview, markets)
    return {**overview, "pipeline": _pipeline_info()}


@app.get("/api/v1/stocks/overview")
async def get_stocks_overview():
    """Вкладка «Акции»: «Рынок США» + тикеры — котировки, скоры по всем полупериодам, счётчики постов."""
    quotes = await asyncio.to_thread(get_stock_quotes)
    overview = await asyncio.to_thread(bg_service.build_stocks_overview, quotes)
    return {**overview, "pipeline": _pipeline_info()}


@app.get("/api/v1/category/{key}")
async def get_category(key: str):
    """Страница категории: скоры по всем полупериодам, счётчики постов, источники, котировки, статус конвейера."""
    key = _resolve_category(key)
    data = await asyncio.to_thread(bg_service.build_category, key)
    extra: dict = {}
    if CATEGORIES[key].get("group") == "stocks":
        q = (await asyncio.to_thread(get_stock_quotes)).get(key, {})
        markets = q.get("markets", [])
        if "next_earnings" in q:                      # свинг-контекст тикера: 5 сессий и дата отчётности
            extra["stock"] = {k: q.get(k) for k in ("change_5d_pct", "next_earnings", "days_to_earnings")}
    else:
        markets = (await asyncio.to_thread(get_markets)).get(key, [])
    return {**data, "markets": markets, "pipeline": _pipeline_info(), **extra}


@app.get("/api/v1/category/{key}/series")
async def get_category_series(key: str, hours: int = 72):
    key = _resolve_category(key)
    return await asyncio.to_thread(bg_service.build_series, key, hours)


@app.get("/api/v1/category/{key}/posts")
async def get_category_posts(key: str, limit: int = 40, hours: int = 0):
    """Последние важные посты категории со скорами FinBERT и Qwen."""
    key = _resolve_category(key)
    limit = max(1, min(limit, 200))
    return await asyncio.to_thread(posts_db.recent_posts, key, limit, hours or None)


@app.post("/api/v1/pipeline/refresh")
async def refresh_pipeline(background_tasks: BackgroundTasks):
    """Запустить цикл конвейера сейчас (если он уже идёт — второй не стартует)."""
    if tg_pipeline.status["running"]:
        return {"status": "already_running"}
    background_tasks.add_task(tg_pipeline.run_cycle, True)
    return {"status": "triggered"}

