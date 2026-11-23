"""
Единое хранилище новостей из ВСЕХ источников (Telegram + RSS) и их оценок. Заменяет tg_db.py.

  posts             одна строка = одна новость. Здесь же всё, что не зависит от категории:
                    текст, вердикты Qwen-фильтра (важная / мусор), флаг дубля, оценка FinBERT.
                    Вердиктов два: is_important — фильтр ЗОЛОТА/макро (исходный промпт), is_important_stocks — фильтр
                    АКЦИЙ США (свой промпт). Золотые категории смотрят только на первый, акции — только на второй.
  post_cat          «новость × категория»: в какие категории (general / oil / us10y / dxy) новость попала
                    и оценка Qwen U/D/N для ЭТОЙ категории (у каждой категории свой промпт).
  sentiment_asof    журнал итоговых скоров (категория × момент времени).

FinBERT от категории не зависит, поэтому считается один раз на новость; Qwen — по разу на каждую категорию.
Время везде UTC 'YYYY-MM-DD HH:MM:SS'. Таблица tg_posts из прошлой версии НЕ удаляется:
при первом запуске её содержимое один раз копируется сюда (вместе со скорами).
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app import config, sources
from app.categories import CATEGORIES, ORDER, STOCK_ORDER
from app.scoring.text_clean import norm_key
from app.storage.db import get_connection

TS_FMT = "%Y-%m-%d %H:%M:%S"
ASOF_TABLE = "sentiment_asof"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    source        TEXT    NOT NULL,       -- Telegram: username канала; RSS: 'rss:<id>'
    post_id       TEXT    NOT NULL,       -- Telegram: message_id; RSS: guid / ссылка
    kind          TEXT    NOT NULL,       -- 'tg' | 'rss'
    posted_at     TEXT    NOT NULL,       -- UTC
    title         TEXT,
    text          TEXT    NOT NULL,       -- как пришло (для RSS: заголовок + описание без HTML)
    clean_text    TEXT,                   -- без ссылок / хэштегов / подписи — его и скорим
    hashtags      TEXT,
    url           TEXT,
    norm_key      TEXT,                   -- ключ точного дубля внутри суток
    is_important  INTEGER,                -- 1 / 0 — вердикт Qwen-фильтра ЗОЛОТА; NULL — ещё не проверен
    is_important_stocks INTEGER,          -- 1 / 0 — вердикт Qwen-фильтра АКЦИЙ; NULL — ещё не проверен
    is_dup        INTEGER NOT NULL DEFAULT 0,
    finbert_pos   REAL, finbert_neg REAL, finbert_neu REAL, finbert_score REAL,
    PRIMARY KEY (source, post_id)
);
CREATE INDEX IF NOT EXISTS idx_posts_time ON posts(posted_at);

CREATE TABLE IF NOT EXISTS post_cat (
    source     TEXT NOT NULL,
    post_id    TEXT NOT NULL,
    category   TEXT NOT NULL,
    qwen_bull  REAL, qwen_bear REAL, qwen_neu REAL, qwen_score REAL,
    PRIMARY KEY (source, post_id, category)
);
CREATE INDEX IF NOT EXISTS idx_post_cat_cat ON post_cat(category);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(TS_FMT)


def _ago(**kw) -> str:
    return utc_str(datetime.now(timezone.utc) - timedelta(**kw))


def _score_text_expr(alias: str = "p") -> str:
    return (f"COALESCE(NULLIF({alias}.clean_text, ''), {alias}.text)" if config.TG_SCORE_CLEANED else f"{alias}.text")


# ---------------------------------------------------------------------------
# Инициализация и миграция
# ---------------------------------------------------------------------------

def init_db() -> None:
    conn = get_connection()
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
    if "is_important_stocks" not in cols:            # база создана до появления вкладки «Акции»
        conn.execute("ALTER TABLE posts ADD COLUMN is_important_stocks INTEGER")
    conn.commit()
    conn.close()
    migrate_legacy_tg_posts()


def migrate_legacy_tg_posts() -> int:
    """Один раз переносит таблицу tg_posts прошлой версии (вместе с оценками) в posts / post_cat['general']."""
    conn = get_connection()
    try:
        done = conn.execute("SELECT value FROM meta WHERE key = 'tg_posts_migrated'").fetchone()
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_posts'").fetchone()
        if done or not has:
            return 0
        before = conn.total_changes
        with conn:
            conn.execute("""
                INSERT OR IGNORE INTO posts (source, post_id, kind, posted_at, title, text, clean_text, hashtags, url,
                    norm_key, is_important, is_dup, finbert_pos, finbert_neg, finbert_neu, finbert_score)
                SELECT channel, CAST(message_id AS TEXT), 'tg', posted_at, NULL, text, clean_text, hashtags,
                       'https://t.me/' || channel || '/' || message_id,
                       norm_key, is_important, is_dup, finbert_pos, finbert_neg, finbert_neu, finbert_score
                FROM tg_posts""")
            conn.execute("""
                INSERT OR IGNORE INTO post_cat (source, post_id, category, qwen_bull, qwen_bear, qwen_neu, qwen_score)
                SELECT channel, CAST(message_id AS TEXT), 'general', qwen_bull, qwen_bear, qwen_neu, qwen_score
                FROM tg_posts""")
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('tg_posts_migrated', ?)",
                         (utc_str(datetime.now(timezone.utc)),))
        return conn.total_changes - before
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Загрузка новостей
# ---------------------------------------------------------------------------

def insert_posts(rows: list[dict]) -> int:
    """
    rows: source, post_id, kind, posted_at, title, text, clean_text, hashtags, url.
    Уже сохранённые пропускаются. Возвращает число новых.
    """
    if not rows:
        return 0
    prepared = []
    for r in rows:
        clean = r.get("clean_text") or ""
        prepared.append((
            r["source"], str(r["post_id"]), r["kind"], r["posted_at"], r.get("title"), r["text"], clean,
            r.get("hashtags"), r.get("url"), norm_key(clean) if clean else None,
            None if clean else 0,          # после очистки ничего не осталось — это не новость
            None if clean else 0,          # (то же для фильтра акций)
        ))
    conn = get_connection()
    before = conn.total_changes
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO posts (source, post_id, kind, posted_at, title, text, clean_text, hashtags, url, "
            "norm_key, is_important, is_important_stocks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", prepared)
    added = conn.total_changes - before
    conn.close()
    return added


def last_tg_message_id(source: str) -> int | None:
    conn = get_connection()
    row = conn.execute("SELECT MAX(CAST(post_id AS INTEGER)) FROM posts WHERE source = ? AND kind = 'tg'",
                       (source,)).fetchone()
    conn.close()
    return row[0]


def import_legacy_live(src_path) -> int:
    """
    Разовый импорт из gold_market_normalized.db (таблица telegram_posts_live): текст, время и вердикт Qwen-фильтра.
    Скоры не переносятся (будут посчитаны заново). Нужен только тем, у кого tg_posts ещё не было.
    """
    import sqlite3
    from pathlib import Path
    from app.scoring.text_clean import clean_telegram_message
    p = Path(src_path)
    if not p.exists():
        return 0
    src = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (config.LEGACY_LIVE_TABLE,)).fetchone():
            return 0
        data = src.execute(f"SELECT channel, message_id, posted_at, text, is_important "
                           f"FROM {config.LEGACY_LIVE_TABLE}").fetchall()
    finally:
        src.close()
    rows, verdicts = [], {}
    for channel, mid, posted_at, text, verdict in data:
        if not text:
            continue
        clean, tags = clean_telegram_message(channel, text)
        rows.append({"source": channel, "post_id": mid, "kind": "tg", "posted_at": posted_at, "text": text,
                     "clean_text": clean, "hashtags": tags, "url": f"https://t.me/{channel}/{mid}"})
        if verdict is not None and clean:
            verdicts[(channel, str(mid))] = verdict
    added = insert_posts(rows)
    conn = get_connection()
    with conn:
        conn.executemany("UPDATE posts SET is_important = ? WHERE source = ? AND post_id = ? AND is_important IS NULL",
                         [(v, s, i) for (s, i), v in verdicts.items()])
    conn.close()
    return added


# ---------------------------------------------------------------------------
# Маршрутизация по категориям и дубли
# ---------------------------------------------------------------------------

def ensure_memberships(window_days: int | None = None) -> int:
    """
    Для постов за окно определяет, в какие категории они попадают по ТЕКУЩЕЙ конфигурации источников
    (app/sources.py: categories + gated + ключевые слова), и добавляет недостающие строки post_cat.
    Идемпотентно; заодно «доначисляет» категории старым постам, если вы добавили источнику новую категорию.
    """
    window_days = window_days or config.AGG_LOOKBACK_DAYS
    cutoff = _ago(days=window_days)
    src_map = sources.by_key()
    conn = get_connection()
    posts = conn.execute("SELECT source, post_id, clean_text, text FROM posts WHERE posted_at >= ?", (cutoff,)).fetchall()
    have = {(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT c.source, c.post_id, c.category FROM post_cat c JOIN posts p "
        "ON p.source = c.source AND p.post_id = c.post_id WHERE p.posted_at >= ?", (cutoff,))}
    new = []
    for r in posts:
        cfg = src_map.get(r["source"])
        if cfg is None:
            continue
        for cat in sources.categories_for(cfg, r["clean_text"] or r["text"]):
            if (r["source"], r["post_id"], cat) not in have:
                new.append((r["source"], r["post_id"], cat))
    if new:
        with conn:
            conn.executemany("INSERT OR IGNORE INTO post_cat (source, post_id, category) VALUES (?,?,?)", new)
    conn.close()
    return len(new)


def mark_duplicates(window_days: int | None = None) -> int:
    """
    Точные дубли внутри суток (правило step2): остаётся самый ранний пост, остальные is_dup = 1. Идемпотентно.

    Акции: одну и ту же новость отдают сразу Yahoo, Google News, CNBC, MarketWatch … и ленты разных тикеров.
    Чтобы дубль не «украл» новость у тикера, чей источник пришёл позже, категории АКЦИЙ дубля переносятся
    на оставшийся (самый ранний) пост — он оценивается Qwen и для них. Категории золота не трогаем.
    """
    cutoff = _ago(days=window_days or config.DEDUP_WINDOW_DAYS)
    conn = get_connection()
    rows = conn.execute(
        "SELECT source, post_id, posted_at, norm_key, is_dup FROM posts "
        "WHERE posted_at >= ? AND norm_key IS NOT NULL AND norm_key <> ''", (cutoff,)).fetchall()
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["posted_at"][:10], r["norm_key"])].append(r)
    updates, merges = [], []
    stock_cats = set(STOCK_ORDER)
    dup_cats: dict[tuple, list] = defaultdict(list)       # (source, post_id) дубля -> его категории акций
    if stock_cats:
        for c in conn.execute("SELECT c.source, c.post_id, c.category FROM post_cat c JOIN posts p "
                              "ON p.source = c.source AND p.post_id = c.post_id WHERE p.posted_at >= ?", (cutoff,)):
            if c["category"] in stock_cats:
                dup_cats[(c["source"], c["post_id"])].append(c["category"])
    for g in groups.values():
        g.sort(key=lambda r: (r["posted_at"], r["source"], r["post_id"]))
        for i, r in enumerate(g):
            want = 0 if i == 0 else 1
            if r["is_dup"] != want:
                updates.append((want, r["source"], r["post_id"]))
            if i > 0:
                for cat in dup_cats.get((r["source"], r["post_id"]), ()):
                    merges.append((g[0]["source"], g[0]["post_id"], cat))
    if updates or merges:
        with conn:
            if updates:
                conn.executemany("UPDATE posts SET is_dup = ? WHERE source = ? AND post_id = ?", updates)
            if merges:
                conn.executemany("INSERT OR IGNORE INTO post_cat (source, post_id, category) VALUES (?,?,?)", merges)
    conn.close()
    return len(updates)


# ---------------------------------------------------------------------------
# Очереди на обработку
# ---------------------------------------------------------------------------

_HAS_CAT = "EXISTS (SELECT 1 FROM post_cat c WHERE c.source = p.source AND c.post_id = p.post_id)"


def _cat_list(keys) -> str:
    return ", ".join("'" + k + "'" for k in keys)         # ключи — наши константы из app/categories.py, не пользовательский ввод


_GOLD_CATS = _cat_list(ORDER)
_STOCK_CATS = _cat_list(STOCK_ORDER)                      # при STOCKS_ENABLED=0 пусто: IN () в SQLite допустимо и ложно
_HAS_GOLD = (f"EXISTS (SELECT 1 FROM post_cat c WHERE c.source = p.source AND c.post_id = p.post_id "
             f"AND c.category IN ({_GOLD_CATS}))")
_HAS_STOCK = (f"EXISTS (SELECT 1 FROM post_cat c WHERE c.source = p.source AND c.post_id = p.post_id "
              f"AND c.category IN ({_STOCK_CATS}))")
# FinBERT от категории и языка фильтра не зависит; пост «пропущен» (не английский) — finbert_neu заполнен, finbert_score пуст
_FB_PENDING = "p.finbert_score IS NULL AND p.finbert_neu IS NULL"


def _imp(category: str) -> str:
    """Колонка вердикта фильтра важности, которая действует для категории: золото -> is_important, акции -> is_important_stocks."""
    return "p.is_important_stocks" if CATEGORIES[category].get("group") == "stocks" else "p.is_important"


def pending_judge(group: str = "gold", limit: int | None = None) -> list[tuple]:
    """
    Посты без вердикта фильтра важности для группы ("gold" | "stocks"; только те, что попали хотя бы в одну категорию
    ЭТОЙ группы), свежие первыми. Золотой фильтр — как раньше; пост про Nvidia из золотого канала оценивается
    фильтром акций, а не золота.
    """
    col, has = ("p.is_important_stocks", _HAS_STOCK) if group == "stocks" else ("p.is_important", _HAS_GOLD)
    conn = get_connection()
    rows = conn.execute(
        f"SELECT p.source, p.post_id, p.text FROM posts p WHERE {col} IS NULL AND p.is_dup = 0 AND {has} "
        "ORDER BY p.posted_at DESC LIMIT ?", (limit or -1,)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


def set_verdict(source: str, post_id: str, verdict: int, group: str = "gold") -> None:
    col = "is_important_stocks" if group == "stocks" else "is_important"
    conn = get_connection()
    with conn:
        conn.execute(f"UPDATE posts SET {col} = ? WHERE source = ? AND post_id = ?", (verdict, source, post_id))
    conn.close()


def pending_finbert() -> list[tuple]:
    conn = get_connection()
    rows = conn.execute(
        f"SELECT p.source, p.post_id, {_score_text_expr()} FROM posts p "
        f"WHERE (p.is_important = 1 OR p.is_important_stocks = 1) AND p.is_dup = 0 AND {_FB_PENDING} AND {_HAS_CAT} "
        "ORDER BY p.posted_at DESC").fetchall()
    conn.close()
    return [tuple(r) for r in rows]


def pending_qwen(category: str) -> list[tuple]:
    conn = get_connection()
    rows = conn.execute(
        f"SELECT c.source, c.post_id, {_score_text_expr()} FROM post_cat c "
        "JOIN posts p ON p.source = c.source AND p.post_id = c.post_id "
        f"WHERE c.category = ? AND c.qwen_score IS NULL AND {_imp(category)} = 1 AND p.is_dup = 0 "
        "ORDER BY p.posted_at DESC", (category,)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


def save_finbert(batch: list[tuple]) -> None:
    """batch: (pos, neg, neu, score, source, post_id)"""
    conn = get_connection()
    with conn:
        conn.executemany("UPDATE posts SET finbert_pos = ?, finbert_neg = ?, finbert_neu = ?, finbert_score = ? "
                         "WHERE source = ? AND post_id = ?", batch)
    conn.close()


def save_qwen(category: str, batch: list[tuple]) -> None:
    """batch: (bull, bear, neu, score, source, post_id)"""
    conn = get_connection()
    with conn:
        conn.executemany("UPDATE post_cat SET qwen_bull = ?, qwen_bear = ?, qwen_neu = ?, qwen_score = ? "
                         "WHERE source = ? AND post_id = ? AND category = ?", [(*b, category) for b in batch])
    conn.close()


# ---------------------------------------------------------------------------
# Чтение для агрегации и панели
# ---------------------------------------------------------------------------

def load_scored_posts(category: str, lookback_days: int | None = None) -> list[tuple]:
    """(posted_at, finbert_score, qwen_score) — важные, не дубли, попавшие в категорию. Любой скор может быть None."""
    cutoff = _ago(days=lookback_days or config.AGG_LOOKBACK_DAYS)
    conn = get_connection()
    rows = conn.execute(
        "SELECT p.posted_at, p.finbert_score, c.qwen_score FROM post_cat c "
        "JOIN posts p ON p.source = c.source AND p.post_id = c.post_id "
        f"WHERE c.category = ? AND {_imp(category)} = 1 AND p.is_dup = 0 AND p.posted_at >= ? ORDER BY p.posted_at",
        (category, cutoff)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


def recent_posts(category: str, limit: int = 40, hours: int | None = None) -> list[dict]:
    sql = ("SELECT p.source, p.kind, p.posted_at, p.title, p.text, p.clean_text, p.url, "
           "p.finbert_pos, p.finbert_neg, p.finbert_neu, p.finbert_score, "
           "c.qwen_bull, c.qwen_bear, c.qwen_neu, c.qwen_score FROM post_cat c "
           "JOIN posts p ON p.source = c.source AND p.post_id = c.post_id "
           f"WHERE c.category = ? AND {_imp(category)} = 1 AND p.is_dup = 0 ")
    args: list = [category]
    if hours:
        sql += "AND p.posted_at >= ? "
        args.append(_ago(hours=hours))
    sql += "ORDER BY p.posted_at DESC LIMIT ?"
    args.append(limit)
    conn = get_connection()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["display_text"] = d.pop("clean_text") or d["text"]
        d.pop("text", None)
        out.append(d)
    return out


def counts_global() -> dict:
    """Счётчики для всего конвейера. «Очередь фильтра» = золотая + акционная очередь; «мусор» — отклонённые всеми фильтрами."""
    conn = get_connection()
    r = conn.execute(
        f"""SELECT COUNT(*) AS total,
                   COALESCE(SUM(is_dup = 1), 0) AS dups,
                   COALESCE(SUM(is_dup = 0 AND (is_important IS NOT NULL OR is_important_stocks IS NOT NULL)
                                AND COALESCE(is_important, 0) = 0 AND COALESCE(is_important_stocks, 0) = 0), 0) AS trash,
                   COALESCE(SUM(is_dup = 0 AND ((is_important IS NULL AND {_HAS_GOLD.replace('p.', 'posts.')})
                                OR (is_important_stocks IS NULL AND {_HAS_STOCK.replace('p.', 'posts.')}))), 0) AS pending_judge,
                   COALESCE(SUM((is_important = 1 OR is_important_stocks = 1) AND is_dup = 0
                                AND finbert_score IS NULL AND finbert_neu IS NULL), 0) AS pending_finbert,
                   MIN(posted_at) AS first_post, MAX(posted_at) AS last_post
            FROM posts""").fetchone()
    pq = conn.execute(
        "SELECT COUNT(*) FROM post_cat c JOIN posts p ON p.source = c.source AND p.post_id = c.post_id "
        f"WHERE c.qwen_score IS NULL AND p.is_dup = 0 AND ((c.category IN ({_GOLD_CATS}) AND p.is_important = 1) "
        f"OR (c.category IN ({_STOCK_CATS}) AND p.is_important_stocks = 1))").fetchone()[0]
    conn.close()
    d = dict(r)
    d["pending_qwen"] = pq
    return d


def counts_category(category: str) -> dict:
    imp = _imp(category)
    conn = get_connection()
    r = conn.execute(
        f"""SELECT COUNT(*) AS members,
                  COALESCE(SUM({imp} = 1 AND p.is_dup = 0), 0) AS important,
                  COALESCE(SUM({imp} = 0 AND p.is_dup = 0), 0) AS trash,
                  COALESCE(SUM(p.is_dup = 1), 0) AS dups,
                  COALESCE(SUM({imp} IS NULL AND p.is_dup = 0), 0) AS pending_judge,
                  COALESCE(SUM({imp} = 1 AND p.is_dup = 0 AND {_FB_PENDING}), 0) AS pending_finbert,
                  COALESCE(SUM({imp} = 1 AND p.is_dup = 0 AND c.qwen_score IS NULL), 0) AS pending_qwen,
                  COALESCE(SUM({imp} = 1 AND p.is_dup = 0 AND p.posted_at >= ?), 0) AS posts_24h,
                  MIN(CASE WHEN {imp} = 1 AND p.is_dup = 0 THEN p.posted_at END) AS first_post,
                  MAX(CASE WHEN {imp} = 1 AND p.is_dup = 0 THEN p.posted_at END) AS last_post
           FROM post_cat c JOIN posts p ON p.source = c.source AND p.post_id = c.post_id
           WHERE c.category = ?""", (_ago(hours=24), category)).fetchone()
    conn.close()
    return dict(r)


def source_stats(category: str, days: int = 7) -> dict:
    """source -> {'posts': N, 'last_post': ts} среди важных постов категории за последние `days` суток."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT p.source, COUNT(*) AS n, MAX(p.posted_at) AS last FROM post_cat c "
        "JOIN posts p ON p.source = c.source AND p.post_id = c.post_id "
        f"WHERE c.category = ? AND {_imp(category)} = 1 AND p.is_dup = 0 AND p.posted_at >= ? GROUP BY p.source",
        (category, _ago(days=days))).fetchall()
    conn.close()
    return {r["source"]: {"posts": r["n"], "last_post": r["last"]} for r in rows}


# ---------------------------------------------------------------------------
# Журнал итоговых скоров
# ---------------------------------------------------------------------------

def save_asof_rows(category: str, rows: list[dict]) -> None:
    if not rows:
        return
    conn = get_connection()
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ASOF_TABLE} (category TEXT NOT NULL, asof TEXT NOT NULL, "
                 "PRIMARY KEY (category, asof))")
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({ASOF_TABLE})")}
    for col in rows[0]:
        if col != "asof" and col not in existing:
            conn.execute(f"ALTER TABLE {ASOF_TABLE} ADD COLUMN {col} {'INTEGER' if col.endswith('_n') else 'REAL'}")
    names = ["category"] + list(rows[0].keys())
    sql = f"INSERT OR REPLACE INTO {ASOF_TABLE} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
    with conn:
        conn.executemany(sql, [(category, *[r[n] for n in names[1:]]) for r in rows])
    conn.close()

