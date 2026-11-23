"""
Оценка новостей (Telegram + RSS) — перенос логики из score_live.py / telegram_parser_live.py.

  judge_importance()    — Qwen-фильтр «важная новость / мусор» (1 / 0): свой промпт для золота и для акций
  qwen_udn(category)    — Qwen как классификатор U/D/N: вероятности по logprobs первого токена;
                          у каждой категории свой промпт (app/scoring/prompts.py), qwen_score = p_bull - p_bear
  finbert_scores()      — FinBERT: finbert_score = p_pos - p_neg (от категории не зависит)

К Ollama обращаемся напрямую по HTTP (httpx уже есть в проекте), без пакетов `openai` и `ollama`.
"""
import asyncio
import logging
import math
import re

import httpx

from app import config
from app.categories import CATEGORIES
from app.model.finbert_model import get_finbert

logger = logging.getLogger(__name__)

# Судья золота / макро (нефть, облигации, доллар) — ИСХОДНЫЙ промпт, без изменений.
JUDGE_PROMPT = """
Ты — финансовый аналитик Forex. Отфильтруй текст сообщения.
Определи, содержит ли оно важную фундаментальную новость. Рекламу, спам и флуд игнорируй.
Ответь СТРОГО одной цифрой: 1 — если новость важная, 0 — если мусор.

Текст сообщения:
{text}
"""

JUDGE_PROMPT_STOCKS = """
Ты — аналитик по акциям США. Отфильтруй текст сообщения.
Определи, содержит ли оно важный фундаментальный факт для компаний или рынка США.
Ответь СТРОГО одной цифрой: 1 — если текст важный, 0 — если мусор или реклама.

Текст сообщения:
{text}
"""
# ---------------------------------------------------------------------------
# FinBERT
# ---------------------------------------------------------------------------

def finbert_scores(text: str) -> tuple[float, float, float, float]:
    """(p_pos, p_neg, p_neu, score). Синхронная — вызывать через asyncio.to_thread."""
    p_pos, p_neg, p_neu = get_finbert().probabilities(text)
    return p_pos, p_neg, p_neu, p_pos - p_neg


# ---------------------------------------------------------------------------
# Qwen: классификатор U / D / N
# ---------------------------------------------------------------------------

def parse_udn(top_logprobs: list[dict]) -> tuple[float, float, float, float] | None:
    """
    top_logprobs первого токена -> (p_bull, p_bear, p_neu, score) либо None, если модель ответила не U/D/N.
    Один и тот же ответ может прийти разными токенами ("U", " U", "u") — их вероятности складываются.
    """
    mass = {"U": 0.0, "D": 0.0, "N": 0.0}
    for item in top_logprobs:
        key = str(item.get("token", "")).strip().upper()
        if key in mass:
            mass[key] += math.exp(item["logprob"])

    total = sum(mass.values())
    if total < 0.5:          # модель ответила не буквой U/D/N — такому результату верить нельзя
        return None
    p_bull, p_bear, p_neu = mass["U"] / total, mass["D"] / total, mass["N"] / total
    return round(p_bull, 4), round(p_bear, 4), round(p_neu, 4), round(p_bull - p_bear, 4)


async def qwen_udn(http: httpx.AsyncClient, text: str, category: str = "general") -> tuple[float, float, float, float] | None:
    """Возвращает (p_bull, p_bear, p_neu, score) или None при ошибке Ollama / нечитаемом ответе."""
    payload = {
        "model": config.QWEN_MODEL,
        "messages": [
            {"role": "system", "content": CATEGORIES[category]["prompt"]},
            {"role": "user", "content": f"Текст новости: {text}\nОценка (U/D/N):"},
        ],
        "max_tokens": 1,
        "temperature": 0.0,
        "logprobs": True,
        "top_logprobs": 10,
    }
    try:
        r = await http.post(f"{config.OLLAMA_URL}/v1/chat/completions", json=payload)
        r.raise_for_status()
        top = r.json()["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        return parse_udn(top)
    except Exception as e:
        logger.warning("Ошибка Qwen (U/D/N): %s", e)
        await asyncio.sleep(config.QWEN_ERROR_PAUSE)
        return None


# ---------------------------------------------------------------------------
# Qwen: фильтр важности
# ---------------------------------------------------------------------------

def parse_verdict(response_text: str) -> int | None:
    m = re.search(r"[01]", response_text or "")       # первая цифра 0/1 в ответе
    return int(m.group()) if m else None


async def judge_importance(http: httpx.AsyncClient, text: str, group: str = "gold") -> int | None:
    """
    1 — важно, 0 — мусор, None — ответа нет (НЕ считается мусором, пост будет проверен позже).
    group: "gold" — исходный фильтр золота/макро, "stocks" — фильтр акций США.
    """
    prompt = JUDGE_PROMPT_STOCKS if group == "stocks" else JUDGE_PROMPT
    payload = {
        "model": config.QWEN_MODEL,
        "prompt": prompt.format(text=text),
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 16},
    }
    try:
        r = await http.post(f"{config.OLLAMA_URL}/api/generate", json=payload)
        r.raise_for_status()
        return parse_verdict(r.json().get("response", ""))
    except Exception as e:
        logger.warning("Ошибка Qwen (фильтр важности): %s", e)
        await asyncio.sleep(config.QWEN_ERROR_PAUSE)
        return None


