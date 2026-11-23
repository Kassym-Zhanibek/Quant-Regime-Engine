"""
Автоматический конвейер «Telegram + RSS -> FinBERT + Qwen -> скоры по категориям».

Один цикл (воркер раз в TG_POLL_SECONDS и кнопка «Докачать посты» на панели):

  1. fetch Telegram   новые посты каналов из app/sources.py (от последнего сохранённого message_id)
  2. fetch RSS        новые записи лент (не чаще, чем раз в RSS_POLL_SECONDS)
  3. routing/dedupe   в какие категории попал пост (источник + ключевые слова), точные дубли внутри суток
  4. judge            Qwen-фильтр важности (1/0): отдельный для золота/макро (исходный промпт) и для акций США
  5. score            FinBERT — один на новость; Qwen U/D/N — по разу на каждую категорию (свой промпт);
                      FinBERT и Qwen пишутся независимо: если Qwen упал, FinBERT-скор сохраняется
  6. aggregate        затухание, shrunk / avg / z / W по каждой категории; журнал в sentiment_asof

Любая стадия может упасть — остальные всё равно выполняются; причина попадает в pipeline.status.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

from app import config, sources
from app.categories import ALL_ORDER, STOCK_ORDER
from app.collector import rss_ingest
from app.scoring import scorer, service
from app.scoring.text_clean import clean_telegram_message, cyrillic_share
from app.storage import posts_db

logger = logging.getLogger(__name__)

BATCH = 20          # как часто сбрасывать накопленные скоры в базу


class Pipeline:
    def __init__(self):
        self._lock: asyncio.Lock | None = None       # создаётся лениво, уже внутри event loop
        self._client = None
        self._legacy_imported = False
        self._last_rss = 0.0
        self._routed_full = False         # полный пересчёт маршрутизации (за весь период) — один раз после запуска
        self.status: dict = {
            "enabled": config.TG_ENABLED,
            "telegram": "idle",          # ok | disabled | no_credentials | telethon_missing | needs_login | error
            "stage": "idle",             # idle | fetch | rss | routing | judge | score | aggregate
            "running": False,
            "last_cycle_at": None,
            "last_cycle_seconds": None,
            "sources": {},               # id -> {"kind", "new", "error"}
            "errors": [],
        }

    # ------------------------------------------------------------------ служебное

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _stage(self, name: str) -> None:
        self.status["stage"] = name

    def _err(self, where: str, e: Exception | str) -> None:
        msg = f"{where}: {e}"
        logger.error(msg)
        self.status["errors"].append(msg)

    # ------------------------------------------------------------------ Telegram

    async def _get_client(self):
        """Подключённый и авторизованный клиент Telethon либо None (причина — в status['telegram'])."""
        if not config.TG_ENABLED:
            self.status["telegram"] = "disabled"
            return None
        if not (config.TG_API_ID and config.TG_API_HASH):
            self.status["telegram"] = "no_credentials"
            return None
        try:
            from telethon import TelegramClient
        except ImportError:
            self.status["telegram"] = "telethon_missing"
            return None

        try:
            if self._client is None:
                self._client = TelegramClient(config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH)
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                self.status["telegram"] = "needs_login"
                return None
        except Exception as e:
            self.status["telegram"] = "error"
            self._err("telegram connect", e)
            return None
        self.status["telegram"] = "ok"
        return self._client

    async def _fetch_tg(self) -> None:
        self._stage("fetch")
        client = await self._get_client()
        if client is None:
            return
        for src in sources.tg_sources():
            channel = src["id"]
            try:
                last_id = await asyncio.to_thread(posts_db.last_tg_message_id, channel)
                if last_id:
                    # только новее последнего сохранённого, от старых к новым
                    it = client.iter_messages(channel, min_id=last_id, reverse=True)
                else:
                    start = datetime.now(timezone.utc) - timedelta(hours=config.TG_BACKFILL_HOURS)
                    it = client.iter_messages(channel, offset_date=start - timedelta(seconds=1), reverse=True)

                rows = []
                async for msg in it:
                    if not msg.text:                       # картинки/стикеры без текста
                        continue
                    clean, tags = clean_telegram_message(channel, msg.text)
                    rows.append({"source": channel, "post_id": msg.id, "kind": "tg",
                                 "posted_at": posts_db.utc_str(msg.date), "title": None, "text": msg.text,
                                 "clean_text": clean, "hashtags": tags, "url": f"https://t.me/{channel}/{msg.id}"})
                    if len(rows) >= config.TG_MAX_PER_CYCLE:
                        break
                n = await asyncio.to_thread(posts_db.insert_posts, rows)
                self.status["sources"][channel] = {"kind": "tg", "new": n, "error": None}
            except Exception as e:
                self.status["sources"][channel] = {"kind": "tg", "new": 0, "error": f"{type(e).__name__}: {e}"[:200]}
                self._err(f"fetch @{channel}", e)

    # ------------------------------------------------------------------ RSS

    async def _fetch_rss(self, http: httpx.AsyncClient, force: bool) -> None:
        if not force and time.monotonic() - self._last_rss < config.RSS_POLL_SECONDS:
            return
        self._stage("rss")
        rows, report = await rss_ingest.fetch_all(http, sources.rss_sources())
        self._last_rss = time.monotonic()
        n = await asyncio.to_thread(posts_db.insert_posts, rows)
        # «новые» по каждой ленте точно не посчитать без лишнего запроса, поэтому показываем число записей в ленте
        for sid, rep in report.items():
            self.status["sources"][sid] = {"kind": "rss", "new": rep["entries"], "error": rep["error"]}
        failed = [sid for sid, rep in report.items() if rep["error"]]
        if failed:
            self._err("rss", f"не открылись ленты: {', '.join(failed)} (см. scripts/check_sources.py)")
        logger.info("RSS: записей в лентах %s, новых в базе %s", len(rows), n)

    # ------------------------------------------------------------------ Qwen-фильтр

    async def _judge(self, http: httpx.AsyncClient) -> None:
        """Золото/макро и акции проверяются РАЗНЫМИ промптами и пишут вердикты в разные колонки. Золото — первым и без лимита."""
        self._stage("judge")
        for group in ("gold", "stocks"):
            if group == "stocks" and not STOCK_ORDER:
                continue
            limit = config.STOCK_JUDGE_PER_CYCLE if group == "stocks" else None
            if not await self._judge_group(http, group, limit):
                return                                           # Ollama не отвечает — остальное на следующем цикле

    async def _judge_group(self, http: httpx.AsyncClient, group: str, limit: int | None) -> bool:
        rows = await asyncio.to_thread(posts_db.pending_judge, group, limit)
        total = len(rows)
        tag = "АКЦИИ" if group == "stocks" else "ЗОЛОТО"
        if total > 0:
            logger.info("🔍 [ЭТАП 1/3] Фильтрация важности (Qwen Judge, %s): в очереди %d постов", tag, total)

        consec = 0
        for idx, (source, post_id, text) in enumerate(rows, 1):
            verdict = await scorer.judge_importance(http, text, group)
            if verdict is None:
                consec += 1
                logger.warning("[%d/%d] ❌ Ollama не ответила (%s)", idx, total, source)
                if consec >= config.MAX_CONSEC_QWEN_FAIL:
                    self._err("judge", f"{consec} неудач подряд — Ollama не отвечает, остальное на следующем цикле")
                    return False
                continue
            consec = 0
            await asyncio.to_thread(posts_db.set_verdict, source, post_id, verdict, group)

            status_str = "✅ ВАЖНО (1)" if verdict == 1 else "🗑 МУСОР (0)"
            preview = (text or "").replace("\n", " ")[:60]
            logger.info("[%s %d/%d] [%s] %s | \"%s...\"", tag, idx, total, source, status_str, preview)

            await asyncio.sleep(random.uniform(config.TG_JUDGE_DELAY, config.TG_JUDGE_DELAY + 1))
        return True

    # ------------------------------------------------------------------ скоринг

    async def _score_finbert(self) -> None:
        rows = await asyncio.to_thread(posts_db.pending_finbert)
        total = len(rows)
        if not rows:
            return
        logger.info("📊 [ЭТАП 2/3] Скоринг FinBERT: %d постов в очереди", total)
        await asyncio.to_thread(scorer.get_finbert)             # первая загрузка модели — не в event loop
        batch = []
        for idx, (source, post_id, text) in enumerate(rows, 1):
            preview = (text or "").replace("\n", " ")[:50]
            if config.FINBERT_SKIP_CYRILLIC and cyrillic_share(text) >= config.CYRILLIC_THRESHOLD:
                # FinBERT англоязычный: на русском тексте он выдал бы шум. Метка «пропущено»: neu заполнен, score пуст;
                # итог по такому посту строится только по Qwen (агрегатор умеет пустые оценки).
                batch.append((None, None, 1.0, None, source, post_id))
                logger.info("[FB %d/%d] [%s] пропущен (не английский) | \"%s...\"", idx, total, source, preview)
            else:
                p, n, u, s = await asyncio.to_thread(scorer.finbert_scores, text)
                batch.append((p, n, u, s, source, post_id))
                logger.info("[FB %d/%d] [%s] Score: %+.3f | \"%s...\"", idx, total, source, s, preview)

            if len(batch) >= BATCH:
                await asyncio.to_thread(posts_db.save_finbert, batch)
                batch = []
        if batch:
            await asyncio.to_thread(posts_db.save_finbert, batch)

    async def _score_qwen(self, http: httpx.AsyncClient) -> None:
        consec = 0
        for category in ALL_ORDER:          # сначала золото, затем акции
            rows = await asyncio.to_thread(posts_db.pending_qwen, category)
            total = len(rows)
            if not rows:
                continue
            logger.info("🤖 [ЭТАП 3/3] Скоринг Qwen [%s]: %d постов", category.upper(), total)
            batch = []
            for idx, (source, post_id, text) in enumerate(rows, 1):
                res = await scorer.qwen_udn(http, text, category)
                if res is None:
                    consec += 1
                    if consec >= config.MAX_CONSEC_QWEN_FAIL:
                        self._err("qwen score", f"{consec} неудач подряд — Ollama не отвечает, остальное на следующем цикле")
                        if batch:
                            await asyncio.to_thread(posts_db.save_qwen, category, batch)
                        return
                    continue
                consec = 0
                batch.append((*res, source, post_id))
                preview = (text or "").replace("\n", " ")[:50]
                logger.info("[QW-%s %d/%d] [%s] Score: %+.3f | \"%s...\"", category.upper(), idx, total, source, res[3], preview)

                if len(batch) >= BATCH:
                    await asyncio.to_thread(posts_db.save_qwen, category, batch)
                    batch = []
            if batch:
                await asyncio.to_thread(posts_db.save_qwen, category, batch)

    async def _score(self, http: httpx.AsyncClient) -> None:
        self._stage("score")
        # FinBERT и Qwen пишутся независимо: сбой одного не отнимает результат другого
        for name, coro in (("finbert", self._score_finbert()), ("qwen", self._score_qwen(http))):
            try:
                await coro
            except Exception as e:
                self._err(f"score {name}", e)

    # ------------------------------------------------------------------ цикл

    async def run_cycle(self, force: bool = False) -> bool:
        """Один полный проход. Если предыдущий ещё идёт — ничего не делает (возвращает False)."""
        if self.lock.locked():
            return False
        async with self.lock:
            t0 = datetime.now(timezone.utc)
            self.status.update(running=True, errors=[])
            try:
                if config.GOLD_DB_PATH and not self._legacy_imported:
                    n = await asyncio.to_thread(posts_db.import_legacy_live, config.GOLD_DB_PATH)
                    self._legacy_imported = True
                    if n:
                        logger.info("Импортировано постов из старой telegram_posts_live: %s", n)

                async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as http:
                    try:
                        await self._fetch_tg()
                    except Exception as e:
                        self._err("fetch telegram", e)
                    try:
                        await self._fetch_rss(http, force)
                    except Exception as e:
                        self._err("fetch rss", e)

                    self._stage("routing")
                    try:
                        # первый цикл после запуска: весь период (подхватывает изменения app/sources.py); дальше — только свежие посты
                        win = None if not self._routed_full else 2
                        await asyncio.to_thread(posts_db.ensure_memberships, win)
                        self._routed_full = True
                        await asyncio.to_thread(posts_db.mark_duplicates)
                    except Exception as e:
                        self._err("routing", e)

                    try:
                        await self._judge(http)
                    except Exception as e:
                        self._err("judge", e)
                    await self._score(http)

                self._stage("aggregate")
                try:
                    await asyncio.to_thread(service.persist_recent)
                except Exception as e:
                    self._err("aggregate", e)
            except Exception as e:
                self._err("cycle", e)
            finally:
                now = datetime.now(timezone.utc)
                self.status.update(
                    running=False, stage="idle",
                    last_cycle_at=now.isoformat(),
                    last_cycle_seconds=round((now - t0).total_seconds(), 1),
                )
            return True

    async def worker(self) -> None:
        """Фоновый воркер: цикл -> пауза -> цикл. Останавливается через task.cancel()."""
        try:
            while True:
                try:
                    await self.run_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._err("worker", e)
                await asyncio.sleep(config.TG_POLL_SECONDS)
        finally:
            await self.close()

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None


pipeline = Pipeline()
