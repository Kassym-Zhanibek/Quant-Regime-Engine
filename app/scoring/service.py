"""
Сервисный слой: собирает из базы то, что показывает панель. Всё считается по КАТЕГОРИЯМ
(general / oil / us10y / dxy), математика агрегации — app/scoring/aggregate.py.

  build_snapshot(cat)   итоговые скоры на «сейчас» по всем полупериодам
  build_series(cat, h)  почасовой ряд итоговых скоров за последние h часов
  build_category(cat)   snapshot + счётчики постов + список источников (страница категории)
  build_overview()      компактная сводка по всем категориям + котировки (главная страница)
  persist_recent()      запись почасового ряда всех категорий в sentiment_asof

Итоговый скор карточки (combined) = w·FinBERT + (1 − w)·Qwen, берётся по основному полупериоду категории.
Золото: w = ENSEMBLE_W_FINBERT (30/70, как раньше), полупериоды HALF_LIVES_H, основной PRIMARY_HL.
Акции:  w = STOCK_W_FINBERT, полупериоды STOCK_HALF_LIVES_H, основной STOCK_PRIMARY_HL (см. app/categories.py).
"""
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app import config, sources
from app.categories import (ALL_ORDER, CATEGORIES, ORDER, STOCK_ORDER, half_lives_for, primary_hl_for,
                            public_info, w_finbert_for)
from app.scoring import aggregate as agg
from app.storage import posts_db

_HIST_TTL = 3600.0
_hist_cache: dict = {"ts": 0.0, "vals": None}


# ---------------------------------------------------------------------------
# μ и σ для z-скора
# ---------------------------------------------------------------------------

def _read_history() -> dict:
    """Исторические скоры из news_posts (старый проект, только золото). Кэш на час."""
    now = time.time()
    if _hist_cache["vals"] is not None and now - _hist_cache["ts"] < _HIST_TTL:
        return _hist_cache["vals"]
    vals = {"finbert_score": [], "qwen_score": []}
    path = config.GOLD_DB_PATH
    if path and Path(path).exists():
        try:
            conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
            for col in vals:
                vals[col] = [r[0] for r in conn.execute(
                    f"SELECT {col} FROM {config.HIST_TABLE} WHERE {col} IS NOT NULL")]
            conn.close()
        except sqlite3.Error:
            vals = {"finbert_score": [], "qwen_score": []}
    _hist_cache.update(ts=now, vals=vals)
    return vals


def hist_stats(col: str, live_scores: np.ndarray, category: str = "general") -> tuple[float, float, str]:
    """
    (μ, σ, источник). Историю из news_posts используем только для «общего сентимента» (на ней считались скоры по
    золоту); для остальных категорий истории нет — μ = 0, σ по свежим постам этой же категории.
    """
    if category == "general":
        vals = _read_history()[col]
        if len(vals) >= config.MIN_HIST:
            arr = np.asarray(vals, dtype=float)
            return float(arr.mean()), float(arr.std(ddof=1)), f"{config.HIST_TABLE}, n={len(arr)}"
    live = live_scores[~np.isnan(live_scores)]
    if len(live) >= 2 and live.std(ddof=1) > 0:
        return 0.0, float(live.std(ddof=1)), f"свежие посты (истории мало), n={len(live)}"
    return 0.0, 1.0, "заглушка (μ=0, σ=1)"


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------

def _num(x):
    """NaN/inf -> None, чтобы JSON не падал."""
    if x is None:
        return None
    return None if (isinstance(x, float) and not math.isfinite(x)) else x


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return _num(obj)


def signal_label(x: float | None) -> str:
    if x is None or abs(x) < config.SIGNAL_THRESHOLD:
        return "NEUTRAL"
    return "BULLISH" if x > 0 else "BEARISH"


def _add_combined(table: list[dict], category: str) -> list[dict]:
    """К каждой строке таблицы полупериодов добавляет итоговый скор 'combined' (взвешенно FinBERT + Qwen)."""
    w = w_finbert_for(category)
    for item in table:
        f, q = item["finbert"], item["qwen"]
        item["combined"] = {
            "shrunk": w * f["shrunk"] + (1 - w) * q["shrunk"],
            "avg": None if f["avg"] is None or q["avg"] is None else w * f["avg"] + (1 - w) * q["avg"],
            "z": w * f["z"] + (1 - w) * q["z"],
        }
    return table


