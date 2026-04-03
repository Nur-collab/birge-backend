"""Патч main.py: добавляет дату в миграцию и find_matches"""
import re

with open('main.py', 'rb') as f:
    content = f.read().decode('utf-8')

changed = []

# --- 1. Migration ---
old1 = '"ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats_taken INTEGER DEFAULT 0",'
new1 = '"ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats_taken INTEGER DEFAULT 0",\n            "ALTER TABLE trips ADD COLUMN IF NOT EXISTS date TEXT",'
if old1 in content:
    content = content.replace(old1, new1, 1)
    changed.append('migration OK')
else:
    changed.append('migration NOT FOUND')

# --- 2. find_matches signature ---
old2 = 'def find_matches(user_id: int, role: str, origin: str, destination: str, time: str, db: Session = Depends(get_db)):'
new2 = 'def find_matches(user_id: int, role: str, origin: str, destination: str, time: str, date: Optional[str] = None, db: Session = Depends(get_db)):'
if old2 in content:
    content = content.replace(old2, new2, 1)
    changed.append('signature OK')
else:
    changed.append('signature NOT FOUND')

# --- 3. find_matches query: add date filter ---
# Find the joinedload block and replace
old3_marker = '    potential_trips = db.query(models.Trip).options(joinedload(models.Trip.user)).filter('
new3_full = '''    trip_query = db.query(models.Trip).options(joinedload(models.Trip.user)).filter(
        models.Trip.role == target_role,
        models.Trip.status == "active",
        models.Trip.user_id != user_id
    )

    # Фильтр по дате: если дата передана — показываем только её (+ legacy записи без даты)
    if date:
        from sqlalchemy import or_ as _or
        trip_query = trip_query.filter(
            _or(models.Trip.date == date, models.Trip.date == None)
        )

    potential_trips = trip_query.all()'''

# Find and replace the old block (multi-line)
pattern = r'    potential_trips = db\.query\(models\.Trip\)\.options\(joinedload\(models\.Trip\.user\)\)\.filter\(\s+models\.Trip\.role == target_role,\s+models\.Trip\.status == "active",\s+models\.Trip\.user_id != user_id\s+\)\.all\(\)'
if re.search(pattern, content):
    content = re.sub(pattern, new3_full, content, count=1)
    changed.append('query OK')
else:
    changed.append('query NOT FOUND via regex')

with open('main.py', 'wb') as f:
    f.write(content.encode('utf-8'))

print('Results:', ', '.join(changed))
