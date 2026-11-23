"""
Разовый вход в Telegram для панели.

Панель работает как фоновый сервис и не может спросить телефон/код, поэтому сессию создаём заранее:

    python scripts/tg_login.py

Скрипт спросит номер телефона и код из Telegram и сохранит файл сессии по пути TG_SESSION
(по умолчанию <корень проекта>/web_session.session).

Если у тебя уже есть web_session.session от telegram_parser*.py — просто СКОПИРУЙ его в корень проекта
(или укажи TG_SESSION) и этот шаг не нужен. Одну и ту же сессию две программы одновременно держать не могут,
поэтому именно копия, а не общий файл.

Нужны переменные окружения TG_API_ID и TG_API_HASH.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402


async def main():
    if not (config.TG_API_ID and config.TG_API_HASH):
        sys.exit("Задай переменные окружения TG_API_ID и TG_API_HASH.")
    from telethon import TelegramClient

    client = TelegramClient(config.TG_SESSION, int(config.TG_API_ID), config.TG_API_HASH)
    await client.start()            # интерактивно: телефон, код, (пароль 2FA)
    me = await client.get_me()
    print(f"✅ Вход выполнен: {me.first_name} (@{me.username}). Сессия: {config.TG_SESSION}.session")
    for ch in config.TG_CHANNELS:
        try:
            ent = await client.get_entity(ch)
            print(f"   канал @{ch}: доступен ({getattr(ent, 'title', ch)})")
        except Exception as e:
            print(f"   канал @{ch}: ❌ {e}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

