"""
Вселенная акций для СВИНГ-торговли (США, высоколиквидные мега-кэпы из разных секторов) + категория «Рынок США».

Каждый тикер — это ещё одна «категория» общего конвейера (как general / oil / us10y / dxy):
свои источники, свои ключевые слова, свой промпт Qwen, свои скоры. Хранилище и агрегация не менялись.

Чтобы добавить акцию, допишите один словарь в UNIVERSE:
  key       тикер (он же ключ категории и адрес страницы /stock/<key>)
  aliases   регулярное выражение: по нему новости ШИРОКИХ источников привязываются к компании
            (aliases_ru — то же по-русски: «Эппл», «Нвидиа», «Майкрософт» — для русскоязычных каналов)
  query     поисковая фраза для ленты Google News по этому тикеру (см. app/sources.py)
  context / up / down   подставляются в промпт Qwen (app/scoring/prompts.py -> STOCK_TEMPLATE)
Для свинга полупериоды длиннее, чем у золота: STOCK_HALF_LIVES_H, основной — STOCK_PRIMARY_HL (app/config.py).
"""
from app import config
from app.scoring import prompts

UNIVERSE: list[dict] = [
    {
        "key": "NVDA", "name": "NVIDIA", "sector": "Полупроводники / ИИ", "accent": "emerald",
        "aliases": r"nvidia|\bnvda\b|jensen huang|blackwell|vera rubin|geforce|\bcuda\b",
        "query": "NVIDIA NVDA stock",
        "aliases_ru": r"нвидиа|энвидиа|дженсен",
        "context": "лидер ускорителей (GPU) для ИИ; выручка зависит от капзатрат облачных гигантов, поставок TSMC и экспортных ограничений США/Китая.",
        "up": "рост капзатрат гиперскейлеров на ИИ, крупные заказы на ускорители, успех новых платформ, ослабление экспортных ограничений",
        "down": "сокращение капзатрат на ИИ, ужесточение экспортных ограничений, успехи конкурентов (AMD, собственные чипы гиперскейлеров), перебои поставок TSMC и памяти",
    },
    {
        "key": "MSFT", "name": "Microsoft", "sector": "Программное обеспечение / облако", "accent": "sky",
        "aliases": r"microsoft|\bmsft\b|\bazure\b|copilot|nadella|\bxbox\b|github|openai",
        "query": "Microsoft MSFT stock",
        "aliases_ru": r"майкрософт|наделл",
        "context": "облако Azure, ПО (Microsoft 365, Windows), ИИ-продукты Copilot, доля в OpenAI; драйверы — рост Azure, монетизация ИИ, капзатраты.",
        "up": "ускорение роста Azure, монетизация Copilot, крупные облачные контракты, рост маржи",
        "down": "замедление роста Azure, рост капзатрат без отдачи, проблемы в отношениях с OpenAI, антимонопольные дела, киберинциденты",
    },
    {
        "key": "AAPL", "name": "Apple", "sector": "Потребительская электроника", "accent": "slate",
        "aliases": r"\bapple\b|\baapl\b|iphone|\bipad\b|macbook|tim cook|app store|apple intelligence|vision pro",
        "query": "Apple AAPL stock",
        "aliases_ru": r"\bэпп?л|айфон|тим кук",
        "context": "iPhone (основная выручка), сервисы (App Store, подписки), Китай, цепочка поставок (Foxconn, TSMC), регуляторные риски App Store.",
        "up": "сильные продажи iPhone, рост сервисов, успех ИИ-функций, рост продаж в Китае, смягчение пошлин",
        "down": "слабый спрос на iPhone, потеря позиций в Китае, пошлины на ввоз из Азии, антимонопольные иски и ограничения App Store, проблемы поставок",
    },
    {
        "key": "AMZN", "name": "Amazon", "sector": "Интернет-торговля / облако", "accent": "orange",
        "aliases": r"amazon|\bamzn\b|\baws\b|andy jassy|prime video|\balexa\b|whole foods|kuiper",
        "query": "Amazon AMZN stock",
        "aliases_ru": r"амазон",
        "context": "облако AWS и ИИ-инфраструктура, e-commerce, реклама, логистика; драйверы — рост AWS, маржа ритейла, капзатраты, потребительский спрос.",
        "up": "ускорение роста AWS, рост операционной маржи, сильные продажи в праздничный сезон, рост рекламы",
        "down": "замедление AWS, слабый потребительский спрос, рост затрат и капзатрат, антимонопольные дела, забастовки и логистические проблемы",
    },
    {
        "key": "GOOGL", "name": "Alphabet (Google)", "sector": "Интернет-реклама / ИИ", "accent": "rose",
        "aliases": r"alphabet|google|\bgoogl?\b|youtube|waymo|pichai|deepmind",
        "query": "Alphabet Google GOOGL stock",
        "aliases_ru": r"гугл|ютуб|пичаи",
        "context": "поиск и реклама, YouTube, Google Cloud, ИИ-модели Gemini, Waymo; риски — антимонопольные дела и конкуренция ИИ-поиска.",
        "up": "рост рекламы и облака, успех ИИ-продуктов, удачные решения по антимонопольным делам, рост Waymo",
        "down": "потеря доли поиска из-за ИИ-конкурентов, антимонопольные решения (принудительные меры), замедление рекламного рынка, рост капзатрат",
    },
    {
        "key": "JPM", "name": "JPMorgan Chase", "sector": "Финансы / банки", "accent": "indigo",
        "aliases": r"jpmorgan|jp ?morgan|\bjpm\b|jamie dimon|big banks?|wall street banks",
        "query": "JPMorgan JPM stock",
        "aliases_ru": r"джей-? ?пи ?морган|джейми даймон|даймон",
        "context": "крупнейший банк США: чистый процентный доход, инвестбанкинг, кредитное качество, регулирование капитала; чувствителен к кривой доходности и состоянию экономики.",
        "up": "рост чистого процентного дохода и доходов инвестбанкинга, улучшение кредитного качества, смягчение требований к капиталу, байбэки и дивиденды, крутая кривая доходности",
        "down": "рост резервов под убытки и просрочки, ужесточение регулирования капитала, штрафы и иски, замедление экономики, уход ключевых менеджеров",
    },
    {
        "key": "XOM", "name": "Exxon Mobil", "sector": "Энергетика / нефть и газ", "accent": "amber",
        "aliases": r"exxon|exxonmobil|\bxom\b|darren woods|permian|guyana",
        "query": "Exxon Mobil XOM stock",
        "aliases_ru": r"эксон|экссон",
        "context": "нефтегазовая компания: прибыль зависит от цен на нефть и газ, маржи переработки, добычи (Пермь, Гайана), капзатрат и возврата капитала акционерам.",
        "up": "рост цен на нефть и газ, рост добычи и маржи переработки, байбэки и дивиденды, успешные проекты, смягчение регулирования",
        "down": "падение цен на нефть и газ, рост предложения ОПЕК+, налог на сверхприбыль, аварии и разливы, ухудшение маржи, сокращение возвратов акционерам",
    },
    {
        "key": "LLY", "name": "Eli Lilly", "sector": "Здравоохранение / фарма", "accent": "teal",
        "aliases": r"eli lilly|\blilly\b|\blly\b|zepbound|mounjaro|tirzepatide|orforglipron|retatrutide|glp-?1",
        "query": "Eli Lilly LLY stock",
        "aliases_ru": r"эли лилли|\bлилли\b|тирзепатид|мунджаро|зепбаунд",
        "context": "фармкомпания, лидер рынка препаратов GLP-1 (Mounjaro, Zepbound), онкология; драйверы — продажи препаратов, данные клинических исследований, решения FDA, конкуренция Novo Nordisk, ценовое регулирование.",
        "up": "сильные данные клинических исследований, одобрения FDA, рост продаж Mounjaro и Zepbound, расширение производства, повышение прогноза",
        "down": "неудачи клинических исследований, отказы или задержки FDA, ценовое давление и регулирование (Medicare, пошлины на лекарства), успехи конкурентов, проблемы производства",
    },
]

