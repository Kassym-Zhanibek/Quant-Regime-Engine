"""
Проверка источников из app/sources.py и подбор Telegram-каналов по РЕАЛЬНОМУ содержимому, а не по названию.

    python scripts/check_sources.py                       # RSS-ленты: открылись ли, сколько свежих записей, куда маршрутизируются
    python scripts/check_sources.py --only stocks         # только ленты акций (или --only gold)
    python scripts/check_sources.py --tg                  # каналы из sources.py: активность, доля «по тикерам», язык, реклама
    python scripts/check_sources.py --probe marketfeed https://t.me/markettwits    # то же для любых каналов (ещё не добавленных)
    python scripts/check_sources.py --find-tg             # поиск каналов по стандартным запросам + оценка содержимого
    python scripts/check_sources.py --find-tg "nasdaq" "earnings" --min-subs 1000 --top 20

Что считается по каналу (последние --limit постов, по умолчанию 100):
  постов/день     как часто пишут
  по тикерам %    какая доля постов попала бы в акции из app/stocks.py (ключевые слова / тикеры — как в боевом конвейере)
  тикеров/день    постов/день × доля — то, что реально дойдёт до Qwen. Канал с 300 постами в день и 0.5 по теме
                  не нужен; канал с 5 постами в день и 60% по теме — нужен
  рус %           доля русскоязычных постов (для них FinBERT пропускается, итог только по Qwen)
  реклама %       «VIP», «сигналы», «подписывайтесь», «промокод» …

ВАЖНО для --tg / --probe / --find-tg: одну сессию Telegram не могут держать две программы. Остановите сайт (uvicorn)
или укажите копию сессии: TG_SESSION=<путь к копии без .session>. Между каналами скрипт делает паузы (защита от FloodWait).
"""
import argparse
import asyncio
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, sources                                   # noqa: E402
from app.scoring.text_clean import clean_telegram_message, cyrillic_share   # noqa: E402
from app.stocks import MARKET_KEY, TICKERS                        # noqa: E402

STOCK_CATS = set(TICKERS) | {MARKET_KEY}
US = [MARKET_KEY] + TICKERS
ALL = ["general", "oil", "us10y", "dxy"]

DEFAULT_QUERIES = ["US stock market", "wall street news", "stock market news", "earnings news", "nasdaq", "S&P 500",
                   "акции США", "фондовый рынок США"]
PROMO = re.compile(
    r"\bvip\b|\bsignals?\b|premium|join (us|now|our)|subscribe|promo ?code|giveaway|referral|bonus|\bcopy ?trad|"
    r"free (course|webinar)|сигнал|подпис|промокод|бонус|розыгрыш|курс по|обучени|вип[- ]?(канал|группа)|t\.me/\+",
    re.IGNORECASE)
GOOD_PER_DAY = 1.0          # меньше «тикерных» постов в сутки — канал почти бесполезен для сентимента
MAX_PROMO = 0.25


