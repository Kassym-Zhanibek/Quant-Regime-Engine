"""
Очистка текста поста и ключ точного дубля.

clean_post  — копия очистки из step1_split_posts.py (убирает «[канал]:», ссылки, хэштеги, подпись
              @Forex_LiveStream). Именно на ТАКОМ тексте считались исторические скоры (step4) и
              обучалась модель, поэтому живые посты оцениваем так же.
norm_key    — копия norm() из step2.py (точный дубль внутри суток).
"""
import re
from collections import OrderedDict

MD_LINK = re.compile(r"\[([^\]]*)\]\(https?://[^)\s]*\)")            # [текст](ссылка) -> текст
ANY_URL = re.compile(r"https?://\S+")
TRAILING_TOKEN = re.compile(r"\s+(#\w+|@Forex_LiveStream|https?://\S+)\s*$", re.IGNORECASE)
SOURCE_PREFIX = re.compile(r"^\[[^\]]+\]:\s*")
SPACES = re.compile(r"\s+")

TRAIL_PUNCT = re.compile(r"[\s.!?…]+$")
QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "«": '"', "»": '"'})


def clean_post(raw: str) -> tuple[str, str | None]:
    """Возвращает (очищенный текст, хэштеги через пробел или None)."""
    body = SOURCE_PREFIX.sub("", raw, count=1)
    body = MD_LINK.sub(r"\1", body)

    tags: list[str] = []
    body = " " + body.rstrip()
    while True:
        m = TRAILING_TOKEN.search(body)
        if not m:
            break
        token = m.group(1)
        if token.startswith("#"):
            tags.append(token)
        body = body[: m.start()]

    body = ANY_URL.sub("", body)
    text = SPACES.sub(" ", body).strip()

    seen: "OrderedDict[str, None]" = OrderedDict()
    for t in reversed(tags):
        seen.setdefault(t, None)
    return text, (" ".join(seen) or None)


def clean_telegram_message(channel: str, text: str) -> tuple[str, str | None]:
    """Так же, как step1b: старый загрузчик хранил пост как '[канал]: текст в одну строку'."""
    return clean_post(f"[{channel}]: " + text.replace("\n", " "))


def norm_key(text: str) -> str:
    t = text.casefold().translate(QUOTES)
    t = SPACES.sub(" ", t).strip()
    return TRAIL_PUNCT.sub("", t)


CYRILLIC = re.compile(r"[\u0400-\u04FF]")
LETTER = re.compile(r"[^\W\d_]")


def cyrillic_share(text: str) -> float:
    """Доля кириллицы среди всех букв текста (0..1). По ней решаем, применим ли англоязычный FinBERT."""
    letters = len(LETTER.findall(text or ""))
    return len(CYRILLIC.findall(text or "")) / letters if letters else 0.0

