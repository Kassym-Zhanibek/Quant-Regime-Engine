"""
Категории панели: общий сентимент, нефть, гос. облигации (US10Y), индекс доллара (DXY).

Для каждой категории задано:
  prompt        — системный промпт Qwen (app/scoring/prompts.py)
  keywords      — регулярное выражение для «ключевых слов»: им фильтруются посты ШИРОКИХ источников
                  (источник помечен в app/sources.py как gated для этой категории)
  markets       — какие котировки показывать на карточке (yfinance)
  up/down_label — как читать знак скора на панели (U — «растёт», D — «падает»)
"""
import re

from app import config, stocks
from app.scoring import prompts

# ORDER — только ЗОЛОТО (4 карточки главной страницы), как и раньше. Акции — отдельная вкладка: STOCK_ORDER.
# ALL_ORDER — все категории, по которым конвейер считает скоры (Qwen, агрегация, журнал sentiment_asof).
ORDER = ["general", "oil", "us10y", "dxy"]

CATEGORIES: dict[str, dict] = {
    "general": {
        "label": "Общий сентимент",
        "subtitle": "Золото (XAU/USD) и макрофон",
        "accent": "amber",
        "icon": "globe",
        "prompt": prompts.GENERAL,
        "up_label": "Бычий для золота",
        "down_label": "Медвежий для золота",
        "markets": [],
        "keywords": (
            r"gold|xau|bullion|safe[- ]haven|\bfed\b|fomc|powell|federal reserve|rate (cut|hike)|interest rate|"
            r"inflation|\bcpi\b|\bpce\b|\bppi\b|payrolls?|\bnfp\b|jobless|treasur(y|ies)|\byields?\b|dollar|\bdxy\b|"
            r"central bank|\becb\b|\bboj\b|\bboe\b|geopolit|\bwars?\b|sanction|tariff|recession|\bopec\b|\boil\b|crude"
        ),
    },
    "oil": {
        "label": "Нефть",
        "subtitle": "WTI / Brent",
        "accent": "orange",
        "icon": "droplet",
        "prompt": prompts.OIL,
        "up_label": "Цена нефти растёт",
        "down_label": "Цена нефти падает",
        "markets": [
            {"name": "WTI", "ticker": "CL=F", "kind": "price"},
            {"name": "Brent", "ticker": "BZ=F", "kind": "price"},
        ],
        "keywords": (
            r"\boil\b|crude|brent|\bwti\b|\bopec\+?|barrels?|\bbpd\b|petroleum|gasoline|diesel|refiner\w*|shale|"
            r"rig count|baker hughes|\beia\b|cushing|\bspr\b|\blng\b|natural gas|hormuz|tanker|pipeline|"
            r"energy (price|market|supply|crisis)|red sea|houthi|sanction"
        ),
    },
    "us10y": {
        "label": "Гос. облигации",
        "subtitle": "Доходность US10Y",
        "accent": "purple",
        "icon": "landmark",
        "prompt": prompts.US10Y,
        "up_label": "Доходность растёт",
        "down_label": "Доходность падает",
        "markets": [{"name": "US10Y", "ticker": "^TNX", "kind": "yield"}],
        "keywords": (
            r"treasur(y|ies)|\byields?\b|\bbonds?\b|10-?y(ea)?r|\bfomc\b|\bfed\b|federal reserve|powell|waller|"
            r"rate (cut|hike|decision)|interest rates?|inflation|\bcpi\b|\bpce\b|\bppi\b|payrolls?|\bnfp\b|"
            r"jobless|unemployment|auction|term premium|quantitative|\bqt\b|\bqe\b|balance sheet|debt ceiling|"
            r"deficit|bessent|\bpmi\b|\bism\b|retail sales|\bgdp\b"
        ),
    },
    "dxy": {
        "label": "Индекс доллара",
        "subtitle": "DXY",
        "accent": "emerald",
        "icon": "dollar-sign",
        "prompt": prompts.DXY,
        "up_label": "Доллар крепнет",
        "down_label": "Доллар слабеет",
        "markets": [{"name": "DXY", "ticker": "DX-Y.NYB", "kind": "pct"}],
        "keywords": (
            r"dollar|\bdxy\b|\busd\b|greenback|\bfed\b|\bfomc\b|powell|federal reserve|\becb\b|lagarde|\bboj\b|ueda|"
            r"\bboe\b|bailey|\bsnb\b|\beuro\b|\beur\b|\byen\b|\bjpy\b|sterling|\bgbp\b|\bfranc\b|\bchf\b|payrolls?|"
            r"\bnfp\b|\bcpi\b|tariffs?|intervention|currenc(y|ies)|treasur(y|ies)|\byields?\b|rate (cut|hike)"
        ),
    },
}

for _c in CATEGORIES.values():
    _c.setdefault("group", "gold")

if config.STOCKS_ENABLED:
    CATEGORIES.update(stocks.build_categories())
STOCK_ORDER = [k for k in stocks.ORDER if k in CATEGORIES]
ALL_ORDER = ORDER + STOCK_ORDER

_COMPILED = {k: re.compile(v["keywords"], re.IGNORECASE) for k, v in CATEGORIES.items()}


def half_lives_for(key: str) -> list:
    """Полупериоды затухания категории: у акций свои (config.STOCK_HALF_LIVES_H), у золота — config.HALF_LIVES_H."""
    return sorted(set(CATEGORIES[key].get("half_lives") or config.HALF_LIVES_H))


def primary_hl_for(key: str) -> float:
    return CATEGORIES[key].get("primary_hl") or config.PRIMARY_HL


def w_finbert_for(key: str) -> float:
    w = CATEGORIES[key].get("w_finbert")
    return config.ENSEMBLE_W_FINBERT if w is None else w


def matches_keywords(category: str, text: str) -> bool:
    return bool(_COMPILED[category].search(text or ""))


def public_info(key: str) -> dict:
    """Что отдаём фронтенду (без промпта и регулярок)."""
    c = CATEGORIES[key]
    return {
        "key": key, "label": c["label"], "subtitle": c["subtitle"], "accent": c["accent"], "icon": c["icon"],
        "up_label": c["up_label"], "down_label": c["down_label"],
        "group": c.get("group", "gold"), "name": c.get("name"), "sector": c.get("sector"),
    }

