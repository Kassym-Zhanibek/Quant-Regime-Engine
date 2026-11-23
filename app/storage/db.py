import os
import sqlite3
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "news.db")


def get_connection():
    """Единая точка подключения к БД с поддержкой WAL и таймаутами."""
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id TEXT PRIMARY KEY,
            title TEXT,
            summary TEXT,
            source TEXT,
            published_at TEXT,
            category TEXT,
            finbert_pos REAL DEFAULT 0.0,
            finbert_neg REAL DEFAULT 0.0,
            finbert_neu REAL DEFAULT 0.0,
            finbert_score REAL DEFAULT 0.0,
            qwen_score REAL DEFAULT 0.0,
            raw_score REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Автоматические миграции
    cursor.execute("PRAGMA table_info(news)")
    columns = [column[1] for column in cursor.fetchall()]
    for col, col_type in [
        ("finbert_pos", "REAL DEFAULT 0.0"),
        ("finbert_neg", "REAL DEFAULT 0.0"),
        ("finbert_neu", "REAL DEFAULT 0.0"),
        ("finbert_score", "REAL DEFAULT 0.0"),
        ("qwen_score", "REAL DEFAULT 0.0")
    ]:
        if col not in columns:
            cursor.execute(f"ALTER TABLE news ADD COLUMN {col} {col_type}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            market_bias REAL,
            sentiment_score REAL,
            macro_score REAL
        )
    """)

    cursor.execute("SELECT COUNT(*) FROM sentiment_history")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO sentiment_history (timestamp, market_bias, sentiment_score, macro_score)
            VALUES (?, 0.0, 0.0, 0.0)
        """, (datetime.now(timezone.utc).isoformat(),))

    conn.commit()
    conn.close()


def is_news_processed(news_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM news WHERE id = ?", (news_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None


def save_news_item(item: dict, finbert_data: dict, qwen_score: float, final_score: float):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO news (
            id, title, summary, source, published_at, category,
            finbert_pos, finbert_neg, finbert_neu, finbert_score, qwen_score, raw_score
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        item["id"],
        item["title"],
        item["summary"],
        item["source"],
        item["published_at"],
        item["category"],
        finbert_data.get("p_positive", 0.0),
        finbert_data.get("p_negative", 0.0),
        finbert_data.get("p_neutral", 0.0),
        finbert_data.get("finbert_score", 0.0),
        qwen_score,
        final_score
    ))
    conn.commit()
    conn.close()


def save_sentiment_snapshot(market_bias: float, sentiment_score: float, macro_score: float):
    conn = get_connection()
    cursor = conn.cursor()

    now = datetime.now(timezone.utc)
    current_hour_str = now.strftime("%Y-%m-%d %H:00:00")

    cursor.execute("""
        SELECT 1 FROM sentiment_history 
        WHERE strftime('%Y-%m-%d %H:00:00', timestamp) = ?
        LIMIT 1
    """, (current_hour_str,))

    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO sentiment_history (timestamp, market_bias, sentiment_score, macro_score)
            VALUES (?, ?, ?, ?)
        """, (
            now.isoformat(),
            market_bias,
            sentiment_score,
            macro_score
        ))
        conn.commit()

    conn.close()


def get_sentiment_history(limit: int = 50):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT timestamp, market_bias, sentiment_score, macro_score
        FROM sentiment_history
        WHERE id IN (
            SELECT MAX(id)
            FROM sentiment_history
            GROUP BY strftime('%Y-%m-%d %H:00:00', timestamp)
        )
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "timestamp": row[0],
            "market_bias": row[1],
            "sentiment_score": row[2],
            "macro_score": row[3]
        }
        for row in reversed(rows)
    ]


def get_recent_news(hours: int = 24, limit: int = 100):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, summary, source, published_at, category,
               finbert_pos, finbert_neg, finbert_neu, finbert_score, qwen_score, raw_score
        FROM news
        ORDER BY datetime(published_at) DESC
        LIMIT ?
    """, (limit * 2,))
    rows = cursor.fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    news_list = []
    for row in rows:
        pub_str = row[4]
        try:
            pub_time = parsedate_to_datetime(pub_str)
            if pub_time.tzinfo is None:
                pub_time = pub_time.replace(tzinfo=timezone.utc)
        except Exception:
            pub_time = now

        if pub_time >= cutoff:
            news_list.append({
                "id": row[0],
                "title": row[1],
                "summary": row[2],
                "source": row[3],
                "published_at": pub_str,
                "category": row[5],
                "finbert_pos": row[6],
                "finbert_neg": row[7],
                "finbert_neu": row[8],
                "finbert_score": row[9],
                "qwen_score": row[10],
                "raw_score": row[11],
                "_dt": pub_time
            })

    news_list.sort(key=lambda x: x["_dt"], reverse=True)

    for item in news_list:
        item.pop("_dt", None)

    return news_list[:limit]
