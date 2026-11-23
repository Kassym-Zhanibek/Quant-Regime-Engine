"""
Источники новостей: Telegram-каналы и RSS-ленты.

Поля:
  id          Telegram: username канала (без @).  RSS: короткий латинский идентификатор.
  kind        "tg" | "rss"
  url         только для RSS — адрес ленты
  categories  для каких категорий (general / oil / us10y / dxy) пост скорится
  gated       подмножество categories, где пост берётся ТОЛЬКО если в тексте есть ключевые слова
              категории (app/categories.py). Для узких источников (напр. нефтяной сайт) оставьте пустым,
              для широких лент заголовков (FinancialJuice, WalterBloomberg …) — обязательно, иначе
              в нефть попадут новости про акции и крипту.

  title_only  (RSS) брать только заголовок, описание игнорировать (Google News дублирует в нём заголовок + издание)

Категории акций: тикеры из app/stocks.py (NVDA, MSFT …) и "spx" («Рынок США»). Список US = все они вместе.

Чтобы добавить/убрать источник — правьте только этот файл. Проверить, что всё живое (и найти TG-каналы):
    python scripts/check_sources.py
"""
from urllib.parse import quote_plus

from app.stocks import MARKET_KEY, TICKERS, UNIVERSE

ALL = ["general", "oil", "us10y", "dxy"]
MACRO = ["general", "us10y", "dxy"]
US = [MARKET_KEY] + TICKERS                 # все категории акций: «Рынок США» + тикеры