def _load(category: str):
    rows = posts_db.load_scored_posts(category)
    if not rows:
        return None
    t, fb, qw = agg.to_arrays(rows)
    stats = {"finbert": hist_stats("finbert_score", fb, category), "qwen": hist_stats("qwen_score", qw, category)}
    return t, fb, qw, stats


# ---------------------------------------------------------------------------
# Снимок, ряд, журнал
# ---------------------------------------------------------------------------

def build_snapshot(category: str, asof: datetime | None = None) -> dict:
    asof = (asof or datetime.now(timezone.utc)).replace(microsecond=0)
    half_lives = half_lives_for(category)
    base = {
        "category": public_info(category),
        "asof": asof.strftime(agg.TS_FMT),
        "primary_hl": primary_hl_for(category),
        "half_lives": half_lives,
        "shrink_k": config.SHRINK_K,
        "w_finbert": w_finbert_for(category),
        "has_data": False,
        "rows": [],
        "warnings": [],
    }
    loaded = _load(category)
    if loaded is None:
        return base
    t, fb, qw, stats = loaded
    row = agg.build_row(asof, t, fb, qw, stats, half_lives, config.SHRINK_K)

    window_h = (asof.timestamp() - float(t.min())) / 3600.0
    warnings = []
    cut = [hl for hl in half_lives if 0.5 ** (window_h / hl) > 0.1]
    if cut:
        warnings.append(
            f"Данные начинаются {window_h:.0f} ч назад — для полупериодов "
            f"{', '.join(f'{h:g}ч' for h in cut)} «хвост» затухания обрезан (W занижена), скоры надёжны меньше."
        )
    table = _add_combined(agg.row_to_table(row, half_lives), category)
    primary = next((r for r in table if r["hl"] == primary_hl_for(category)), table[0])
    base.update({
        "has_data": True,
        "rows": table,
        "label": signal_label(primary["combined"]["shrunk"]),
        "counts": {
            "finbert_n": row["finbert_n"], "qwen_n": row["qwen_n"],
            "finbert_raw_mean": row["finbert_raw_mean"], "qwen_raw_mean": row["qwen_raw_mean"],
        },
        "norm": {n: {"mu": stats[n][0], "sigma": stats[n][1], "source": stats[n][2]} for n in stats},
        "window_hours": round(window_h, 1),
        "warnings": warnings,
    })
    return _clean(base)


def build_series(category: str, hours: int = 72) -> dict:
    hours = max(6, min(int(hours), 24 * 10))
    asof = datetime.now(timezone.utc).replace(microsecond=0)
    half_lives = half_lives_for(category)
    loaded = _load(category)
    if loaded is None:
        return {"half_lives": half_lives, "primary_hl": primary_hl_for(category), "points": []}
    t, fb, qw, stats = loaded
    points = []
    for g in agg.build_grid(asof, hours):
        row = agg.build_row(g, t, fb, qw, stats, half_lives, config.SHRINK_K)
        points.append({"asof": row["asof"], "rows": _add_combined(agg.row_to_table(row, half_lives), category)})
    return _clean({"half_lives": half_lives, "primary_hl": primary_hl_for(category), "points": points})


def persist_recent(hours: int = 48) -> int:
    """Пишет почасовой ряд (и точку «сейчас») всех категорий в sentiment_asof. Возвращает число строк."""
    total = 0
    asof = datetime.now(timezone.utc).replace(microsecond=0)
    for category in ALL_ORDER:
        half_lives = half_lives_for(category)
        loaded = _load(category)
        if loaded is None:
            continue
        t, fb, qw, stats = loaded
        rows = [agg.build_row(g, t, fb, qw, stats, half_lives, config.SHRINK_K) for g in agg.build_grid(asof, hours)]
        posts_db.save_asof_rows(category, rows)
        total += len(rows)
    return total


# ---------------------------------------------------------------------------
# Страница категории и главная
# ---------------------------------------------------------------------------

