"""
Настройки проекта. Все значения можно переопределить переменными окружения
(в PyCharm: Run -> Edit Configurations -> Environment variables).
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent      # корень проекта (папка, где лежит app/)


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str, default: list) -> list:
    v = os.getenv(name)
    return [x.strip() for x in v.split(",") if x.strip()] if v else list(default)


# ---------------- Telegram ----------------
PIPELINE_ENABLED = _bool("PIPELINE_ENABLED", True)         # фоновый конвейер целиком (Telegram + RSS + скоринг)
TG_ENABLED = _bool("TG_ENABLED", True)                       # только загрузка из Telegram (RSS и скоринг работают и без него)
TG_API_ID = os.getenv("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH")
# Путь к файлу сессии БЕЗ расширения .session. ВАЖНО: нужна КОПИЯ web_session.session,
# а не тот же файл, что открыт в telegram_parser*.py (две программы не могут держать одну сессию).
TG_SESSION = os.getenv("TG_SESSION", str(BASE_DIR / "parser_session"))
# Каналы и RSS-ленты теперь задаются в app/sources.py
TG_POLL_SECONDS = _int("TG_POLL_SECONDS", 300)               # как часто запускать цикл «докачать -> оценить -> пересчитать»
# Если в базе по каналу ещё нет постов — за сколько часов назад докачать историю.
# 240 ч (10 суток) нужно, чтобы «хвост» затухания для полупериода 72 ч не был обрезан.
TG_BACKFILL_HOURS = _int("TG_BACKFILL_HOURS", 240)
TG_MAX_PER_CYCLE = _int("TG_MAX_PER_CYCLE", 3000)            # предохранитель: максимум постов с канала за один цикл
TG_JUDGE_DELAY = _float("TG_JUDGE_DELAY", 1.0)               # пауза после вердикта Qwen: random(D, D+1) сек (защита от перегрева)

# ---------------- RSS ----------------
RSS_POLL_SECONDS = _int("RSS_POLL_SECONDS", 600)             # как часто опрашивать RSS-ленты (внутри цикла конвейера)
RSS_MAX_AGE_HOURS = _int("RSS_MAX_AGE_HOURS", 240)           # старые записи лент не берём
RSS_TIMEOUT = 15.0
RSS_MAX_SUMMARY_CHARS = 600                                  # длинное описание обрезается (экономия на Qwen)
# Прежний RSS-конвейер (app/collector/rss_parser.py, таблица news) кормит модель Market Bias (Random Forest).
# Пока модель не переведена на новые скоры — оставьте 1. 0 = не запускать (Market Bias тогда получит пустые признаки).
LEGACY_RSS_ENABLED = _bool("LEGACY_RSS_ENABLED", True)

# ---------------- Ollama / Qwen ----------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen2.5:7b")
MAX_CONSEC_QWEN_FAIL = 10                                    # столько неудач подряд -> Ollama лежит, цикл прерывает Qwen-этап
QWEN_ERROR_PAUSE = 5.0                                       # пауза после ошибки Ollama, сек

# ---------------- Скоринг и агрегация ----------------
# Скорить очищенный текст (без ссылок/хэштегов/подписи) — как в step1 -> step4 при обучении модели.
# Поставь TG_SCORE_CLEANED=0, чтобы скорить «сырой» текст, как делал score_live.py.
TG_SCORE_CLEANED = _bool("TG_SCORE_CLEANED", True)
ENSEMBLE_W_FINBERT = _float("ENSEMBLE_W_FINBERT", 0.3)       # итоговый скор карточки = 0.3·FinBERT + 0.7·Qwen (как раньше в RSS-конвейере)
SIGNAL_THRESHOLD = 0.05                                      # |скор| меньше порога — «нейтрально»
MARKET_CACHE_SECONDS = 60                                    # кэш котировок (yfinance), чтобы не дёргать биржу при каждом обновлении панели
HALF_LIVES_H = [3, 12, 24, 72]                               # полупериоды затухания, часы
PRIMARY_HL = 24                                              # основной полупериод (его показывают крупные карточки)
SHRINK_K = _float("SHRINK_K", 1.0)                           # k «псевдо-нейтральных» постов, тянущих скор к 0
MIN_HIST = 30                                                # минимум постов в истории для μ/σ
AGG_LOOKBACK_DAYS = _int("AGG_LOOKBACK_DAYS", 30)            # сколько суток постов брать в расчёт
DEDUP_WINDOW_DAYS = 14                                       # за сколько суток пересчитывать точные дубли

# ---------------- Акции США (свинг) ----------------
# Выключить всю вкладку «Акции» (тикеры не попадут в конвейер и не нагружают Qwen): STOCKS_ENABLED=0
STOCKS_ENABLED = 0
# Для свинга (3-10 торговых дней) память новостей длиннее, чем у золота. Основной полупериод — 72 ч.
# Для полупериода 120 ч «хвост» затухания заполнится только через ~17 суток накопленной истории
# (RSS / Telegram докачивают максимум 240 ч) — до тех пор панель покажет предупреждение, это нормально.
STOCK_HALF_LIVES_H = [12, 24, 72, 120]
STOCK_PRIMARY_HL = 72
STOCK_SERIES_HOURS = 240                                     # окно графика на странице тикера
# Вес FinBERT в итоговом скоре АКЦИЙ. FinBERT обучен на корпоративных новостях, и для компаний «позитивный тон» ≈
# «хорошо для цены», поэтому вес выше, чем у золота (там тон макро-новости и направление золота часто расходятся).
# Стартовое значение — подбирайте по IC скора к форвардной доходности.
STOCK_W_FINBERT = _float("STOCK_W_FINBERT", 0.4)
# Сколько постов АКЦИЙ судья важности проверяет за один цикл (свежие первыми, остальное — в следующих циклах).
# Без предела первый запуск (сотни постов истории) на много минут задержал бы и золото: цикл у конвейера общий.
STOCK_JUDGE_PER_CYCLE = _int("STOCK_JUDGE_PER_CYCLE", 150)
# FinBERT понимает только английский. Русские посты (БКС Экспресс, Market Twits …) он оценил бы случайным шумом,
# поэтому для них FinBERT пропускается, итог по такому посту = только Qwen. Выключить: FINBERT_SKIP_CYRILLIC=0.
FINBERT_SKIP_CYRILLIC = _bool("FINBERT_SKIP_CYRILLIC", True)
CYRILLIC_THRESHOLD = _float("CYRILLIC_THRESHOLD", 0.3)       # доля кириллицы среди букв, с которой текст считается нерусскоязычным для FinBERT

# Файл gold_market_normalized.db из старого проекта (необязательно). Из него берутся:
#   * таблица news_posts   -> μ и σ для z-скора (историческое распределение скоров);
#   * таблица telegram_posts_live -> разовый импорт уже скачанных постов с вердиктами Qwen.
GOLD_DB_PATH = os.getenv("GOLD_DB_PATH") or None
HIST_TABLE = "news_posts"
LEGACY_LIVE_TABLE = "telegram_posts_live"


