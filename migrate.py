"""
Скрипт миграции БД — добавляет недостающие колонки в таблицу users.
Запустите один раз: python migrate.py
"""
import sqlite3

conn = sqlite3.connect("birge.db")
cursor = conn.cursor()

migrations = [
    ("last_trip_date", "ALTER TABLE users ADD COLUMN last_trip_date TEXT"),
    ("car_model",      "ALTER TABLE users ADD COLUMN car_model TEXT"),
    ("car_color",      "ALTER TABLE users ADD COLUMN car_color TEXT"),
    ("car_plate",      "ALTER TABLE users ADD COLUMN car_plate TEXT"),
]

# Получаем текущие колонки
cursor.execute("PRAGMA table_info(users)")
existing_columns = {row[1] for row in cursor.fetchall()}
print(f"Текущие колонки: {existing_columns}")

for col_name, sql in migrations:
    if col_name not in existing_columns:
        cursor.execute(sql)
        print(f"✅ Добавлена колонка: {col_name}")
    else:
        print(f"⏭️  Колонка уже есть: {col_name}")

conn.commit()
conn.close()
print("\n🎉 Миграция завершена! Теперь запустите uvicorn заново.")
