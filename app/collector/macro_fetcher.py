import yfinance as yf


def fetch_macro_indicators() -> dict:
    """
    Скачивает макро-данные и считает изменения:
    DXY - относительное изменение (в долях), US10Y - изменение в базисных пунктах (bps).
    """
    try:
        dxy_data = yf.Ticker("DX-Y.NYB").history(period="7d", interval="1d")
        tnx_data = yf.Ticker("^TNX").history(period="7d", interval="1d")

        if len(dxy_data) < 2 or len(tnx_data) < 2:
            return get_fallback_data()

        dxy_prev_close = dxy_data['Close'].iloc[-2]
        dxy_live = dxy_data['Close'].iloc[-1]

        tnx_prev_close = tnx_data['Close'].iloc[-2]
        tnx_live = tnx_data['Close'].iloc[-1]

        # DXY delta в долях (например, 0.0015 для +0.15%)
        dxy_delta = (dxy_live - dxy_prev_close) / dxy_prev_close
        # Доходность 10Y в базисных пунктах (bps)
        us10y_delta_bps = (tnx_live - tnx_prev_close) * 100.0

        macro_score = max(-1.0, min(1.0, round(-(dxy_delta * 10.0 + (us10y_delta_bps / 100.0)), 4)))

        return {
            "dxy": {
                "price": round(float(dxy_live), 2),
                "change_pct": round(float(dxy_delta), 4)
            },
            "us10y_yields": {
                "yield_pct": round(float(tnx_live), 2),
                "change_bps": round(float(us10y_delta_bps), 1)
            },
            "macro_score": macro_score
        }
    except Exception as e:
        print(f"Ошибка сбора макро-данных yfinance: {e}")
        return get_fallback_data()


def get_fallback_data() -> dict:
    return {
        "dxy": {"price": 104.0, "change_pct": 0.0},
        "us10y_yields": {"yield_pct": 4.2, "change_bps": 0.0},
        "macro_score": 0.0
    }


# ---------------------------------------------------------------------------
# Котировки для карточек панели (нефть, US10Y, DXY) — отдельно от fetch_macro_indicators(),
# которая по-прежнему кормит модель Market Bias и остаётся без изменений.
# ---------------------------------------------------------------------------
import threading
import time

_MARKETS_LOCK = threading.Lock()
_MARKETS_CACHE: dict = {"ts": 0.0, "data": None}


def _quote(ticker: str, kind: str, name: str) -> dict:
    """Последняя цена и изменение к предыдущему закрытию. change_pct — в ПРОЦЕНТАХ (0.15 = +0.15%), change_bps — в б.п."""
    out = {"name": name, "ticker": ticker, "kind": kind, "price": None, "change_pct": None, "change_bps": None}
    try:
        hist = yf.Ticker(ticker).history(period="7d", interval="1d")["Close"].dropna()
        if len(hist) < 2:
            return out
        prev, last = float(hist.iloc[-2]), float(hist.iloc[-1])
        out["price"] = round(last, 3 if kind == "yield" else 2)
        if kind == "yield":
            out["change_bps"] = round((last - prev) * 100.0, 1)      # ^TNX котируется в процентах -> б.п.
        else:
            out["change_pct"] = round((last - prev) / prev * 100.0, 2)
    except Exception as e:
        print(f"Ошибка котировки {ticker}: {e}")
    return out


def get_markets() -> dict:
    """{'oil': [WTI, Brent], 'us10y': [US10Y], 'dxy': [DXY], 'general': []}; кэш MARKET_CACHE_SECONDS секунд."""
    from app import config
    from app.categories import CATEGORIES
    with _MARKETS_LOCK:
        if _MARKETS_CACHE["data"] is not None and time.time() - _MARKETS_CACHE["ts"] < config.MARKET_CACHE_SECONDS:
            return _MARKETS_CACHE["data"]
        data = {key: [_quote(m["ticker"], m["kind"], m["name"]) for m in cat["markets"]]
                for key, cat in CATEGORIES.items() if cat.get("group", "gold") == "gold"}
        _MARKETS_CACHE.update(ts=time.time(), data=data)
        return data


