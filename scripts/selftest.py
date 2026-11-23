"""
Офлайн-проверка «акционной» части (без сети, GPU, Ollama и Telegram) — запускать после любых правок:

    python scripts/selftest.py

Проверяет:
  * конфигурацию: у каждой категории есть промпт и рабочее регулярное выражение, источники ссылаются на существующие категории;
  * маршрутизацию: какие тестовые заголовки в какие категории попадают;
  * хранилище и скоринг на ВРЕМЕННОЙ базе: дубль не отнимает новость у тикера, затухание и итоговый скор считаются,
    сводка «Акции» собирается. Ваша рабочая база не затрагивается;
  * два фильтра важности (золото / акции) не мешают друг другу, исходный промпт золота не изменён;
  * русские посты: тикеры находятся по-русски, FinBERT для них пропускается, итог считается по Qwen;
  * оценка Telegram-канала (analyse из scripts/check_sources.py) на искусственных постах.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import categories, sources, stocks                       # noqa: E402
from app.categories import CATEGORIES, STOCK_ORDER                # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def test_config() -> None:
    print("1. Конфигурация")
    check(len(set(stocks.TICKERS)) == len(stocks.TICKERS), f"тикеры уникальны ({', '.join(stocks.TICKERS)})")
    for key in categories.ALL_ORDER:
        c = CATEGORIES[key]
        check(bool(c["prompt"].strip()) and "U, D или N" in c["prompt"], f"[{key}] промпт и формат ответа U/D/N")
    ids = [s["id"] for s in sources.SOURCES]
    check(len(ids) == len(set(ids)), f"id источников уникальны ({len(ids)} шт.)")
    bad = [(s["id"], c) for s in sources.SOURCES for c in s["categories"] + s.get("gated", []) if c not in CATEGORIES]
    check(not bad, f"источники ссылаются только на существующие категории {bad or ''}")
    bad = [s["id"] for s in sources.SOURCES if not set(s.get("gated", [])) <= set(s["categories"])]
    check(not bad, f"gated ⊆ categories у всех источников {bad or ''}")
    bad = [t for t in stocks.TICKERS if not any(t in s["categories"] and not s["gated"] for s in sources.SOURCES)]
    check(not bad, f"у каждого тикера есть персональная (негейтированная) лента {bad or ''}")


ROUTING = [   # (источник, заголовок, ожидаемые категории — все должны присутствовать, запрещённые категории)
    ("bloomberg_markets", "Nvidia's Blackwell demand surges as hyperscalers lift AI capex", {"NVDA"}, {"AAPL", "oil"}),
    ("bloomberg_markets", "Apple shares slip after iPhone sales miss estimates in China", {"AAPL"}, {"NVDA", "XOM"}),
    ("bloomberg_markets", "Exxon, Chevron profits fall as crude slides to two-month low", {"XOM", "oil"}, {"JPM"}),
    ("bloomberg_markets", "Fed's Powell signals rate cut as Treasury yields drop", {"general", "us10y", "spx"}, {"NVDA"}),
    ("cnbc_earnings", "Lilly's Zepbound sales beat estimates, raises full-year guidance", {"LLY"}, {"NVDA"}),
    ("cnbc_earnings", "JPMorgan's Dimon warns on credit quality as big banks report", {"JPM"}, {"AAPL"}),
    ("cnbc_top", "Local bakery wins award for best croissant", set(), set(stocks.TICKERS) | {"spx"}),
    ("FinancialJuice", "GOLD HITS RECORD HIGH AS DOLLAR WEAKENS", {"general", "dxy"}, set(stocks.TICKERS)),
    ("yahoo_NVDA", "3 reasons to hold on to this chip stock", {"NVDA"}, {"AAPL"}),   # персональная лента — без фильтра
    # русскоязычные каналы: тикеры находятся по-русски, российские бумаги и индийский рынок — отсекаются
    ("bcs_express", "Nvidia отчиталась лучше прогнозов, акции растут в премаркете", {"NVDA"}, {"AAPL"}),
    ("markettwits", "Эппл представила новые айфоны, бумаги растут", {"AAPL"}, {"NVDA"}),
    ("bcs_express", "Майкрософт и Амазон наращивают инвестиции в дата-центры", {"MSFT", "AMZN"}, {"NVDA"}),
    ("markettwits", "ФРС снизила ставку, индекс S&P 500 обновил рекорд", {"spx"}, set(stocks.TICKERS)),
    ("markettwits", "Сбербанк повысил прогноз по прибыли на 2026 год", set(), set(stocks.TICKERS) | {"spx"}),
    ("TheFinancialExpressOnline", "Sensex ends higher, Nifty above 25,000 on bank buying", set(), set(stocks.TICKERS) | {"spx"}),
    ("marketfeed", "Eli Lilly's Zepbound sales beat estimates", {"LLY"}, {"NVDA"}),
    # прежние золотые источники теперь тоже питают акции — но только по тикерам, а золотые категории остались как были
    ("forexnews", "Gold slips as Exxon and Chevron earnings lift oil majors", {"general", "oil", "XOM"}, {"NVDA"}),
    ("fxstreetforex", "EUR/USD steady ahead of ECB decision", {"general", "dxy"}, set(stocks.TICKERS)),
    ("forexlive_commodities", "Apple supplier warns on iPhone demand", {"AAPL"}, {"XOM"}),
]


def test_routing() -> None:
    print("2. Маршрутизация заголовков")
    by_id = sources.by_id()
    for sid, title, want, forbid in ROUTING:
        got = set(sources.categories_for(by_id[sid], title))
        check(want <= got and not (forbid & got), f"{sid}: «{title[:52]}» -> {sorted(got)}")


def test_pipeline_logic() -> None:
    print("3. Хранилище и скоринг на временной базе")
    from app.storage import db
    tmp = tempfile.TemporaryDirectory()
    db.DB_PATH = str(Path(tmp.name) / "selftest.db")
    from app.scoring import service
    from app.storage import posts_db

    db.init_db()
    posts_db.init_db()
    now = datetime.now(timezone.utc)

    def add(source: str, pid: str, text: str, hours_ago: float, fb: float, qw_by_cat: dict) -> None:
        ts = posts_db.utc_str(now - timedelta(hours=hours_ago))
        posts_db.insert_posts([{"source": source, "post_id": pid, "kind": "rss", "posted_at": ts, "title": text,
                                "text": text, "clean_text": text, "hashtags": None, "url": None}])
        posts_db.set_verdict(source, pid, 1, "stocks")
        posts_db.save_finbert([(0.5, 0.1, 0.4, fb, source, pid)])
        for cat, q in qw_by_cat.items():
            conn = db.get_connection()
            with conn:
                conn.execute("INSERT OR IGNORE INTO post_cat (source, post_id, category) VALUES (?,?,?)", (source, pid, cat))
            conn.close()
            posts_db.save_qwen(cat, [(0, 0, 0, q, source, pid)])

    # Одна и та же новость «Nvidia and Microsoft …» пришла из двух персональных лент: сначала из ленты MSFT, позже из ленты NVDA.
    same = "Nvidia and Microsoft expand AI cloud partnership, shares rise"
    add("rss:yahoo_MSFT", "a1", same, 5, 0.6, {"MSFT": 0.8})
    add("rss:yahoo_NVDA", "b1", same, 4, 0.6, {"NVDA": 0.7})
    add("rss:yahoo_NVDA", "b2", "Nvidia faces new export curbs on AI chips to China", 2, -0.5, {"NVDA": -0.9})
    add("rss:yahoo_NVDA", "b3", "Analysts raise Nvidia price targets after strong demand", 1, 0.7, {"NVDA": 0.6})

    posts_db.mark_duplicates()
    conn = db.get_connection()
    dup = conn.execute("SELECT is_dup FROM posts WHERE source='rss:yahoo_NVDA' AND post_id='b1'").fetchone()[0]
    cats = {r[0] for r in conn.execute("SELECT category FROM post_cat WHERE source='rss:yahoo_MSFT' AND post_id='a1'")}
    conn.close()
    check(dup == 1, "поздний дубль помечен is_dup")
    check(cats == {"MSFT", "NVDA"}, f"категории дубля перенесены на оригинал (теперь {sorted(cats)})")
    check(not posts_db.pending_qwen("MSFT"), "для MSFT у оригинала Qwen уже есть")
    check(len(posts_db.pending_qwen("NVDA")) == 1, "у оригинала появилась очередь Qwen для NVDA (новая категория)")
    posts_db.save_qwen("NVDA", [(0, 0, 0, 0.7, "rss:yahoo_MSFT", "a1")])

    snap = service.build_snapshot("NVDA")
    check(snap["has_data"], "build_snapshot('NVDA') видит данные")
    check(snap["half_lives"] == sorted(set(categories.half_lives_for("NVDA"))) and snap["primary_hl"] == 72,
          f"полупериоды акций {snap['half_lives']}, основной {snap['primary_hl']}")
    check(abs(snap["w_finbert"] - 0.4) < 1e-9, f"вес FinBERT для акций {snap['w_finbert']}")
    row = next(r for r in snap["rows"] if r["hl"] == 72)
    w = snap["w_finbert"]
    exp = w * row["finbert"]["shrunk"] + (1 - w) * row["qwen"]["shrunk"]
    check(abs(row["combined"]["shrunk"] - exp) < 1e-9, f"combined = {w}·FB + {1 - w:.1f}·QW ({row['combined']['shrunk']:+.3f})")
    gold = service.build_snapshot("general")
    check(abs(gold["w_finbert"] - 0.3) < 1e-9 and gold["half_lives"] == [3, 12, 24, 72] and gold["primary_hl"] == 24,
          "золото не изменилось: 0.3/0.7, полупериоды 3/12/24/72, основной 24")

    series = service.build_series("NVDA", 240)
    check(len(series["points"]) > 200 and series["primary_hl"] == 72, f"ряд на 240 ч: {len(series['points'])} точек")
    quotes = {k: {"key": k, "price": 100.0, "change_pct": 1.0, "change_5d_pct": 2.0, "next_earnings": "2099-01-01",
                  "days_to_earnings": 999, "markets": []} for k in STOCK_ORDER}
    ov = service.build_stocks_overview(quotes)
    nv = next(i for i in ov["items"] if i["key"] == "NVDA")
    check(len(ov["items"]) == len(STOCK_ORDER), f"сводка «Акции»: {len(ov['items'])} карточек")
    check(set(nv["by_hl"]) == {"12", "24", "72", "120"} and nv["quote"]["price"] == 100.0, "у тикера есть скоры по всем полупериодам и котировка")
    n = service.persist_recent(48)
    check(n > 0, f"журнал sentiment_asof записан ({n} строк)")
    tmp.cleanup()


def test_judges_and_language() -> None:
    print("4. Два фильтра важности, русский текст, миграция базы")
    import sqlite3
    import ast
    from app.scoring import service, text_clean
    from app.storage import db, posts_db

    # промпты судей читаем из текста scorer.py (сам модуль тянет torch / httpx — тест должен быть лёгким и офлайн)
    tree = ast.parse((Path(__file__).resolve().parent.parent / "app" / "scoring" / "scorer.py").read_text(encoding="utf-8"))
    prompts_ = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id.startswith("JUDGE_PROMPT")}
    gold_judge, stock_judge = prompts_["JUDGE_PROMPT"], prompts_["JUDGE_PROMPT_STOCKS"]
    check("Ты — финансовый аналитик Forex. Отфильтруй текст сообщения." in gold_judge
          and "акци" not in gold_judge.lower() and "Рекламу, спам и флуд игнорируй." in gold_judge,
          "промпт судьи золота — исходный (аналитик Forex, без упоминания акций)")
    check("акциям США" in stock_judge and "{text}" in stock_judge and "{text}" in gold_judge, "у акций свой промпт судьи")

    # миграция: база, созданная ДО вкладки «Акции» (колонки is_important_stocks нет)
    tmp = tempfile.TemporaryDirectory()
    path = str(Path(tmp.name) / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE posts (source TEXT NOT NULL, post_id TEXT NOT NULL, kind TEXT NOT NULL, posted_at TEXT NOT NULL, "
              "title TEXT, text TEXT NOT NULL, clean_text TEXT, hashtags TEXT, url TEXT, norm_key TEXT, is_important INTEGER, "
              "is_dup INTEGER NOT NULL DEFAULT 0, finbert_pos REAL, finbert_neg REAL, finbert_neu REAL, finbert_score REAL, "
              "PRIMARY KEY (source, post_id))")
    c.execute("INSERT INTO posts (source, post_id, kind, posted_at, text, clean_text, is_important) "
              "VALUES ('forexnews','1','tg','2026-11-12 10:00:00','Gold up','Gold up',1)")
    c.commit(); c.close()
    db.DB_PATH = path
    posts_db.init_db()
    conn = db.get_connection()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    old_verdict = conn.execute("SELECT is_important, is_important_stocks FROM posts").fetchone()
    conn.close()
    check("is_important_stocks" in cols, "миграция: колонка is_important_stocks добавлена в старую базу")
    check(tuple(old_verdict) == (1, None), "миграция: прежний вердикт золота сохранён, у акций пока не проверен")
    tmp.cleanup()

    # свежая база
    tmp = tempfile.TemporaryDirectory()
    db.DB_PATH = str(Path(tmp.name) / "fresh.db")
    db.init_db(); posts_db.init_db()
    now = datetime.now(timezone.utc)

    def put(source: str, pid: str, text: str, hours_ago: float = 1.0) -> None:
        ts = posts_db.utc_str(now - timedelta(hours=hours_ago))
        clean, tags = text_clean.clean_telegram_message(source, text)
        posts_db.insert_posts([{"source": source, "post_id": pid, "kind": "tg", "posted_at": ts, "title": None, "text": text,
                                "clean_text": clean, "hashtags": tags, "url": None}])

    put("forexnews", "1", "Gold steady as Nvidia surges on AI demand")           # золотой канал: general + NVDA
    put("bcs_express", "2", "Nvidia отчиталась лучше прогнозов, акции растут")     # русский, только акции
    put("Forex_LiveStream", "3", "USD/JPY edges higher ahead of BoJ")           # только золото
    posts_db.ensure_memberships()
    gold = {r[:2] for r in posts_db.pending_judge("gold")}
    stk = {r[:2] for r in posts_db.pending_judge("stocks")}
    check(gold == {("forexnews", "1"), ("Forex_LiveStream", "3")}, "очередь судьи ЗОЛОТА: только посты золотых категорий")
    check(stk == {("forexnews", "1"), ("bcs_express", "2")}, "очередь судьи АКЦИЙ: только посты тикеров")
    check(len(posts_db.pending_judge("stocks", limit=1)) == 1, "лимит очереди акций работает")

    posts_db.set_verdict("forexnews", "1", 0, "gold")       # золотой фильтр: мусор
    posts_db.set_verdict("forexnews", "1", 1, "stocks")     # фильтр акций: важно
    posts_db.set_verdict("bcs_express", "2", 1, "stocks")
    posts_db.set_verdict("Forex_LiveStream", "3", 1, "gold")
    check([r[:2] for r in posts_db.pending_qwen("NVDA")].count(("forexnews", "1")) == 1,
          "пост, отклонённый фильтром золота, всё равно идёт в NVDA (решает фильтр акций)")
    check(("forexnews", "1") not in {r[:2] for r in posts_db.pending_qwen("general")}, "…и НЕ попадает в золото")
    check(("Forex_LiveStream", "3") in {r[:2] for r in posts_db.pending_qwen("general")}, "золотой пост по-прежнему идёт в золото")
    check(("bcs_express", "2") not in {r[:2] for r in posts_db.pending_qwen("general")}, "акционный пост в золото не попадает")

    # FinBERT: русский пропускается, английский ждёт очереди
    pend = {r[:2] for r in posts_db.pending_finbert()}
    check(("bcs_express", "2") in pend and ("forexnews", "1") in pend, "до обработки оба поста в очереди FinBERT")
    check(text_clean.cyrillic_share("Nvidia отчиталась лучше прогнозов") >= config_threshold(), "русский текст распознаётся")
    posts_db.save_finbert([(None, None, 1.0, None, "bcs_express", "2")])        # то, что пишет pipeline для русского поста
    posts_db.save_finbert([(0.6, 0.1, 0.3, 0.5, "forexnews", "1")])
    left = {r[:2] for r in posts_db.pending_finbert()}
    check(("bcs_express", "2") not in left and ("forexnews", "1") not in left,
          "после «пропуска» русский пост не зависает в очереди FinBERT")
    check(left == {("Forex_LiveStream", "3")}, "в очереди остался только необработанный золотой пост")

    # скоринг: у русского поста только Qwen, итог считается
    posts_db.save_qwen("NVDA", [(0.8, 0.1, 0.1, 0.7, "bcs_express", "2"), (0.7, 0.1, 0.2, 0.6, "forexnews", "1")])
    snap = service.build_snapshot("NVDA")
    row = next(r for r in snap["rows"] if r["hl"] == 72)
    check(snap["has_data"] and row["qwen"]["shrunk"] > 0, f"NVDA: Qwen по русскому и английскому посту {row['qwen']['shrunk']:+.3f}")
    check(snap["counts"]["finbert_n"] == 1 and snap["counts"]["qwen_n"] == 2,
          f"FinBERT усреднил только английский пост (n={snap['counts']['finbert_n']}), Qwen — оба (n={snap['counts']['qwen_n']})")
    cnt = posts_db.counts_category("NVDA")
    check(cnt["important"] == 2 and cnt["pending_finbert"] == 0, f"счётчики категории акций: важных {cnt['important']}")
    g = posts_db.counts_global()
    check(g["total"] == 3, f"глобальные счётчики считаются ({g['total']} постов, в очереди фильтра {g['pending_judge']})")
    tmp.cleanup()


def config_threshold() -> float:
    from app import config
    return config.CYRILLIC_THRESHOLD


def test_channel_analysis() -> None:
    print("5. Оценка Telegram-канала (check_sources.analyse)")
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_sources", Path(__file__).resolve().parent / "check_sources.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    now = datetime.now(timezone.utc)

    def msgs(texts):
        return [(now - timedelta(hours=2 * i), t) for i, t in enumerate(texts)]

    good = cs.analyse(msgs(["Nvidia beats estimates, raises guidance", "Apple iPhone sales miss in China", "Fed signals rate cut, S&P 500 rallies",
                            "Exxon profit falls as crude slides", "Local football results", "Lilly Zepbound data positive",
                            "Microsoft Azure growth accelerates", "Weather forecast for Monday"] * 6), "x")
    check(good["verdict"].startswith("✅") and good["stock_hit"] > 0.5, f"хороший канал: {good['verdict']} (по тикерам {good['stock_hit'] * 100:.0f}%)")
    promo = cs.analyse(msgs(["VIP signals! Join our premium channel now", "Nvidia 🚀 subscribe for free signals", "Промокод на бонус, подписывайтесь",
                             "Join us: copy trading, giveaway today"] * 10), "y")
    check(promo["verdict"].startswith("🚫"), f"рекламный канал отсекается: {promo['verdict']}")
    off = cs.analyse(msgs(["Sensex ends higher, Nifty above 25,000", "Reliance shares gain on Q2 results", "RBI keeps repo rate unchanged"] * 10), "z")
    check(off["stock_hit"] == 0 and off["verdict"].startswith("⚠️"), f"канал не про наши акции: {off['verdict']}")
    check(cs.norm_channel("https://t.me/markettwits") == "markettwits" and cs.norm_channel("@marketfeed") == "marketfeed", "ссылки t.me нормализуются")


if __name__ == "__main__":
    print(f"Категории: {', '.join(categories.ALL_ORDER)}\n")
    test_config()
    test_routing()
    test_pipeline_logic()
    test_judges_and_language()
    test_channel_analysis()
    print("\n" + ("❌ ЕСТЬ ОШИБКИ: " + "; ".join(FAILS) if FAILS else "✅ Всё в порядке"))
    sys.exit(1 if FAILS else 0)
