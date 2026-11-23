"""
Проверка скоров панели на реальных ценах: «предсказывает ли скор движение рынка?»

Что делает
----------
1. Берёт посты из базы (posts / post_cat) и ПЕРЕСЧИТЫВАЕТ скоры на каждый целый час в прошлом тем же кодом,
   что и панель (app/scoring/aggregate.py). В расчёт идут только посты с posted_at <= момента расчёта.
2. Берёт почасовые цены: yfinance (золото GC=F, нефть CL=F, US10Y ^TNX, доллар DX-Y.NYB) или ваш CSV.
   Цена «известна» только после закрытия часового бара — заглядывания в будущее нет.
3. Для каждого часа считает, как изменилась цена через H часов, и сравнивает со скором:
     IC      ранговая корреляция (Спирмен) «скор -> будущее движение». > 0: скор угадывает направление.
     95% CI  доверительный интервал IC (блочный бутстрэп: учитывает, что окна H часов перекрываются).
     hit%    доля верных знаков среди «не нейтральных» сигналов (|скор| >= порога).
     спред   средний ход цены после бычьего сигнала минус после медвежьего, в б.п.
4. Сравнивает: FinBERT, Qwen, арифметическое среднее и знаковое взвешенное геометрическое среднее.
   Дополнительно: IC по всем полупериодам (3/12/24/72 ч) и (с --sweep) по сетке весов FinBERT/Qwen.

Знак скора = направление ЦЕНЫ актива (для US10Y — направление ДОХОДНОСТИ), поэтому IC > 0 всегда «хорошо».

Запуск (из корня проекта, панель может работать параллельно):
    python scripts/validate_scores.py
    python scripts/validate_scores.py --categories general,oil --horizons 6,24,72 --sweep
    python scripts/validate_scores.py --csv general=C:/data/XAUUSD_H1.csv        # свои цены вместо yfinance

Формат CSV: первая колонка — время бара (UTC), колонка Close/close/price (или последняя). Если время в файле
брокерское (например UTC+3), укажите --csv-shift-h 3. Время считается ОТКРЫТИЕМ часового бара (--bar-hours 1).
Если в файле моменты, в которые цена уже известна, поставьте --bar-hours 0.

Нужно: pandas, numpy (есть в проекте) и для загрузки цен — yfinance (уже используется в macro_fetcher.py).
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config                       # noqa: E402
from app.scoring import aggregate as agg     # noqa: E402
from app.scoring import service              # noqa: E402
from app.storage import posts_db             # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")   # чтобы кириллица и ★ не ломались в консоли Windows
except Exception:
    pass

# kind: pct — лог-доходность цены в б.п.; bps — абсолютное изменение доходности (^TNX котируется в %) в б.п.
ASSETS = {
    "general": {"ticker": "GC=F",     "name": "Золото (GC=F)", "kind": "pct"},
    "oil":     {"ticker": "CL=F",     "name": "WTI (CL=F)",    "kind": "pct"},
    "us10y":   {"ticker": "^TNX",     "name": "US10Y (^TNX)",  "kind": "bps"},
    "dxy":     {"ticker": "DX-Y.NYB", "name": "DXY",           "kind": "pct"},
}

KIND_TITLES = ("fb", "qw", "arith", "geo")


# ---------------------------------------------------------------------------
# Формулы итогового скора
# ---------------------------------------------------------------------------

def arith_mean(f, q, w):
    return w * np.asarray(f, dtype=float) + (1.0 - w) * np.asarray(q, dtype=float)


def geo_mean(f, q, w):
    """Знаковое взвешенное геометрическое среднее: знаки совпали -> sign·|f|^w·|q|^(1-w), иначе 0."""
    f = np.asarray(f, dtype=float)
    q = np.asarray(q, dtype=float)
    out = np.zeros_like(f)
    ok = (f * q) > 0
    out[ok] = np.sign(f[ok]) * np.abs(f[ok]) ** w * np.abs(q[ok]) ** (1.0 - w)
    return out


def make_signals(fb, qw, w):
    """kind -> (название, массив)."""
    return {
        "fb": ("FinBERT", np.asarray(fb, dtype=float)),
        "qw": ("Qwen", np.asarray(qw, dtype=float)),
        "arith": (f"Арифм. {w:.0%}/{1 - w:.0%}", arith_mean(fb, qw, w)),
        "geo": (f"Геометр. {w:.0%}/{1 - w:.0%}", geo_mean(fb, qw, w)),
    }


# ---------------------------------------------------------------------------
# Скоры по часам (пересчёт из базы, без заглядывания в будущее)
# ---------------------------------------------------------------------------

def build_scores(category, lookback_days, warmup_h, half_lives):
    rows = posts_db.load_scored_posts(category, lookback_days)
    if not rows:
        return None
    t, fb, qw = agg.to_arrays(rows)
    stats = {"finbert": service.hist_stats("finbert_score", fb, category),
             "qwen": service.hist_stats("qwen_score", qw, category)}

    first = pd.Timestamp(float(t.min()), unit="s", tz="UTC")
    start = (first + pd.Timedelta(hours=warmup_h)).ceil("h")
    end = pd.Timestamp.now(tz="UTC").floor("h")
    if start >= end:
        return None
    grid = pd.date_range(start, end, freq="1h")

    recs = []
    for g in grid:
        row = agg.build_row(g.to_pydatetime(), t, fb, qw, stats, half_lives, config.SHRINK_K)
        rec = {"asof": g}
        for hl in half_lives:
            tag = agg.hl_tag(hl)
            rec[f"fb_{hl:g}"] = row[f"finbert_shrunk_{tag}"]
            rec[f"qw_{hl:g}"] = row[f"qwen_shrunk_{tag}"]
        recs.append(rec)
    return pd.DataFrame(recs)


# ---------------------------------------------------------------------------
# Цены
# ---------------------------------------------------------------------------

def to_ns(idx) -> np.ndarray:
    """DatetimeIndex (любой tz / единицы времени) -> int64 наносекунды UTC."""
    idx = pd.DatetimeIndex(idx)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return idx.tz_localize(None).to_numpy(dtype="datetime64[ns]").astype("int64")


def load_prices(category, start, end, csv_map, bar_hours, csv_shift_h) -> pd.Series:
    """Series: индекс = момент, когда цена стала известна (конец бара), значения = Close."""
    if category in csv_map:
        df = pd.read_csv(csv_map[category])
        tcol = df.columns[0]
        ccol = next((c for c in df.columns if str(c).lower() in ("close", "c", "price")), df.columns[-1])
        idx = pd.DatetimeIndex(pd.to_datetime(df[tcol], utc=True)) - pd.Timedelta(hours=csv_shift_h)
        s = pd.Series(df[ccol].astype(float).to_numpy(), index=idx)
    else:
        import yfinance as yf
        asset = ASSETS[category]
        df = yf.download(asset["ticker"],
                         start=(start - pd.Timedelta(days=2)).strftime("%Y-%m-%d"),
                         end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                         interval="1h", progress=False, auto_adjust=False)
        if df is None or df.empty:
            raise RuntimeError(f"yfinance не вернул часовые данные по {asset['ticker']}")
        close = df["Close"]
        if isinstance(close, pd.DataFrame):          # новые версии yfinance дают MultiIndex-колонки
            close = close.iloc[:, 0]
        idx = pd.DatetimeIndex(close.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        s = pd.Series(close.astype(float).to_numpy(), index=idx)
        bar_hours = 1.0
    s = s.dropna()
    s.index = s.index + pd.Timedelta(hours=bar_hours)        # цена известна в КОНЦЕ бара
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s) < 50:
        raise RuntimeError(f"слишком мало ценовых баров: {len(s)}")
    return s


def price_at(bar_end_ns, prices, times_ns, max_stale_ns):
    """Последняя известная цена на каждый момент; NaN, если свежего бара нет (рынок закрыт / выходные)."""
    pos = np.searchsorted(bar_end_ns, times_ns, side="right") - 1
    have = pos >= 0
    safe = np.clip(pos, 0, None)
    out = np.where(have, prices[safe], np.nan)
    age = times_ns - bar_end_ns[safe]
    out[(~have) | (age > max_stale_ns)] = np.nan
    return out


def forward_returns(scores_asof, prices: pd.Series, horizon_h, kind, max_stale_h):
    """Изменение цены за horizon_h часов после момента расчёта скора, в б.п. (NaN, если рынок был закрыт)."""
    bar_end = to_ns(prices.index)
    px = prices.to_numpy(dtype=float)
    t0 = to_ns(scores_asof)
    t1 = t0 + int(horizon_h * 3600 * 1e9)
    stale = int(max_stale_h * 3600 * 1e9)
    p0, p1 = price_at(bar_end, px, t0, stale), price_at(bar_end, px, t1, stale)
    with np.errstate(divide="ignore", invalid="ignore"):
        if kind == "bps":
            return (p1 - p0) * 100.0
        return np.log(p1 / p0) * 1e4


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------

def spearman(x, y):
    return pd.Series(x).rank().corr(pd.Series(y).rank())


def block_ci(x, y, block, boots, seed=1):
    """95% доверительный интервал IC блочным бутстрэпом (блоки подряд идущих точек)."""
    n = len(x)
    if boots <= 0 or n < 3 * block:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    vals = []
    for _ in range(boots):
        starts = rng.integers(0, n - block + 1, size=nb)
        idx = (starts[:, None] + np.arange(block)[None, :]).ravel()[:n]
        vals.append(spearman(x[idx], y[idx]))
    vals = np.asarray(vals, dtype=float)
    if np.isnan(vals).all():
        return np.nan, np.nan
    lo, hi = np.nanpercentile(vals, [2.5, 97.5])
    return float(lo), float(hi)


def evaluate(sig, ret, horizon, thr, boots):
    ok = np.isfinite(sig) & np.isfinite(ret)
    sig, ret = sig[ok], ret[ok]
    n = len(sig)
    res = {"n": n, "n_eff": n / max(horizon, 1), "ic": np.nan, "ci_lo": np.nan, "ci_hi": np.nan,
           "hit": np.nan, "trades": 0, "spread": np.nan}
    if n < 20:
        return res
    res["ic"] = float(spearman(sig, ret))
    res["ci_lo"], res["ci_hi"] = block_ci(sig, ret, max(horizon, 24), boots)
    act = (np.abs(sig) >= thr) & (ret != 0)
    res["trades"] = int(act.sum())
    if act.sum() >= 10:
        res["hit"] = float((np.sign(sig[act]) == np.sign(ret[act])).mean())
    bull, bear = ret[sig >= thr], ret[sig <= -thr]
    if len(bull) >= 5 and len(bear) >= 5:
        res["spread"] = float(bull.mean() - bear.mean())
    return res


# ---------------------------------------------------------------------------
# Печать
# ---------------------------------------------------------------------------

MIN_EFF = 20      # меньше ~независимых окон — интервал ненадёжен, ★ / ▼ не ставим


def fmt_ci(r):
    if np.isnan(r["ci_lo"]):
        return "—", ""
    if r["n_eff"] < MIN_EFF:
        mark = "?"
    else:
        mark = "★" if r["ci_lo"] > 0 else ("▼" if r["ci_hi"] < 0 else "")
    return f"[{r['ci_lo']:+.2f}; {r['ci_hi']:+.2f}]", mark


def print_main_table(rows, horizons):
    head = f"{'сигнал':<18}{'гор.':>5}{'n':>6}{'~нез.':>6}{'IC':>8}  {'95% CI':<16}{'':<2}{'hit%':>6}{'сигн.':>6}{'спред б.п.':>11}"
    print(head)
    print("-" * len(head))
    for h in horizons:
        for kind in KIND_TITLES:
            r = next((x for x in rows if x["horizon"] == h and x["kind"] == kind), None)
            if r is None:
                continue
            ic = "   —  " if np.isnan(r["ic"]) else f"{r['ic']:+.3f}"
            ci, mark = fmt_ci(r)
            hit = "  —  " if np.isnan(r["hit"]) else f"{r['hit'] * 100:5.1f}"
            spread = "—" if np.isnan(r["spread"]) else f"{r['spread']:+.1f}"
            print(f"{r['signal']:<18}{h:>4}ч{r['n']:>6d}{r['n_eff']:>6.0f}{ic:>8}  {ci:<16}{mark:<2}"
                  f"{hit:>6}{r['trades']:>6d}{spread:>11}")
        print()


def print_hl_pivot(rows, kind, title, horizons, half_lives):
    print(f"IC по полупериодам — {title}")
    print(f"{'полупериод':<12}" + "".join(f"{str(h) + ' ч':>9}" for h in horizons))
    for hl in half_lives:
        cells = []
        for h in horizons:
            r = next((x for x in rows if x["hl"] == hl and x["horizon"] == h and x["kind"] == kind), None)
            cells.append("    —" if r is None or np.isnan(r["ic"]) else f"{r['ic']:+.3f}")
        print(f"{str(hl) + ' ч':<12}" + "".join(f"{c:>9}" for c in cells))
    print()


def print_sweep(sweep_rows, horizons):
    print("Сетка весов (полупериод основной). w = вес FinBERT; IC арифм. | геометр. по горизонтам")
    print(f"{'w':>5}  " + "".join(f"{'ар ' + str(h) + 'ч':>9}" for h in horizons) + "   |"
          + "".join(f"{'гм ' + str(h) + 'ч':>9}" for h in horizons))
    for w in sorted({r["w"] for r in sweep_rows}):
        def cell(kind, h):
            r = next((x for x in sweep_rows if x["w"] == w and x["kind"] == kind and x["horizon"] == h), None)
            return "    —" if r is None or np.isnan(r["ic"]) else f"{r['ic']:+.3f}"
        print(f"{w:>5.1f}  " + "".join(f"{cell('arith', h):>9}" for h in horizons) + "   |"
              + "".join(f"{cell('geo', h):>9}" for h in horizons))
    print("(при w=0/1 геометрическое всё равно требует совпадения знаков, поэтому не равно чистому Qwen/FinBERT)\n")


LEGEND = """Как читать
  IC — ранговая корреляция «скор -> движение цены через H часов». На часовых финансовых данных 0.05–0.10 уже
       заметно; 0.2 и выше почти не бывает — если получилось так много, ищите ошибку или утечку.
  ★  — 95% интервал целиком выше 0: связь отличима от шума.   ▼ — целиком ниже 0: скор работает НАОБОРОТ.
  ?  — независимых окон меньше 20: интервал ненадёжен, ★ / ▼ не ставлю (на длинных горизонтах нужны недели данных).
  «—» в CI — слишком мало данных для интервала (нужно хотя бы ~3 блока по max(H, 24) часов).
  ~нез. — грубая оценка числа независимых окон (n / H). Меньше ~30 — выводам верить нельзя, это только намёк.
  hit% — доля верных знаков среди сигналов |скор| >= порога (сигн. — сколько таких часов). Около 50% = монетка.
  спред — средний ход цены после бычьего минус после медвежьего сигнала, в б.п. (для US10Y — б.п. доходности).