def source_info(category: str) -> list[dict]:
    """Какие источники кормят категорию (для блока «Источники» на странице) + сколько постов дали за 7 суток."""
    stats = posts_db.source_stats(category, days=7)
    out = []
    for s in sources.SOURCES:
        if category not in s["categories"]:
            continue
        key = sources.source_key(s)
        st = stats.get(key, {})
        out.append({
            "id": s["id"], "kind": s["kind"],
            "url": f"https://t.me/{s['id']}" if s["kind"] == "tg" else s.get("url"),
            "gated": category in s.get("gated", []),
            "posts_7d": st.get("posts", 0), "last_post": st.get("last_post"),
        })
    return out


def build_category(category: str) -> dict:
    snap = build_snapshot(category)
    snap["posts"] = posts_db.counts_category(category)
    snap["sources"] = source_info(category)
    return snap


def build_overview(markets: dict | None = None) -> dict:
    """Сводка для главной: по каждой категории — скоры основного полупериода, число постов за 24 ч и котировки."""
    markets = markets or {}
    cats = []
    for key in ORDER:
        snap = build_snapshot(key)
        counts = posts_db.counts_category(key)
        item = {**public_info(key), "has_data": snap["has_data"], "hl": primary_hl_for(key),
                "posts_24h": counts["posts_24h"], "last_post": counts["last_post"],
                "pending": counts["pending_judge"] + counts["pending_finbert"] + counts["pending_qwen"],
                "markets": markets.get(key, []), "sentiment": None}
        if snap["has_data"]:
            r = next((r for r in snap["rows"] if r["hl"] == primary_hl_for(key)), snap["rows"][0])
            item["sentiment"] = {
                "combined": r["combined"]["shrunk"], "finbert": r["finbert"]["shrunk"], "qwen": r["qwen"]["shrunk"],
                "label": signal_label(r["combined"]["shrunk"]),
                "n": max(snap["counts"]["finbert_n"], snap["counts"]["qwen_n"]),
            }
        cats.append(item)
    return _clean({"asof": datetime.now(timezone.utc).strftime(agg.TS_FMT), "categories": cats,
                   "w_finbert": config.ENSEMBLE_W_FINBERT, "hl": config.PRIMARY_HL})


def build_stocks_overview(quotes: dict | None = None) -> dict:
    """
    Сводка для вкладки «Акции»: по «Рынку США» и каждому тикеру — котировка (+5 сессий, дата отчёта),
    скоры по ВСЕМ полупериодам (чтобы страница переключала их без новых запросов) и счётчики постов.
    """
    quotes = quotes or {}
    items = []
    for key in STOCK_ORDER:
        snap = build_snapshot(key)
        counts = posts_db.counts_category(key)
        hl = primary_hl_for(key)
        item = {**public_info(key), "has_data": snap["has_data"], "hl": hl, "half_lives": snap["half_lives"],
                "posts_24h": counts["posts_24h"], "last_post": counts["last_post"],
                "pending": counts["pending_judge"] + counts["pending_finbert"] + counts["pending_qwen"],
                "quote": quotes.get(key) or {}, "sentiment": None, "by_hl": {}}
        if snap["has_data"]:
            for r in snap["rows"]:
                item["by_hl"][f"{r['hl']:g}"] = {
                    "combined": r["combined"]["shrunk"], "finbert": r["finbert"]["shrunk"],
                    "qwen": r["qwen"]["shrunk"], "z": r["combined"]["z"]}
            r = next((r for r in snap["rows"] if r["hl"] == hl), snap["rows"][0])
            item["sentiment"] = {
                "combined": r["combined"]["shrunk"], "finbert": r["finbert"]["shrunk"], "qwen": r["qwen"]["shrunk"],
                "z": r["combined"]["z"], "label": signal_label(r["combined"]["shrunk"]),
                "n": max(snap["counts"]["finbert_n"], snap["counts"]["qwen_n"]),
            }
        items.append(item)
    return _clean({"asof": datetime.now(timezone.utc).strftime(agg.TS_FMT), "items": items,
                   "w_finbert": config.STOCK_W_FINBERT, "primary_hl": config.STOCK_PRIMARY_HL,
                   "half_lives": sorted(set(config.STOCK_HALF_LIVES_H))})

