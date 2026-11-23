"""
Агрегация постовых скоров в итоговый скор «общего новостного фона» на момент as-of.
Математика один-в-один из score_live.py (чистые функции, без обращений к базе).

Для каждого полупериода H (часы) и ОТДЕЛЬНО для FinBERT и Qwen:

    w_i    = 0.5 ** (age_i / H)         затухание: пост возраста H часов весит вдвое меньше свежего
    W      = Σ w_i                      «свежая масса» новостей
    avg    = Σ w_i·s_i / W              взвешенное среднее, [-1, 1]
    shrunk = Σ w_i·s_i / (W + k)        ИТОГОВЫЙ скор: то же, но с «якорем» из k псевдо-нейтральных постов
    z      = Σ w_i·(s_i − μ) / σ        затухающий импульс в единицах σ (μ, σ — из истории скоров)

Заглядывания в будущее нет: берутся только посты с posted_at <= as-of.
"""
from datetime import datetime, timedelta, timezone

import numpy as np

TS_FMT = "%Y-%m-%d %H:%M:%S"


def hl_tag(hl: float) -> str:
    return f"h{hl:g}".replace(".", "_")


def to_arrays(rows: list[tuple]):
    """rows: (posted_at 'YYYY-MM-DD HH:MM:SS' UTC, finbert_score|None, qwen_score|None) -> t, fb, qw (numpy)."""
    t = np.array([datetime.strptime(r[0], TS_FMT).replace(tzinfo=timezone.utc).timestamp() for r in rows], dtype=float)
    fb = np.array([np.nan if r[1] is None else r[1] for r in rows], dtype=float)
    qw = np.array([np.nan if r[2] is None else r[2] for r in rows], dtype=float)
    return t, fb, qw


def decayed(t, s, asof_ts, hl, mu, sigma, k) -> dict:
    m = (t <= asof_ts) & ~np.isnan(s)
    if not m.any():
        return {"avg": None, "shrunk": 0.0, "z": 0.0, "w": 0.0}
    age_h = (asof_ts - t[m]) / 3600.0
    w = 0.5 ** (age_h / hl)
    W = float(w.sum())
    ssum = float((w * s[m]).sum())
    return {
        "avg": ssum / W,
        "shrunk": ssum / (W + k),
        "z": float((w * (s[m] - mu)).sum() / sigma) if sigma > 0 else 0.0,
        "w": W,
    }


def build_row(asof: datetime, t, fb, qw, stats: dict, half_lives, k) -> dict:
    """Плоская строка (как в таблице sentiment_asof_live): asof, finbert_n, finbert_avg_h24, finbert_shrunk_h24, ..."""
    ts = asof.timestamp()
    row = {"asof": asof.strftime(TS_FMT)}
    for name, arr in (("finbert", fb), ("qwen", qw)):
        mu, sigma, _ = stats[name]
        m = (t <= ts) & ~np.isnan(arr)
        row[f"{name}_n"] = int(m.sum())
        row[f"{name}_raw_mean"] = float(arr[m].mean()) if m.any() else None      # без затухания, для сравнения
        for hl in half_lives:
            f = decayed(t, arr, ts, hl, mu, sigma, k)
            tag = hl_tag(hl)
            row[f"{name}_avg_{tag}"] = f["avg"]
            row[f"{name}_shrunk_{tag}"] = f["shrunk"]
            row[f"{name}_z_{tag}"] = f["z"]
            row[f"{name}_w_{tag}"] = f["w"]
    return row


def build_grid(asof: datetime, hours: int) -> list[datetime]:
    """Целые часы за последние `hours` часов + сама точка as-of."""
    start = (asof - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
    grid, h = [], start
    while h < asof:
        grid.append(h)
        h += timedelta(hours=1)
    grid.append(asof)
    return grid


def row_to_table(row: dict, half_lives) -> list[dict]:
    """Плоская строка -> [{hl, finbert:{shrunk,avg,z,w}, qwen:{...}}] для API / панели."""
    out = []
    for hl in half_lives:
        tag = hl_tag(hl)
        item = {"hl": hl}
        for name in ("finbert", "qwen"):
            item[name] = {
                "shrunk": row[f"{name}_shrunk_{tag}"],
                "avg": row[f"{name}_avg_{tag}"],
                "z": row[f"{name}_z_{tag}"],
                "w": row[f"{name}_w_{tag}"],
            }
        out.append(item)
    return out