Предупреждение: сравнивается много комбинаций (полупериоды × горизонты × сигналы × веса), поэтому часть ★ и
лучших весов появится случайно. Выбирайте параметры, которые хороши СОСЕДНИМИ значениями, а не один пик."""


# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Проверка скоров панели на реальных ценах",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--categories", default="general,oil,us10y,dxy", help="через запятую")
    p.add_argument("--horizons", default="6,24,72", help="горизонты, часы, через запятую")
    p.add_argument("--hl", type=float, default=float(config.PRIMARY_HL), help="основной полупериод, ч")
    p.add_argument("--w", type=float, default=config.ENSEMBLE_W_FINBERT, help="вес FinBERT (как ENSEMBLE_W_FINBERT)")
    p.add_argument("--threshold", type=float, default=config.SIGNAL_THRESHOLD, help="порог |скор| для hit%% и спреда")
    p.add_argument("--lookback-days", type=int, default=config.AGG_LOOKBACK_DAYS, help="сколько суток постов брать")
    p.add_argument("--warmup-h", type=float, default=48.0, help="сколько часов после первого поста пропустить")
    p.add_argument("--max-stale-h", type=float, default=1.5, help="цена старше этого = рынок закрыт, точка пропускается")
    p.add_argument("--boots", type=int, default=300, help="число бутстрэп-выборок (0 = без доверительных интервалов)")
    p.add_argument("--sweep", action="store_true", help="перебрать веса FinBERT/Qwen от 0 до 1 с шагом 0.1")
    p.add_argument("--csv", action="append", default=[], metavar="CAT=PATH", help="свои цены: general=path.csv")
    p.add_argument("--bar-hours", type=float, default=1.0, help="длина бара в CSV (0 = время уже и есть момент цены)")
    p.add_argument("--csv-shift-h", type=float, default=0.0, help="часов вычесть из времени CSV, чтобы получить UTC")
    p.add_argument("--out", default="validate_scores_results.csv", help="куда сохранить все результаты")
    return p.parse_args()


def main():
    args = parse_args()
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    unknown = [c for c in cats if c not in ASSETS]
    if unknown:
        sys.exit(f"Неизвестные категории: {unknown}. Доступны: {list(ASSETS)}")
    horizons = sorted({int(x) for x in args.horizons.split(",") if x.strip()})
    csv_map = dict(item.split("=", 1) for item in args.csv)
    half_lives = sorted(set(config.HALF_LIVES_H) | {args.hl})
    w = args.w

    try:
        posts_db.init_db()
    except Exception as e:                              # база могла быть занята — не критично
        print(f"(init_db: {e})")

    all_rows, sweep_rows = [], []
    for cat in cats:
        asset = ASSETS[cat]
        print("=" * 100)
        print(f"{cat} · {asset['name']} · основной полупериод {args.hl:g} ч")
        print("=" * 100)

        scores = build_scores(cat, args.lookback_days, args.warmup_h, half_lives)
        if scores is None or len(scores) < 24:
            print("Недостаточно скоренных постов (или данных меньше разогрева). Дайте конвейеру поработать "
                  "или уменьшите --warmup-h.\n")
            continue
        try:
            prices = load_prices(cat, scores["asof"].iloc[0], scores["asof"].iloc[-1], csv_map,
                                 args.bar_hours, args.csv_shift_h)
        except Exception as e:
            print(f"Не удалось получить цены: {type(e).__name__}: {e}\n"
                  f"Подсказка: yfinance/сеть, либо передайте свои цены: --csv {cat}=путь.csv\n")
            continue

        rets = {h: forward_returns(scores["asof"], prices, h, asset["kind"], args.max_stale_h) for h in horizons}
        print(f"Скоры: {scores['asof'].iloc[0]:%Y-%m-%d %H:%M} … {scores['asof'].iloc[-1]:%Y-%m-%d %H:%M} UTC, "
              f"{len(scores)} часов. Цены: {len(prices)} баров. "
              "Точек с известной ценой в начале и в конце окна: "
              + ", ".join(f"{h}ч — {int(np.isfinite(rets[h]).sum())}" for h in horizons) + "\n")

        cat_rows = []
        for hl in half_lives:
            fb, qw = scores[f"fb_{hl:g}"].to_numpy(), scores[f"qw_{hl:g}"].to_numpy()
            signals = make_signals(fb, qw, w)
            for h in horizons:
                for kind, (name, arr) in signals.items():
                    boots = args.boots if abs(hl - args.hl) < 1e-9 else 0
                    r = evaluate(arr, rets[h], h, args.threshold, boots)
                    r.update(category=cat, hl=hl, horizon=h, kind=kind, signal=name, w=w)
                    cat_rows.append(r)

        main_rows = [r for r in cat_rows if abs(r["hl"] - args.hl) < 1e-9]
        print_main_table(main_rows, horizons)
        print_hl_pivot(cat_rows, "arith", f"арифметическое {w:.0%}/{1 - w:.0%}", horizons, half_lives)
        print_hl_pivot(cat_rows, "geo", f"геометрическое {w:.0%}/{1 - w:.0%}", horizons, half_lives)
        all_rows += cat_rows

        if args.sweep:
            fb, qw = scores[f"fb_{args.hl:g}"].to_numpy(), scores[f"qw_{args.hl:g}"].to_numpy()
            cat_sweep = []
            for ww in np.round(np.arange(0.0, 1.0001, 0.1), 1):
                sigs = make_signals(fb, qw, float(ww))
                for h in horizons:
                    for kind in ("arith", "geo"):
                        r = evaluate(sigs[kind][1], rets[h], h, args.threshold, 0)
                        r.update(category=cat, hl=args.hl, horizon=h, kind=kind, signal=sigs[kind][0], w=float(ww))
                        cat_sweep.append(r)
            print_sweep(cat_sweep, horizons)
            sweep_rows += cat_sweep

    if all_rows:
        df = pd.DataFrame(all_rows + sweep_rows)
        df["sweep"] = [False] * len(all_rows) + [True] * len(sweep_rows)
        cols = ["category", "hl", "horizon", "kind", "signal", "w", "sweep", "n", "n_eff", "ic", "ci_lo", "ci_hi",
                "hit", "trades", "spread"]
        df[cols].to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"Полные результаты сохранены: {Path(args.out).resolve()}\n")
        print(LEGEND)
    else:
        print("Нет результатов: ни по одной категории не набралось данных.")


if __name__ == "__main__":
    main()