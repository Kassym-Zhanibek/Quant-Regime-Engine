import threading

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification


class FinBERTGatekeeper:
    def __init__(self, model_name: str = "ProsusAI/finbert"):
        # GPU, если есть (RTX 4090 ускоряет скоринг Telegram-постов в разы), иначе CPU
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name).to(self.device)
        self.model.eval()
        # RSS-воркер и Telegram-воркер работают в разных потоках с ОДНОЙ моделью — инференс под замком
        self._lock = threading.Lock()

    def probabilities(self, text: str) -> tuple[float, float, float]:
        """Сырые вероятности (positive, negative, neutral) без округления."""
        inputs = self.tokenizer(
            text,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=512
        ).to(self.device)

        with self._lock, torch.no_grad():
            outputs = self.model(**inputs)
            probs = torch.nn.functional.softmax(outputs.logits, dim=-1)[0].cpu()

        # В ProsusAI/finbert: 0 -> positive, 1 -> negative, 2 -> neutral
        return probs[0].item(), probs[1].item(), probs[2].item()

    def analyze_text(self, text: str, neutral_threshold: float = 0.95) -> dict:
        """
        1 этап конвейера:
        Возвращает 3 вероятности (positive, negative, neutral),
        вычисляет базовый балл FinBERT (pos - neg) и выполняет фильтрацию шума.
        """
        p_positive, p_negative, p_neutral = self.probabilities(text)

        # Первичный балл FinBERT (-1.0 ... +1.0)
        finbert_score = p_positive - p_negative

        # Базовая отсечка информационного шума
        is_relevant = p_neutral < neutral_threshold

        return {
            "p_positive": round(p_positive, 4),
            "p_negative": round(p_negative, 4),
            "p_neutral": round(p_neutral, 4),
            "finbert_score": round(finbert_score, 4),
            "is_relevant": is_relevant
        }


_instance: FinBERTGatekeeper | None = None
_instance_lock = threading.Lock()


def get_finbert() -> FinBERTGatekeeper:
    """Единый экземпляр FinBERT на всё приложение (RSS-конвейер и Telegram-конвейер делят одну модель)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = FinBERTGatekeeper()
        return _instance