# ---------------------------------------------------------------------------
# Акции США: котировки для вкладки «Акции» (отдельный кэш, золотая страница их не дёргает)
# ---------------------------------------------------------------------------
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

_STOCKS_LOCK = threading.Lock()
_STOCKS_CACHE: dict = {"ts": 0.0, "data": None}
_EARN_CACHE: dict = {}                       # ticker -> (время запроса, 'YYYY-MM-DD' | None)
EARNINGS_TTL = 6 * 3600                      # дату отчёта меняют редко
EARNINGS_MISS_TTL = 30 * 60                  # не нашли — пробуем чаще


def _stock_quote(ticker: str, name: str) -> dict:
    """Цена, изменение к пред. закрытию (%) и за 5 сессий (%). При ошибке поля остаются None."""
    out = {"name": name, "ticker": ticker, "kind": "price", "price": None, "change_pct": None,
           "change_5d_pct": None, "change_bps": None}
    try:
        hist = yf.Ticker(ticker).history(period="1mo", interval="1d")["Close"].dropna()
        if len(hist) >= 2:
            last, prev = float(hist.iloc[-1]), float(hist.iloc[-2])
            out["price"] = round(last, 2)
            out["change_pct"] = round((last - prev) / prev * 100.0, 2)
            if len(hist) >= 6:
                base = float(hist.iloc[-6])
                out["change_5d_pct"] = round((last - base) / base * 100.0, 2)
    except Exception as e:
        print(f"Ошибка котировки {ticker}: {e}")
    return out


def _to_date(x):
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _next_earnings(ticker: str) -> str | None:
    """Ближайшая дата отчётности ('YYYY-MM-DD') по данным Yahoo; None, если не удалось получить (yfinance капризен)."""
    now = time.time()
    hit = _EARN_CACHE.get(ticker)
    if hit and now - hit[0] < (EARNINGS_TTL if hit[1] else EARNINGS_MISS_TTL):
        return hit[1]
    value = None
    try:
        cal = yf.Ticker(ticker).calendar
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if raw is None and cal is not None and hasattr(cal, "loc"):        # старые версии yfinance: DataFrame
            raw = cal.loc["Earnings Date"].tolist()
        if raw is not None and not isinstance(raw, (list, tuple)):
            raw = [raw]
        today = date.today()
        future = sorted(d for d in (_to_date(x) for x in (raw or [])) if d is not None and d >= today)
        value = future[0].isoformat() if future else None
    except Exception as e:
        print(f"Не получена дата отчёта {ticker}: {e}")
    _EARN_CACHE[ticker] = (now, value)
    return value


def get_stock_quotes() -> dict:
    """
    {key: {...котировка, 'markets': [...], 'next_earnings', 'days_to_earnings'}} для «Рынка США» и тикеров.
    Для 'spx' в 'markets' лежат S&P 500 / Nasdaq / VIX, для акции — её собственная котировка. Кэш MARKET_CACHE_SECONDS.
    """
    from app import config
    from app.categories import CATEGORIES, STOCK_ORDER
    with _STOCKS_LOCK:
        if _STOCKS_CACHE["data"] is not None and time.time() - _STOCKS_CACHE["ts"] < config.MARKET_CACHE_SECONDS:
            return _STOCKS_CACHE["data"]

        def one(key: str) -> dict:
            cat = CATEGORIES[key]
            if cat["markets"]:                                  # «Рынок США»: индексы
                return {"key": key, "markets": [_quote(m["ticker"], m["kind"], m["name"]) for m in cat["markets"]]}
            q = _stock_quote(cat["ticker"], cat["name"])
            earn = _next_earnings(cat["ticker"])
            days = (date.fromisoformat(earn) - date.today()).days if earn else None
            return {"key": key, **{k: q[k] for k in ("price", "change_pct", "change_5d_pct")},
                    "next_earnings": earn, "days_to_earnings": days, "markets": [q]}

        with ThreadPoolExecutor(max_workers=6) as ex:
            data = dict(zip(STOCK_ORDER, ex.map(one, STOCK_ORDER)))
        _STOCKS_CACHE.update(ts=time.time(), data=data)
        return data