SOURCES: list[dict] = [
    # ====================== TELEGRAM ======================
    # Уже были у вас (общий сентимент). Теперь их посты дополнительно идут в нефть / облигации / доллар по ключевым словам.
    {"id": "forexnews",        "kind": "tg", "categories": ALL, "gated": ["oil", "us10y", "dxy"]},
    {"id": "Forex_LiveStream", "kind": "tg", "categories": ALL, "gated": ["oil", "us10y", "dxy"]},
    # Новые каналы (подписаться, см. README):
    {"id": "FinancialJuice",   "kind": "tg", "categories": ALL + US, "gated": ALL + US},   # лента заголовков: макро, ФРС, нефть/ОПЕК+, геополитика
    {"id": "WalterBloomberg",  "kind": "tg", "categories": ALL + US, "gated": ALL + US},   # заголовки уровня терминала (CAPS), много шума -> только по ключевым словам
    {"id": "fxstreetforex",    "kind": "tg", "categories": ALL, "gated": ALL},   # официальный новостной канал FXStreet (валюты, ЦБ, сырьё)

    # Каналы по акциям (только акции США, только по тикерам / ключевым словам из app/stocks.py):
    {"id": "marketfeed",                "kind": "tg", "categories": US, "gated": US},     # если канал даёт и глобальный макро — замените на ALL + US
    {"id": "TheFinancialExpressOnline", "kind": "tg", "categories": US, "gated": US},     # индийское издание: берём только то, что про наши тикеры / рынок США
    {"id": "bcs_express",               "kind": "tg", "categories": US, "gated": US},     # русский: тикеры ищутся и по-русски (aliases_ru)
    {"id": "markettwits",               "kind": "tg", "categories": US, "gated": US},     # русский; много российских бумаг — они отсекаются фильтром по тикерам

    # ====================== RSS: ваши прежние ленты (шли в Daily NLP Features) ======================
    {"id": "forexlive_commodities", "kind": "rss", "url": "https://www.forexlive.com/feed/commodities",
     "categories": ["general", "oil"], "gated": ["oil"]},
    {"id": "forexlive_centralbank", "kind": "rss", "url": "https://www.forexlive.com/feed/centralbank",
     "categories": MACRO, "gated": ["us10y", "dxy"]},
    {"id": "investing_commodities", "kind": "rss", "url": "https://www.investing.com/rss/news_11.rss",
     "categories": ["general", "oil"], "gated": ["oil"]},
    {"id": "investing_centralbanks", "kind": "rss", "url": "https://www.investing.com/rss/central_banks.rss",
     "categories": MACRO, "gated": ["us10y", "dxy"]},
    {"id": "cnbc_finance", "kind": "rss", "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
     "categories": MACRO + US, "gated": ["us10y", "dxy"] + US},
    {"id": "wsj_markets", "kind": "rss", "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
     "categories": MACRO + US, "gated": ["us10y", "dxy"] + US},
    {"id": "cnbc_commodities", "kind": "rss",
     "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000839",
     "categories": ["general", "oil"], "gated": ["oil"]},

    # ====================== RSS: НЕФТЬ ======================
    {"id": "oilprice_main", "kind": "rss", "url": "https://oilprice.com/rss/main",
     "categories": ["oil", "general"], "gated": ["general"]},
    {"id": "eia_today_in_energy", "kind": "rss", "url": "https://www.eia.gov/rss/todayinenergy.xml",
     "categories": ["oil"], "gated": ["oil"]},
    {"id": "cnbc_energy", "kind": "rss", "url": "https://www.cnbc.com/id/19836768/device/rss/rss.html",
     "categories": ["oil", "general"], "gated": ["general"]},
    {"id": "rigzone_latest", "kind": "rss", "url": "https://www.rigzone.com/news/rss/rigzone_latest.aspx",
     "categories": ["oil"], "gated": ["oil"]},

    # ====================== RSS: ОБЛИГАЦИИ / ДОЛЛАР ======================
    {"id": "fed_monetary", "kind": "rss", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml",
     "categories": MACRO, "gated": []},                      # решения ФРС — всегда по делу
    {"id": "fed_speeches", "kind": "rss", "url": "https://www.federalreserve.gov/feeds/speeches.xml",
     "categories": MACRO, "gated": []},
    {"id": "ecb_press", "kind": "rss", "url": "https://www.ecb.europa.eu/rss/press.html",
     "categories": ["dxy", "general"], "gated": ["general"]},
    {"id": "investing_forex", "kind": "rss", "url": "https://www.investing.com/rss/news_1.rss",
     "categories": ["dxy", "general"], "gated": ["dxy", "general"]},
    {"id": "investing_economy", "kind": "rss", "url": "https://www.investing.com/rss/news_14.rss",
     "categories": MACRO, "gated": MACRO},
    {"id": "investing_indicators", "kind": "rss", "url": "https://www.investing.com/rss/news_95.rss",
     "categories": MACRO, "gated": MACRO},
    {"id": "fxstreet_news", "kind": "rss", "url": "https://www.fxstreet.com/rss/news",
     "categories": ALL, "gated": ALL},

    # ====================== RSS: АКЦИИ США — широкие ленты (по ключевым словам: тикеры + «Рынок США») ======================
    # Часть лент кормит и золото (макро-новости): так общий фон богаче. Фильтр по словам оставляет только нужное.
    {"id": "bloomberg_markets", "kind": "rss", "url": "https://feeds.bloomberg.com/markets/news.rss",
     "categories": ALL + US, "gated": ALL + US},
    {"id": "bloomberg_technology", "kind": "rss", "url": "https://feeds.bloomberg.com/technology/news.rss",
     "categories": US, "gated": US},
    {"id": "marketwatch_top", "kind": "rss", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
     "categories": ALL + US, "gated": ALL + US},
    {"id": "marketwatch_realtime", "kind": "rss", "url": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
     "categories": ALL + US, "gated": ALL + US},
    {"id": "marketwatch_pulse", "kind": "rss", "url": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
     "categories": US, "gated": US},
    {"id": "cnbc_top", "kind": "rss", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
     "categories": ALL + US, "gated": ALL + US},
    {"id": "cnbc_earnings", "kind": "rss", "url": "https://www.cnbc.com/id/15839135/device/rss/rss.html",
     "categories": US, "gated": US},
    {"id": "cnbc_technology", "kind": "rss", "url": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
     "categories": US, "gated": US},
    {"id": "cnbc_investing", "kind": "rss", "url": "https://www.cnbc.com/id/15839069/device/rss/rss.html",
     "categories": US, "gated": US},
    {"id": "wsj_technology", "kind": "rss", "url": "https://feeds.a.dj.com/rss/RSSWSJD.xml",
     "categories": US, "gated": US},
    {"id": "wsj_business", "kind": "rss", "url": "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
     "categories": US, "gated": US},
    {"id": "investing_stocks", "kind": "rss", "url": "https://www.investing.com/rss/news_25.rss",
     "categories": US, "gated": US},
    {"id": "yahoo_finance", "kind": "rss", "url": "https://yahoo.com",
     "categories": US, "gated": US},
    {"id": "seekingalpha_currents", "kind": "rss", "url": "https://seekingalpha.com/market_currents.xml",
     "categories": US, "gated": US},
    {"id": "benzinga", "kind": "rss", "url": "https://www.benzinga.com/feed",
     "categories": US, "gated": US},
]