def _age(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return f"{h * 60:.0f} мин" if h < 1.5 else f"{h:.1f} ч" if h < 48 else f"{h / 24:.0f} дн"


def norm_channel(name: str) -> str:
    """'https://t.me/marketfeed' / '@marketfeed' / 'marketfeed' -> 'marketfeed'."""
    name = name.strip()
    name = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", name, flags=re.IGNORECASE)
    return name.lstrip("@").split("/")[0].split("?")[0]


# ---------------------------------------------------------------------------
# Оценка канала по его постам (чистая функция — проверяется в scripts/selftest.py без сети)
# ---------------------------------------------------------------------------

def analyse(messages: list[tuple[datetime, str]], channel: str, cfg: dict | None = None) -> dict:
    """messages: (время UTC, сырой текст). cfg — конфиг источника (по умолчанию: «широкий» канал ALL + US с фильтром по словам)."""
    cfg = cfg or {"id": channel, "kind": "tg", "categories": ALL + US, "gated": ALL + US}
    n = len(messages)
    out = {"n": n, "per_day": 0.0, "stock_hit": 0.0, "gold_hit": 0.0, "stock_per_day": 0.0, "ru": 0.0, "promo": 0.0,
           "top": [], "newest": None, "verdict": "нет постов"}
    if not n:
        return out
    stock = gold = ru = promo = 0
    tickers: dict[str, int] = {}
    for _, text in messages:
        clean, _tags = clean_telegram_message(channel, text)
        cats = sources.categories_for(cfg, clean)
        hit = [c for c in cats if c in STOCK_CATS]
        stock += bool(hit)
        gold += any(c in ALL for c in cats)
        for c in hit:
            tickers[c] = tickers.get(c, 0) + 1
        ru += cyrillic_share(clean) >= config.CYRILLIC_THRESHOLD
        promo += bool(PROMO.search(text))
    dates = [d for d, _ in messages]
    span_days = max((max(dates) - min(dates)).total_seconds() / 86400, 1 / 24)
    out.update(per_day=n / span_days, stock_hit=stock / n, gold_hit=gold / n, ru=ru / n, promo=promo / n,
               newest=max(dates), top=[f"{k}:{v}" for k, v in sorted(tickers.items(), key=lambda kv: -kv[1])[:4]])
    out["stock_per_day"] = out["per_day"] * out["stock_hit"]
    if out["promo"] > MAX_PROMO:
        out["verdict"] = "🚫 реклама / сигналы"
    elif out["stock_per_day"] < GOOD_PER_DAY:
        out["verdict"] = "⚠️ мало по теме"
    else:
        out["verdict"] = "✅ годится"
    return out


def stats_line(name: str, subs, a: dict) -> str:
    subs_s = "?" if subs is None else str(subs)
    return (f"@{name:<26} подп. {subs_s:>8} | постов/д {a['per_day']:>6.1f} | по тикерам {a['stock_hit'] * 100:>3.0f}% "
            f"({a['stock_per_day']:>5.1f}/д) | золото {a['gold_hit'] * 100:>3.0f}% | рус {a['ru'] * 100:>3.0f}% | "
            f"реклама {a['promo'] * 100:>3.0f}% | {a['verdict']}"
            + (f" | {', '.join(a['top'])}" if a["top"] else ""))


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def check_rss(only: str | None) -> int:
    import feedparser
    import httpx
    from app.collector.rss_ingest import HEADERS, parse_feed_text

    feeds = [s for s in sources.rss_sources()
             if only is None or (only == "stocks") == bool(set(s["categories"]) & STOCK_CATS)]
    print(f"RSS-лент к проверке: {len(feeds)}\n")

    def one(src: dict) -> dict:
        try:
            with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=config.RSS_TIMEOUT) as c:
                r = c.get(src["url"])
            r.raise_for_status()
            raw = feedparser.parse(r.text)
            rows = parse_feed_text(src, r.text)
            routed: dict[str, int] = {}
            for row in rows:
                for cat in sources.categories_for(src, row["clean_text"] or row["text"]):
                    routed[cat] = routed.get(cat, 0) + 1
            newest = max((datetime.strptime(x["posted_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                          for x in rows), default=None)
            return {"src": src, "ok": True, "raw": len(raw.entries), "fresh": len(rows), "routed": routed, "newest": newest}
        except Exception as e:
            return {"src": src, "ok": False, "error": f"{type(e).__name__}: {e}"[:160]}

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(one, feeds))

    good = 0
    for r in results:
        sid = r["src"]["id"]
        if not r["ok"]:
            print(f"❌ {sid:<26} {r['error']}\n   {r['src']['url']}")
            continue
        warn = r["raw"] == 0 or r["fresh"] == 0
        good += not warn
        routed = ", ".join(f"{k}:{v}" for k, v in sorted(r["routed"].items())) or "никуда"
        print(f"{'⚠️ ' if warn else '✅'} {sid:<26} записей {r['raw']:>3}, свежих {r['fresh']:>3}, последняя {_age(r['newest']):>7} | попадёт: {routed}")
        if warn:
            print(f"   лента пуста или всё старше {config.RSS_MAX_AGE_HOURS} ч: {r['src']['url']}")
    print(f"\nИтого рабочих: {good} из {len(feeds)}")
    return len(feeds) - good


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def _messages(client, entity, limit: int) -> list[tuple[datetime, str]]:
    out = []
    async for m in client.iter_messages(entity, limit=limit):
        if m.text:
            out.append((m.date.astimezone(timezone.utc), m.text))
    return out


async def check_tg(mode: str, names: list[str], min_subs: int, top: int, limit: int) -> None:
    if not (config.TG_API_ID and config.TG_API_HASH):
        sys.exit("Задайте TG_API_ID и TG_API_HASH (и сделайте вход: python scripts/tg_login.py).")
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.contacts import SearchRequest

    client = TelegramClient(config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            sys.exit("Нет входа в Telegram: python scripts/tg_login.py")

        async def probe(name: str, cfg: dict | None, subs=None, entity=None):
            ent = entity or await client.get_entity(name)
            if subs is None:
                subs = getattr(ent, "participants_count", None)
            msgs = await _messages(client, ent, limit)
            return subs, analyse(msgs, name, cfg)

        if mode == "sources":                                    # каналы из app/sources.py — по их боевой конфигурации
            print(f"Каналов в sources.py: {len(sources.tg_sources())} (оценка по последним {limit} постам)\n")
            for s in sources.tg_sources():
                try:
                    subs, a = await probe(s["id"], s)
                    print(stats_line(s["id"], subs, a) + f" | последний пост {_age(a['newest'])} назад")
                except FloodWaitError as e:
                    sys.exit(f"Telegram просит подождать {e.seconds} с (FloodWait) — повторите позже.")
                except Exception as e:
                    print(f"❌ @{s['id']:<26} {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(1.0)

        elif mode == "probe":                                    # произвольные каналы — как «широкий» источник ALL + US
            for raw in names:
                name = norm_channel(raw)
                try:
                    subs, a = await probe(name, None)
                    print(stats_line(name, subs, a) + f" | последний пост {_age(a['newest'])} назад")
                except FloodWaitError as e:
                    sys.exit(f"Telegram просит подождать {e.seconds} с (FloodWait) — повторите позже.")
                except Exception as e:
                    print(f"❌ @{name:<26} {type(e).__name__}: {e}"[:200])
                await asyncio.sleep(1.0)

        else:                                                    # поиск
            queries = names or DEFAULT_QUERIES
            have = {s["id"].lower() for s in sources.tg_sources()}
            found: dict[str, object] = {}
            for q in queries:
                res = await client(SearchRequest(q=q, limit=30))
                for ch in res.chats:
                    u = getattr(ch, "username", None)
                    if not (getattr(ch, "broadcast", False) and u) or u.lower() in have:
                        continue
                    subs = getattr(ch, "participants_count", None)
                    if subs is not None and subs < min_subs:
                        continue
                    found[u] = ch
                await asyncio.sleep(1.0)
            cands = sorted(found.values(), key=lambda c: -(getattr(c, "participants_count", 0) or 0))[:top]
            print(f"Найдено каналов (≥{min_subs} подписчиков, ещё не в sources.py): {len(found)}; оцениваю {len(cands)} крупнейших "
                  f"по последним {limit} постам…\n")
            scored = []
            for ch in cands:
                try:
                    subs, a = await probe(ch.username, None, getattr(ch, "participants_count", None), ch)
                    scored.append((a["stock_per_day"] if a["verdict"].startswith("✅") else -1, ch.username, subs, a))
                except FloodWaitError as e:
                    print(f"Telegram просит подождать {e.seconds} с (FloodWait) — показываю то, что успел.")
                    break
                except Exception as e:
                    print(f"❌ @{ch.username}: {type(e).__name__}: {e}"[:160])
                await asyncio.sleep(1.5)
            for _, name, subs, a in sorted(scored, key=lambda x: -x[0]):
                print(stats_line(name, subs, a))
            print('\nПодходящий канал (✅) добавьте в app/sources.py, например:\n'
                  '   {"id": "<username>", "kind": "tg", "categories": US, "gated": US},')
    finally:
        await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=["stocks", "gold"], help="RSS: проверять только ленты акций / золота")
    ap.add_argument("--tg", action="store_true", help="оценить Telegram-каналы из sources.py")
    ap.add_argument("--probe", nargs="+", metavar="CHANNEL", help="оценить указанные каналы (username или ссылка t.me)")
    ap.add_argument("--find-tg", nargs="*", metavar="QUERY", help="найти и оценить публичные каналы (без запросов — стандартный набор)")
    ap.add_argument("--min-subs", type=int, default=300, help="поиск: минимум подписчиков (по умолчанию 300)")
    ap.add_argument("--top", type=int, default=12, help="поиск: сколько крупнейших каналов оценивать (по умолчанию 12)")
    ap.add_argument("--limit", type=int, default=100, help="сколько последних постов анализировать (по умолчанию 100)")
    a = ap.parse_args()
    if a.probe:
        asyncio.run(check_tg("probe", a.probe, a.min_subs, a.top, a.limit))
    elif a.find_tg is not None:
        asyncio.run(check_tg("find", a.find_tg, a.min_subs, a.top, a.limit))
    elif a.tg:
        asyncio.run(check_tg("sources", [], a.min_subs, a.top, a.limit))
    else:
        check_rss(a.only)


if __name__ == "__main__":
    main()
