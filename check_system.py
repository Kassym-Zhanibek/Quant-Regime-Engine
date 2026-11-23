import os
import sys

# Добавляем корень проекта в пути импорта
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.storage.db import init_db, get_sentiment_history, DB_PATH
from app.main import app

print("=" * 50)
print("ДИАГНОСТИКА СИСТЕМЫ QUANT REGIME")
print("=" * 50)

print(f"1. Абсолютный путь к БД: {os.path.abspath(DB_PATH)}")
print(f"   Файл БД существует? -> {os.path.exists(DB_PATH)}")

# 1. Проверяем инициализацию и историю
init_db()
history = get_sentiment_history()
print(f"\n2. Записей в истории: {len(history)}")
print(f"   Содержимое истории: {history}")

# 2. Проверяем маршруты FastAPI
routes = [route.path for route in app.routes]
print("\n3. Зарегистрированные API-маршруты:")
for r in routes:
    print(f"   - {r}")

if "/api/v1/sentiment/history" in routes:
    print("\n✅ МАРШРУТ ИСТОРИИ ЗАРЕГИСТРИРОВАН УСПЕШНО!")
else:
    print("\n❌ ОШИБКА: Маршрут /api/v1/sentiment/history отсутствует в app.main!")
print("=" * 50)