# ====================== RSS: АКЦИИ США — персональные ленты по каждому тикеру (без фильтра: лента и так про компанию) ======================
#   yahoo_<TICKER>  — Yahoo Finance: новости и пресс-релизы по тикеру
#   gnews_<TICKER>  — Google News: агрегатор (Reuters, Bloomberg, WSJ, CNBC, Barron's …) за последние 7 суток
# ====================== ВСЕ прежние золотые источники тоже питают акции — но только по тикерам / ключевым словам ======================
# Золотые категории у них остаются как были. Добавляется лишь gated-маршрут в тикеры и «Рынок США»: если в посте про золото
# или нефть мелькнёт «Nvidia» или «Exxon», он попадёт и в эту акцию (и будет оценён фильтром акций, а не золота).
for _s in SOURCES:
    if not set(US) & set(_s["categories"]):
        _s["categories"] = _s["categories"] + US
        _s["gated"] = list(_s.get("gated", [])) + US

for _t in UNIVERSE:
    SOURCES.append({"id": f"yahoo_{_t['key']}", "kind": "rss", "categories": [_t["key"]], "gated": [],
                    "url": f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={_t['key']}&region=US&lang=en-US"})
    SOURCES.append({"id": f"gnews_{_t['key']}", "kind": "rss", "categories": [_t["key"]], "gated": [],
                    "title_only": True,
                    "url": f"https://news.google.com/rss/search?q={quote_plus(_t['query'] + ' when:7d')}"
                           f"&hl=en-US&gl=US&ceid=US:en"})


def by_id() -> dict:
    return {s["id"]: s for s in SOURCES}


def _enabled(s: dict) -> bool:
    """Источник нужен, пока хотя бы одна его категория включена (при STOCKS_ENABLED=0 лишние ленты не качаем)."""
    from app.categories import CATEGORIES
    return any(c in CATEGORIES for c in s["categories"])


def tg_sources() -> list[dict]:
    return [s for s in SOURCES if s["kind"] == "tg" and _enabled(s)]


def rss_sources() -> list[dict]:
    return [s for s in SOURCES if s["kind"] == "rss" and _enabled(s)]


def source_key(s: dict) -> str:
    """Ключ источника в таблице posts: для Telegram — username, для RSS — 'rss:<id>'."""
    return s["id"] if s["kind"] == "tg" else f"rss:{s['id']}"


def by_key() -> dict:
    return {source_key(s): s for s in SOURCES}


def categories_for(source: dict, clean_text: str) -> list[str]:
    """В какие категории попадает пост этого источника (с учётом фильтра по ключевым словам)."""
    from app.categories import CATEGORIES, matches_keywords
    out = []
    for cat in source["categories"]:
        if cat not in CATEGORIES:                 # категория выключена (STOCKS_ENABLED=0)
            continue
        if cat in source.get("gated", []) and not matches_keywords(cat, clean_text):
            continue
        out.append(cat)
    return out

