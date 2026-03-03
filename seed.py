"""
Скрипт для заполнения базы данных тестовыми данными.
Запуск: .\venv\Scripts\python.exe seed.py
"""
from database import SessionLocal, engine
import models

# Создаем таблицы если их нет
models.Base.metadata.create_all(bind=engine)

db = SessionLocal()

# Очищаем таблицы
db.query(models.Review).delete()
db.query(models.Trip).delete()
db.query(models.User).delete()
db.commit()

# Создаем тестовых пользователей
users = [
    models.User(
        id=1, name="Азамат (Вы)", phone="+996 555 123 456",
        photo="https://i.pravatar.cc/150?u=azamat",
        trust_rating=4.8, is_verified=True, trips_today=0, registered_since="2022"
    ),
    models.User(
        id=2, name="Айнура", phone="+996 777 987 654",
        photo="https://i.pravatar.cc/150?u=ainura",
        trust_rating=4.9, is_verified=True, trips_today=0, registered_since="2023"
    ),
    models.User(
        id=3, name="Бекзат (Водитель)", phone="+996 700 555 333",
        photo="https://i.pravatar.cc/150?u=bekzat",
        trust_rating=4.6, is_verified=True, trips_today=1, registered_since="2021"
    ),
]

for u in users:
    db.add(u)
db.commit()

# Добавляем отзывы
reviews = [
    models.Review(user_id=1, author_name="Айнура", text="Отличный попутчик, вежливый!", rating=5),
    models.Review(user_id=1, author_name="Бекзат", text="Всегда вовремя.", rating=5),
    models.Review(user_id=3, author_name="Азамат", text="Хороший водитель, едет аккуратно.", rating=4),
]
for r in reviews:
    db.add(r)
db.commit()

# Добавляем поездки от других пользователей (чтобы был мэтч!)
trips = [
    # Водитель едет из Ала-Арчи в ЦУМ в 08:00
    models.Trip(
        user_id=3, role="driver",
        origin="Жилмассив Ала-Арча", destination="ЦУМ (Центр)",
        time="08:00", status="active"
    ),
    # Пассажир едет из Ала-Арчи в ЦУМ в 08:20
    models.Trip(
        user_id=2, role="passenger",
        origin="Жилмассив Ала-Арча (у магазина)", destination="ЦУМ (Центр)",
        time="08:20", status="active"
    ),
]
for t in trips:
    db.add(t)
db.commit()
db.close()

print("✅ База данных заполнена тестовыми данными!")
print(f"   Пользователей: {len(users)}")
print(f"   Поездок: {len(trips)}")
print(f"   Отзывов: {len(reviews)}")
