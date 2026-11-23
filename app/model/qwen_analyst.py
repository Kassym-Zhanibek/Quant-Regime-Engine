import httpx
import re
import logging

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL_NAME = "qwen2.5:7b"

GOLD_VALIDATOR_SYSTEM_PROMPT = """
You are a Senior Quantitative Commodity Analyst specializing in Gold (XAU/USD).
Your role is DOUBLE VERIFICATION and VALIDATION of financial news sentiment.

You will receive:
1. News text.
2. Initial FinBERT model assessment (probabilities for Positive, Negative, Neutral and calculated score).

Your Task:
Validate and adjust the sentiment score specifically for GOLD (XAU/USD) prices [-1.0 to +1.0]:
- Check context, financial logic, central bank policy, geopolitical events, and macro impact on Gold.
- Correct FinBERT if it misinterpreted general sentiment vs. direct impact on Gold (e.g., "Hawkish Fed" might sound positive for USD/economy in FinBERT, but is BEARISH for Gold (-0.7)).
- If FinBERT's directional bias matches Gold fundamentals, refine and validate the final score.

Domain Rules for Gold (XAU/USD):
1. Central Bank Buying / Accumulation -> Bullish (+0.6 to +0.9)
2. Geopolitical Escalation / Safe Haven Demand -> Bullish (+0.5 to +0.8)
3. Strong US Dollar / High Treasury Yields / Hawkish Fed (Rate Hikes) -> Bearish (-0.5 to -0.9)
4. Weak NFP / Soft US Inflation / Dovish Fed (Rate Cuts) -> Bullish (+0.4 to +0.8)
5. General Market Risk-Off -> Neutral to Bullish for Gold as Safe Haven

CRITICAL REQUIREMENT: Output ONLY a single numeric float score between -1.0 and +1.0. Do NOT write any explanations or text.
"""


async def verify_and_analyze_gold_sentiment(text: str, finbert_data: dict) -> float:
    """
    2 этап двойной верификации: Qwen выступает в роли эксперта-валидатора,
    проверяя текстовый контекст новости и корректируя балл с учетом специфики XAU/USD.
    """
    user_prompt = (
        f"News Text: \"{text}\"\n\n"
        f"FinBERT Initial Assessment:\n"
        f"- Positive Prob: {finbert_data['p_positive']}\n"
        f"- Negative Prob: {finbert_data['p_negative']}\n"
        f"- Neutral Prob: {finbert_data['p_neutral']}\n"
        f"- FinBERT Raw Score: {finbert_data['finbert_score']}\n\n"
        f"Validate context and output ONLY the final verified float score for Gold (XAU/USD) [-1.0 to +1.0]:"
    )

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": GOLD_VALIDATOR_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        "stream": False,
        "options": {
            "temperature": 0.0,
            "num_predict": 10
        }
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(OLLAMA_URL, json=payload)
            response.raise_for_status()
            result = response.json()

            content = result.get("message", {}).get("content", "").strip()

            match = re.search(r"[-+]?\d*\.\d+|\d+", content)
            if match:
                score = float(match.group())
                return max(-1.0, min(1.0, score))

            logger.warning(f"Qwen вернул нечисловой ответ: {content}")
            return finbert_data["finbert_score"]

    except Exception as e:
        logger.error(f"Ошибка обращения к локальной Ollama (Qwen): {e}")
        # Фоллбэк на балл FinBERT при недоступности Ollama
        return finbert_data["finbert_score"]