MARKET_KEY = "spx"
MARKET_KEYWORDS = (
    r"s&p ?500|\bspx\b|nasdaq|dow jones|\bdow\b|wall street|\bstocks?\b|equit(y|ies)|\bvix\b|earnings season|"
    r"rall(y|ies)|sell-?off|risk[- ](on|off)|bull market|bear market|correction|\bfed\b|\bfomc\b|powell|"
    r"rate (cut|hike)|interest rates?|treasur(y|ies)|\byields?\b|inflation|\bcpi\b|\bpce\b|payrolls?|\bnfp\b|"
    r"jobless|recession|tariffs?|trade (war|deal|talks)|\bgdp\b|\bism\b|\bpmi\b"
)
# русскоязычные источники (БКС Экспресс, Market Twits …): ядро тем «Рынка США» по-русски. Латиницей S&P 500 / Nasdaq / Dow уже выше.
MARKET_KEYWORDS_RU = (
    r"уолл-?стрит|\bфрс\b|пауэлл|казначейск|американск\w+ (рынок|акци|индекс|экономик)|рынк\w+ труда сша|"
    r"инфляци\w+ в сша|пошлин\w+ трамп|рецесси"
)

ORDER: list[str] = [MARKET_KEY] + [t["key"] for t in UNIVERSE]
TICKERS: list[str] = [t["key"] for t in UNIVERSE]


def build_categories() -> dict[str, dict]:
    """Словари в том же формате, что и CATEGORIES в app/categories.py (+ поля group / ticker / sector и т.п.)."""
    common = {
        "group": "stocks",
        "half_lives": list(config.STOCK_HALF_LIVES_H),
        "primary_hl": config.STOCK_PRIMARY_HL,
        "w_finbert": config.STOCK_W_FINBERT,
    }
    out: dict[str, dict] = {
        MARKET_KEY: {
            **common,
            "label": "Рынок акций США", "subtitle": "S&P 500 / Nasdaq", "accent": "cyan", "icon": "bar-chart-3",
            "name": "Рынок США", "ticker": "^GSPC", "sector": "Индексы",
            "prompt": prompts.STOCK_MARKET,
            "up_label": "Рынок растёт", "down_label": "Рынок падает",
            "markets": [
                {"name": "S&P 500", "ticker": "^GSPC", "kind": "price"},
                {"name": "Nasdaq", "ticker": "^IXIC", "kind": "price"},
                {"name": "VIX", "ticker": "^VIX", "kind": "price"},
            ],
            "keywords": MARKET_KEYWORDS + "|" + MARKET_KEYWORDS_RU,
        }
    }
    for t in UNIVERSE:
        out[t["key"]] = {
            **common,
            "label": f"{t['name']} ({t['key']})", "subtitle": t["sector"], "accent": t["accent"], "icon": "trending-up",
            "name": t["name"], "ticker": t["key"], "sector": t["sector"], "query": t["query"],
            "prompt": prompts.stock(t["name"], t["key"], t["context"], t["up"], t["down"]),
            "up_label": f"Бычий для {t['key']}", "down_label": f"Медвежий для {t['key']}",
            "markets": [],
            "keywords": t["aliases"] + ("|" + t["aliases_ru"] if t.get("aliases_ru") else ""),
        }
    return out
