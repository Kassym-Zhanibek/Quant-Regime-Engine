"""
Утилита для мониторинга очередей обработки постов в базе данных.
Запускать в отдельном терминале: python scripts/watch_queue.py
"""
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage import posts_db

print("=== МОНИТОРИНГ ОЧЕРЕДИ ОБРАБОТКИ НОВОСТЕЙ ===")
try:
    while True:
        stats = posts_db.counts_global()
        print(
            f"\rВсего постов: {stats['total']} | "
            f"⏳ В очереди фильтра: {stats['pending_judge']} | "
            f"📊 Очередь FinBERT: {stats['pending_finbert']} | "
            f"🤖 Очередь Qwen: {stats['pending_qwen']} | "
            f"🗑 Мусор: {stats['trash']}      ",
            end="",
            flush=True
        )
        time.sleep(2)
except KeyboardInterrupt:
    print("\nМониторинг остановлен.")
