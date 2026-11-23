"""
Один цикл конвейера из командной строки (без запуска сайта) — удобно для первой загрузки истории и отладки:

    python scripts/run_cycle_once.py

Делает то же, что фоновый воркер: докачка постов -> дубли -> фильтр Qwen -> FinBERT + Qwen -> пересчёт скоров,
после чего печатает итоговые скоры.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from app.storage import posts_db                     # noqa: E402
from app.scoring import service                      # noqa: E402
from app.scoring.pipeline import pipeline            # noqa: E402


async def main():
    from app.storage.db import init_db
    init_db()
    posts_db.init_db()
    await pipeline.run_cycle()
    await pipeline.close()

    print(json.dumps(pipeline.status, ensure_ascii=False, indent=2))
    print(json.dumps(posts_db.counts_global(), ensure_ascii=False, indent=2))

    snap = service.build_snapshot("general")
    print(f"\nСкоры на {snap['asof']} UTC (shrunk, полупериод -> FinBERT / Qwen):")
    for r in snap["rows"]:
        print(f"  {r['hl']:>3g} ч: {r['finbert']['shrunk']:+.3f} / {r['qwen']['shrunk']:+.3f}")


if __name__ == "__main__":
    asyncio.run(main())
