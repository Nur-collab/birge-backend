from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Header, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional
import database, models, schemas, auth
import random
import asyncio

# Initialize DB tables
models.Base.metadata.create_all(bind=database.engine)

# Миграция: добавляем новые колонки если их нет (для существующих баз)
def run_migrations():
    """Безопасная миграция — работает и в SQLite и в PostgreSQL."""
    is_sqlite = str(database.engine.url).startswith("sqlite")
    with database.engine.connect() as conn:
        if is_sqlite:
            # SQLite не поддерживает IF NOT EXISTS для ALTER TABLE
            migrations = [
                "ALTER TABLE trips ADD COLUMN seats INTEGER DEFAULT 3",
                "ALTER TABLE trips ADD COLUMN seats_taken INTEGER DEFAULT 0",
                "ALTER TABLE trips ADD COLUMN date TEXT",
                "ALTER TABLE trips ADD COLUMN price_per_seat INTEGER DEFAULT 0",
                # phone_normalized: только цифры, индекс для быстрого поиска chat_id
                "ALTER TABLE telegram_bindings ADD COLUMN phone_normalized TEXT",
                # reminder_sent: флаг отправки напоминания (не теряется при рестарте)
                "ALTER TABLE trips ADD COLUMN reminder_sent BOOLEAN DEFAULT 0",
            ]
        else:
            # PostgreSQL поддерживает IF NOT EXISTS
            migrations = [
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats INTEGER DEFAULT 3",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats_taken INTEGER DEFAULT 0",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS date TEXT",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS price_per_seat INTEGER DEFAULT 0",
                "ALTER TABLE telegram_bindings ADD COLUMN IF NOT EXISTS phone_normalized TEXT",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE",
            ]
        for sql in migrations:
            try:
                conn.execute(database.text(sql))
                conn.commit()
            except Exception:
                # Колонка уже существует — игнорируем
                pass

try:
    from sqlalchemy import text as _text
    database.text = _text
    run_migrations()
except Exception as e:
    print(f"Migration warning: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle: startup → yield → shutdown."""
    await startup_tasks()
    yield
    # shutdown — ничего не делаем

app = FastAPI(title="Birge API - MVP Backend", lifespan=lifespan)

@app.get("/health")
def health_check():
    """Публичный эндпоинт для мониторинга (UptimeRobot, etc.). Без авторизации."""
    return {"status": "ok"}

# Разрешаем запросы с нашего React приложения (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://birge-nine.vercel.app",
        "https://birge.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _extract_token(authorization: Optional[str]) -> str:
    """Извлекает JWT токен из заголовка Authorization: Bearer <token>.
    Вызывать только после проверки, что значение уже не None и начинается с 'Bearer '.
    """
    # Используем partition вместо slice, чтобы избежать ложной ошибки Pyre2
    # 'Bearer eyJ...' -> partition(' ') -> ('Bearer', ' ', 'eyJ...')
    return (authorization or "").partition(" ")[2]


def get_current_user(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),  # для SSE (EventSource не шлёт заголовки)
    db: Session = Depends(get_db),
) -> models.User:
    """Возвращает текущего пользователя по JWT-токену.

    Принимает токениз:
      - `Authorization: Bearer <token>` заголовок (стандартные запросы)
      - `?token=<token>` query-параметр (SSE/EventSource, не поддерживает кастомные заголовки)
    """
    # 1. Извлекаем сырой токен
    raw_token: Optional[str] = None
    if authorization and authorization.startswith("Bearer "):
        raw_token = _extract_token(authorization)
    elif token:
        raw_token = token

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. Проверяем подпись
    user_id = auth.verify_token(raw_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 3. Загружаем пользователя
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


# --- USERS ---
# --- AUTH ---
@app.post("/auth/send-code")
async def send_code(payload: dict, db: Session = Depends(get_db)):
    """Отправка кода через Telegram Bot (или консоль как fallback). Rate limit: 60 сек."""
    phone = payload.get("phone", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number required")

    # Генерируем 6-значный код
    code = str(random.randint(100000, 999999))

    # Сохраняем в БД (с проверкой rate limit)
    ok, error_msg = auth.save_sms_code(phone, code, db)
    if not ok:
        raise HTTPException(status_code=429, detail=error_msg)

    # Пробуем отправить через Telegram Bot
    chat_id = auth.get_telegram_chat_id(phone, db)
    tg_sent = False
    if chat_id:
        tg_sent = await auth.send_telegram_code(chat_id, code)

    # Fallback: выводим в консоль (для разработки)
    if not tg_sent:
        print(f"\n{'='*40}")
        print(f"📱 КОД ДЛЯ {phone}")
        print(f"👉 ВАШ КОД БИРГЕ: {code} 👈")
        if not chat_id:
            print(f"⚠️  Telegram не привязан — код только в консоли")
        print(f"{'='*40}\n")

    channel = "telegram" if tg_sent else "console"
    return {"message": f"Code sent via {channel}", "tg_linked": bool(chat_id)}

@app.post("/auth/verify-code")
def verify_code(payload: dict, db: Session = Depends(get_db)):
    """Проверяет SMS-код и выдает JWT токен."""
    phone = payload.get("phone", "").strip()
    code = payload.get("code", "").strip()
    name = payload.get("name", "").strip()

    if not auth.verify_sms_code(phone, code, db):
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    
    # Ищем существующего пользователя или создаём нового
    user = db.query(models.User).filter(models.User.phone == phone).first()
    if not user:
        user = models.User(
            name=name or "Новый пользователь",
            phone=phone,
            photo=f"https://i.pravatar.cc/150?u={phone}"
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    
    # Создаём JWT токен
    token = auth.create_access_token({"sub": str(user.id)})
    return {"access_token": token, "token_type": "bearer", "user_id": user.id}

@app.get("/users/me", response_model=schemas.User)
def read_current_user(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Возвращает профиль текущего пользователя по JWT токену.
    Eager-load отзывов: без этого Profile.jsx покажет 0 отзывов.
    """
    from sqlalchemy.orm import selectinload
    user = (
        db.query(models.User)
        .options(selectinload(models.User.reviews))
        .filter(models.User.id == current_user.id)
        .first()
    )
    return user or current_user

@app.patch("/users/me", response_model=schemas.User)
def update_current_user(
    user_update: schemas.UserUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Обновляет профиль и данные машины текущего пользователя."""
    update_data = user_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(current_user, key, value)
    db.commit()
    db.refresh(current_user)
    return current_user


@app.get("/users/me/trips", response_model=List[schemas.Trip])
def read_current_user_trips(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Возвращает историю поездок пользователя."""
    return (
        db.query(models.Trip)
        .filter(models.Trip.user_id == current_user.id)
        .order_by(models.Trip.id.desc())
        .all()
    )


@app.get("/users/me/active-trip")
def get_my_active_trip(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Возвращает active-поездку с датой сегодня, если такая есть.
    Фронтенд вызывает при входе чтобы предложить возобновить поиск."""
    import datetime as _dt
    today = _dt.date.today().isoformat()

    trip = (
        db.query(models.Trip)
        .filter(
            models.Trip.user_id == current_user.id,
            models.Trip.status == "active",
            models.Trip.date == today,
        )
        .order_by(models.Trip.id.desc())
        .first()
    )

    if not trip:
        return {"found": False}

    return {
        "found": True,
        "trip_id": trip.id,
        "role": trip.role,
        "origin": trip.origin,
        "destination": trip.destination,
        "time": trip.time,
        "date": trip.date,
        "seats": trip.seats or 3,
    }

@app.post("/users/me/verify")
async def verify_user(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Верификация аккаунта через Telegram.
    Если TelegramBinding есть — ставим is_verified=True сразу.
    Если нет — возвращаем ссылку на бота.
    """
    if current_user.is_verified:
        return {"status": "already_verified"}

    chat_id = auth.get_telegram_chat_id(current_user.phone, db)
    if chat_id:
        current_user.is_verified = True
        db.commit()
        db.refresh(current_user)
        msg = (
            f"✅ *Аккаунт верифицирован!*\n\n"
            f"Привет, {current_user.name}! Твой аккаунт Birge теперь имеет значок ✅ на профиле.\n"
            f"Другие пользователи будут точно знать, что ты настоящий — это повышает доверие 🙏"
        )
        await auth.send_telegram_message(chat_id, msg)
        return {"status": "verified", "message": "Аккаунт успешно верифицирован"}
    else:
        # Telegram не привязан — возвращаем инструкцию
        import os
        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "BirgeBot")
        # Форматируем номер для deep-link (убираем '+' и пробелы)
        phone_clean = "".join(filter(str.isdigit, current_user.phone))
        bot_url = f"https://t.me/{bot_username}?start={phone_clean}"
        return {
            "status": "need_telegram",
            "bot_url": bot_url,
            "message": (
                "Откройте Telegram-бота, "
                "отправьте /start и запустите верификацию снова"
            )
        }


@app.get("/users/me/scheduled-trips")
def read_scheduled_trips(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Возвращает запланированные поездки пользователя (и как водителя, и как пассажира)."""
    import datetime
    today_str = datetime.date.today().isoformat()
    user_id = current_user.id

    result = []

    # 1. Поездки где пользователь — ВОДИТЕЛЬ (его Trip со статусом scheduled)
    driver_trips = db.query(models.Trip).filter(
        models.Trip.user_id == user_id,
        models.Trip.role == "driver",
        models.Trip.status == "scheduled",
        models.Trip.date > today_str
    ).order_by(models.Trip.date, models.Trip.time).all()

    for trip in driver_trips:
        # Собираем список принятых пассажиров
        accepted_reqs = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id == trip.id,
            models.TripRequest.status == "accepted"
        ).all()
        passengers = []
        for r in accepted_reqs:
            passenger_user = db.query(models.User).filter(models.User.id == r.requester_id).first()
            if passenger_user:
                passengers.append({
                    "id": passenger_user.id,
                    "name": passenger_user.name,
                    "photo": passenger_user.photo,
                    "trust_rating": passenger_user.trust_rating,
                    "is_verified": passenger_user.is_verified,
                    "request_id": r.id,
                })
        result.append({
            "trip_id": trip.id,
            "role": "driver",
            "origin": trip.origin,
            "destination": trip.destination,
            "date": trip.date,
            "time": trip.time,
            "seats": trip.seats or 3,
            "seats_taken": trip.seats_taken or 0,
            "passengers": passengers,
            "driver": None,
        })

    # 2. Поездки где пользователь — ПАССАЖИР (принятые TripRequest для будущих поездок)
    from sqlalchemy.orm import joinedload as _jl2

    accepted_requests = (
        db.query(models.TripRequest)
        .options(_jl2(models.TripRequest.driver))  # eager-load водителя без N+1
        .filter(
            models.TripRequest.requester_id == user_id,
            models.TripRequest.status == "accepted",
        )
        .all()
    )

    # Bulk-загрузка поездок водителей одним IN-запросом
    passenger_trip_ids = list({r.trip_id for r in accepted_requests if r.trip_id})
    passenger_trips_map: dict[int, models.Trip] = {}
    if passenger_trip_ids:
        passenger_trips_map = {
            t.id: t
            for t in db.query(models.Trip)
            .filter(models.Trip.id.in_(passenger_trip_ids))
            .all()
        }

    for req in accepted_requests:
        driver_trip = passenger_trips_map.get(req.trip_id)
        if not driver_trip:
            continue
        # Только будущие поездки
        trip_date = driver_trip.date or ""
        if not trip_date or trip_date <= today_str:
            continue
        driver_user = req.driver  # уже загружен через joinedload
        result.append({
            "trip_id": req.trip_id,
            "requester_trip_id": req.requester_trip_id,
            "role": "passenger",
            "origin": driver_trip.origin,
            "destination": driver_trip.destination,
            "date": driver_trip.date,
            "time": driver_trip.time,
            "seats": driver_trip.seats or 3,
            "seats_taken": driver_trip.seats_taken or 0,
            "passengers": [],
            "driver": {
                "id": driver_user.id if driver_user else None,
                "name": driver_user.name if driver_user else "Водитель",
                "photo": driver_user.photo if driver_user else "",
                "trust_rating": driver_user.trust_rating if driver_user else 5.0,
                "is_verified": driver_user.is_verified if driver_user else False,
                "car_model": driver_user.car_model if driver_user else None,
                "car_color": driver_user.car_color if driver_user else None,
                "car_plate": driver_user.car_plate if driver_user else None,
            } if driver_user else None,
        })

    # Сортируем итоговый список по дате
    result.sort(key=lambda x: (x.get("date", ""), x.get("time", "")))
    return result


@app.post("/trips/", response_model=schemas.Trip)
def create_trip(trip: schemas.TripCreate, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    user = current_user
    if not user:
         raise HTTPException(status_code=404, detail="User not found")

    import datetime
    today_str = datetime.date.today().isoformat()

    # Сбрасываем лимит, если начался новый день
    if user.last_trip_date != today_str:
        user.trips_today = 0
        user.last_trip_date = today_str

    # Проверка лимита водителя
    if trip.role == "driver":
        if user.trips_today >= 3:
            raise HTTPException(status_code=400, detail="Daily driver trip limit (3) exceeded")
        
        # Увеличиваем счетчик поездок только для водителя
        user.trips_today += 1

    db.commit()

    trip_data = trip.model_dump()
    trip_data["user_id"] = user.id

    # Если дата поездки в будущем — ставим статус 'scheduled', иначе 'active'
    trip_date = trip_data.get("date")
    if trip_date and trip_date > today_str:
        trip_data["status"] = "scheduled"
    else:
        trip_data["status"] = "active"

    new_trip = models.Trip(**trip_data)
    db.add(new_trip)
    db.commit()
    db.refresh(new_trip)
    return new_trip

@app.get("/trips/matches", response_model=List[schemas.Trip])
def find_matches(role: str, origin: str, destination: str, time: str, date: Optional[str] = None, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Улучшенный алгоритм поиска попутчиков:
    1. Haversine-расстояние по геокоординатам (радиус 2км)
    2. Fallback: текстовый поиск с нормализацией
    3. Время: разница не более 30 минут
    """
    import math

    # Геословарь Бишкека
    BISHKEK_LOCATIONS = {
        # Жилмассивы
        'ала-арча': (42.8380, 74.5520),
        'ала арча': (42.8380, 74.5520),
        'аламедин': (42.8700, 74.5200),
        'джал': (42.8200, 74.5700),
        'кара-жыгач': (42.8650, 74.5050),
        'кара жыгач': (42.8650, 74.5050),
        'асанбай': (42.8450, 74.6300),
        'тунгуч': (42.8920, 74.5980),
        'восток-5': (42.8750, 74.6100),
        'восток 5': (42.8750, 74.6100),
        'мкр 7': (42.8550, 74.5650),
        'мкр. 7': (42.8550, 74.5650),
        'ак-орго': (42.8600, 74.5300),
        'ак орго': (42.8600, 74.5300),
        'кожомкул': (42.8500, 74.5100),
        'деревня': (42.8850, 74.6200),
        'кеминчи': (42.8950, 74.5600),
        'бай тюбе': (42.8300, 74.5900),
        'байтюбе': (42.8300, 74.5900),
        'новпостройка': (42.8650, 74.5800),
        'интернациональный аэропорт': (42.8474, 74.4776),
        # Центр и районы
        'цум': (42.8760, 74.6050),
        'центр': (42.8746, 74.5698),
        'площадь': (42.8762, 74.6036),
        'ала-тоо': (42.8762, 74.6036),
        'ала тоо': (42.8762, 74.6036),
        'ошский базар': (42.8820, 74.5780),
        'ошский': (42.8820, 74.5780),
        'дордой': (42.9100, 74.6300),
        'кузнечная': (42.8780, 74.5850),
        'карпинка': (42.8670, 74.6010),
        'рушания': (42.8900, 74.5700),
        'цум-азия': (42.8760, 74.6050),
        'горький парк': (42.8820, 74.5980),
        'фучик': (42.8800, 74.5900),
        'ботанический сад': (42.8850, 74.5820),
        'октябрьская': (42.8730, 74.5950),
        'истанкул': (42.8680, 74.5720),
        'меркентиль': (42.8940, 74.5940),
        # Аэропорт / Автовокзал
        'манас': (42.8474, 74.4776),
        'аэропорт': (42.8474, 74.4776),
        'западный автовокзал': (42.8700, 74.5500),
        'западный': (42.8700, 74.5500),
        'восточный автовокзал': (42.8780, 74.6400),
    }

    def geocode(location_str: str):
        """Ищем координаты по подстроке из словаря"""
        if not location_str:
            return None
        lower = location_str.lower().strip()
        for key, coords in BISHKEK_LOCATIONS.items():
            if key in lower:
                return coords
        return None

    def haversine_km(coord1, coord2):
        """Расстояние между двумя координатами в километрах"""
        lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
        lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        return 6371 * 2 * math.asin(math.sqrt(a))

    def locations_match(loc_a: str, loc_b: str, radius_km: float = 2.0) -> bool:
        """True, если локации идентичны или находятся в радиусе radius_km"""
        # Сначала пробуем гео-матчинг
        coords_a = geocode(loc_a)
        coords_b = geocode(loc_b)
        if coords_a and coords_b:
            return haversine_km(coords_a, coords_b) <= radius_km
        # Fallback: текстовый поиск
        a_norm = " ".join(loc_a.lower().split())
        b_norm = " ".join(loc_b.lower().split())
        return a_norm in b_norm or b_norm in a_norm

    target_role = "passenger" if role == "driver" else "driver"
    
    from sqlalchemy.orm import joinedload
    from sqlalchemy import or_ as _or

    # Показываем как 'active', так и 'scheduled' поездки
    trip_query = db.query(models.Trip).options(joinedload(models.Trip.user)).filter(
        models.Trip.role == target_role,
        _or(models.Trip.status == "active", models.Trip.status == "scheduled"),
        models.Trip.user_id != current_user.id
    )

    # Фильтр по дате: если дата передана — показываем только её (+ legacy записи без даты)
    if date:
        trip_query = trip_query.filter(
            _or(models.Trip.date == date, models.Trip.date == None)
        )

    potential_trips = trip_query.all()

    matches = []
    for trip in potential_trips:
        from_match = locations_match(origin, trip.origin)
        to_match = locations_match(destination, trip.destination)

        if not (from_match and to_match):
            continue
        
        # Сравниваем время (разница не более 30 минут)
        try:
            my_hour, my_min = map(int, time.split(":"))
            t_hour, t_min = map(int, trip.time.split(":"))
            my_total = my_hour * 60 + my_min
            t_total = t_hour * 60 + t_min
            if abs(my_total - t_total) <= 30:
                matches.append(trip)
        except ValueError:
            pass

    return matches

# --- WEBSOCKET CHAT ---
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict
import json
from datetime import datetime

# Хранилище активных подключений: trip_id -> список WebSocket соединений
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, trip_id: str):
        await websocket.accept()
        if trip_id not in self.active_connections:
            self.active_connections[trip_id] = []
        self.active_connections[trip_id].append(websocket)

    def disconnect(self, websocket: WebSocket, trip_id: str):
        if trip_id in self.active_connections:
            self.active_connections[trip_id].remove(websocket)
            if not self.active_connections[trip_id]:
                self.active_connections.pop(trip_id)

    async def broadcast(self, message: str, trip_id: str):
        if trip_id in self.active_connections:
            for connection in self.active_connections[trip_id]:
                await connection.send_text(message)

manager = ConnectionManager()


# --- SSE MANAGER (мгновенные уведомления пассажиру) ---
class SSEManager:
    """Хранит список asyncio.Queue для каждого user_id.

    Поддерживает несколько одновременных подключений одного пользователя
    (например, две открытые вкладки браузера) — все получают события.
    """
    def __init__(self):
        self._queues: dict[int, list[asyncio.Queue]] = {}

    def subscribe(self, user_id: int) -> asyncio.Queue:
        """Создаёт новую очередь и добавляет её в список для user_id."""
        q: asyncio.Queue = asyncio.Queue()
        if user_id not in self._queues:
            self._queues[user_id] = []
        self._queues[user_id].append(q)
        return q

    def unsubscribe(self, user_id: int, q: asyncio.Queue) -> None:
        """Удаляет конкретную очередь. Если очередей не осталось — чистим словарь."""
        queues = self._queues.get(user_id)
        if queues and q in queues:
            queues.remove(q)
        if not queues:
            self._queues.pop(user_id, None)

    async def push(self, user_id: int, data: str) -> None:
        """Отправляет событие во все активные очереди пользователя."""
        for q in list(self._queues.get(user_id, [])):
            await q.put(data)


sse_manager = SSEManager()       # passenger: receives accept/decline events
driver_sse_manager = SSEManager() # driver: receives new trip-request events


@app.get("/trip-requests/driver-events/{driver_id}")
async def driver_trip_request_events(
    driver_id: int,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = None,
):
    """SSE-стрим для водителя.
    Открывается пока водитель в поиске или в активной поездке;
    при каждом новом запросе от пассажира водитель получает event мгновенно.
    Токен: Authorization header ИЛИ ?token= (EventSource не поддерживает хедеры).
    """
    raw_token: str = ""
    if authorization and authorization.startswith("Bearer "):
        raw_token = _extract_token(authorization)
    elif token:
        raw_token = token

    if not raw_token:
        return StreamingResponse(
            iter(["event: error\ndata: unauthorized\n\n"]),
            media_type="text/event-stream",
        )

    token_user_id = auth.verify_token(raw_token)
    if not token_user_id or token_user_id != driver_id:
        return StreamingResponse(
            iter(["event: error\ndata: forbidden\n\n"]),
            media_type="text/event-stream",
        )

    q = driver_sse_manager.subscribe(driver_id)

    async def event_generator():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"event: new_request\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            driver_sse_manager.unsubscribe(driver_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/trip-requests/events/{requester_id}")
async def trip_request_events(
    requester_id: int,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = None,  # query param для EventSource (не поддерживает кастомные хедеры)
):
    """SSE-стрим для пассажира. Открывается на время поиска;
    когда водитель принимает запрос — пассажир получает event мгновенно.
    Токен: Authorization header (обычные запросы) ИЛИ ?token= (EventSource).
    """
    # Получаем токен из header или query
    raw_token: str = ""
    if authorization and authorization.startswith("Bearer "):
        raw_token = _extract_token(authorization)
    elif token:
        raw_token = token

    if not raw_token:
        return StreamingResponse(
            iter(["event: error\ndata: unauthorized\n\n"]),
            media_type="text/event-stream"
        )

    token_user_id = auth.verify_token(raw_token)
    if not token_user_id or token_user_id != requester_id:
        return StreamingResponse(
            iter(["event: error\ndata: forbidden\n\n"]),
            media_type="text/event-stream"
        )

    q = sse_manager.subscribe(requester_id)

    async def event_generator():
        try:
            # Keepalive — каждые 20 сек чтобы соединение не обрывалось
            while True:
                try:
                    data = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"event: request_update\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sse_manager.unsubscribe(requester_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.websocket("/ws/chat/{trip_id}/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    trip_id: str,
    user_id: int,
    token: Optional[str] = None,   # ?token= из query-строки (Chat.jsx передаёт при connect)
    db: Session = Depends(get_db),
):
    """WebSocket чат поездки с JWT-авторизацией.

    Клиент подключается как:
        wss://host/ws/chat/{trip_id}/{user_id}?token=<jwt>
    Токен проверяется до подключения; несовпадение user_id → закрытие 1008.
    """
    # --- Авторизация ---
    if not token:
        await websocket.close(code=1008, reason="Token required")
        return
    verified_user_id = auth.verify_token(token)
    if not verified_user_id or verified_user_id != user_id:
        await websocket.close(code=1008, reason="Invalid or forbidden token")
        return

    await manager.connect(websocket, trip_id)
    try:
        while True:
            data = await websocket.receive_text()

            # Парсим JSON: ожидаем {"text": "привет"}
            try:
                msg_data = json.loads(data)
                text = msg_data.get("text", "")
            except json.JSONDecodeError:
                text = data

            timestamp = datetime.now().strftime("%I:%M %p")

            # Сохраняем в БД
            new_msg = models.ChatMessage(
                trip_id=int(trip_id),
                sender_id=user_id,
                text=text,
                timestamp=timestamp,
            )
            db.add(new_msg)
            db.commit()
            db.refresh(new_msg)

            # Рассылаем всем участникам комнаты
            response_data = {
                "id": new_msg.id,
                "text": text,
                "sender_id": user_id,
                "timestamp": timestamp,
                "trip_id": int(trip_id),
            }
            await manager.broadcast(json.dumps(response_data), trip_id)

    except WebSocketDisconnect:
        manager.disconnect(websocket, trip_id)

@app.get("/trips/{trip_id}/messages", response_model=List[schemas.ChatMessage])
def get_trip_messages(trip_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Возвращает историю сообщений поездки"""
    messages = db.query(models.ChatMessage).filter(models.ChatMessage.trip_id == trip_id).all()
    return messages


@app.patch("/trips/{trip_id}/status")
async def update_trip_status(
    trip_id: int,
    payload: dict,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Обновляет статус поездки.

    Поддерживаемые переходы:
      active | matched | in_progress | completed — управляет водитель.
      passenger_cancelled — пассажир выходит из поездки; водитель получает SSE.
    """
    import json as _json

    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    new_status = payload.get("status", "active")

    # --- Пассажир отменяет участие ---
    if new_status == "passenger_cancelled":
        # Ищем принятый запрос текущего пользователя на эту поездку
        req = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id == trip_id,
            models.TripRequest.requester_id == current_user.id,
            models.TripRequest.status == "accepted",
        ).first()
        if req:
            req.status = "cancelled"
            # Освобождаем место в поездке
            if trip.seats_taken and trip.seats_taken > 0:
                trip.seats_taken -= 1
            db.commit()
            # Уведомляем водителя через SSE
            event_data = _json.dumps({
                "event": "passenger_cancelled",
                "trip_id": trip_id,
                "passenger_name": current_user.name,
            })
            await driver_sse_manager.push(trip.user_id, event_data)
        return {"id": trip_id, "status": "passenger_cancelled"}

    # --- Водитель управляет статусом ---
    if trip.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the driver can change trip status")

    trip.status = new_status
    db.commit()

    # При завершении — уведомляем всех принятых пассажиров
    if new_status == "completed":
        accepted = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id == trip_id,
            models.TripRequest.status == "accepted",
        ).all()
        event_data = _json.dumps({"event": "trip_completed", "trip_id": trip_id})
        for req in accepted:
            await sse_manager.push(req.requester_id, event_data)

    return {"id": trip_id, "status": new_status}


@app.delete("/trips/{trip_id}")
def cancel_trip(
    trip_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Отмена активной поездки текущим пользователем."""
    trip = db.query(models.Trip).filter(
        models.Trip.id == trip_id,
        models.Trip.user_id == current_user.id,
    ).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found or access denied")

    db.delete(trip)
    db.commit()
    return {"id": trip_id, "deleted": True}


@app.post("/trips/{trip_id}/panic")
async def send_panic_alert(
    trip_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Пассажир нажал тревожную кнопку — отправляем уведомление в Telegram поддержки.
    TELEGRAM_ADMIN_CHAT_ID должен быть задан в env (chat_id группы или личного чата поддержки).
    """
    import os, httpx

    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()

    admin_chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not bot_token:
        return {"sent": False, "reason": "bot_token_missing"}

    # Если нет admin chat — fallback: отправляем самому пользователю
    if not admin_chat_id:
        admin_chat_id = auth.get_telegram_chat_id(current_user.phone, db)

    if not admin_chat_id:
        return {"sent": False, "reason": "no_chat_id"}

    msg_parts = [
        "🚨 *ТРЕВОГА — ПАССАЖИР В ОПАСНОСТИ!*",
        "",
        f"👤 *{current_user.name}*",
        f"📱 `{current_user.phone}`",
        f"🚗 Поездка #*{trip_id}*",
    ]
    if trip:
        msg_parts.append(f"📍 {trip.origin} → {trip.destination}")
        msg_parts.append(f"🕐 {trip.time}")
    msg_parts += ["", "⚠️ Срочно свяжитесь с пассажиром!"]
    message = "\n".join(msg_parts)

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": admin_chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                },
            )
        return {"sent": resp.status_code == 200}
    except Exception as e:
        return {"sent": False, "reason": str(e)}


@app.post("/reviews/", response_model=schemas.Review)
def create_review(
    review: schemas.ReviewCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Сохраняет отзыв о пользователе после поездки.

    Правила:
    - Нельзя оставить отзыв самому себе.
    - Один пользователь может оставить только один отзыв конкретному человеку.
      (защита от накрутки рейтинга)
    """
    if review.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot review yourself")

    user = db.query(models.User).filter(models.User.id == review.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Защита от дублирующих отзывов: один автор — один отзыв на пользователя
    existing = db.query(models.Review).filter(
        models.Review.user_id == review.user_id,
        models.Review.author_name == current_user.name,
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You have already reviewed this user",
        )

    new_review = models.Review(
        user_id=review.user_id,
        author_name=current_user.name,
        text=review.text,
        rating=review.rating,
    )
    db.add(new_review)

    # Пересчитываем средний рейтинг (включая новый)
    all_reviews = db.query(models.Review).filter(models.Review.user_id == review.user_id).all()
    total_rating = sum(r.rating for r in all_reviews) + review.rating
    count = len(all_reviews) + 1
    user.trust_rating = int(total_rating * 10 / count) / 10.0

    db.commit()
    db.refresh(new_review)
    return new_review


# --- TRIP REQUESTS (запросы на поездку) ---

@app.post("/trip-requests/")
async def send_trip_request(
    payload: dict,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Пассажир отправляет запрос водителю на поездку.
    requester_id берётся из JWT (защита от spoofing).
    После сохранения в БД немедленно уведомляет водителя через SSE.
    """
    trip_id = payload.get("trip_id")                     # ID поездки водителя
    requester_trip_id = payload.get("requester_trip_id") # ID поездки пассажира
    requester_id = current_user.id                       # ID пассажира — только из токена!
    driver_id = payload.get("driver_id")                 # ID водителя

    # Проверяем, не отправлял ли уже
    existing = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.requester_id == requester_id,
        models.TripRequest.status == "pending",
    ).first()
    if existing:
        return {"id": existing.id, "status": "pending", "message": "Already sent"}

    req = models.TripRequest(
        trip_id=trip_id,
        requester_trip_id=requester_trip_id,
        requester_id=requester_id,
        driver_id=driver_id,
        status="pending",
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    # Собираем данные пассажира для SSE-пуша водителю
    driver_trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()

    sse_payload = {
        "id": req.id,
        "trip_id": trip_id,
        "requester_trip_id": requester_trip_id,
        "requester_id": requester_id,
        "driver_id": driver_id,
        "status": "pending",
        "requester_name": current_user.name,
        "requester_photo": current_user.photo,
        "requester_rating": current_user.trust_rating,
        "origin": driver_trip.origin if driver_trip else "",
        "destination": driver_trip.destination if driver_trip else "",
        "time": driver_trip.time if driver_trip else "",
        "date": driver_trip.date if driver_trip else "",
    }

    # Мгновенно уведомляем водителя через SSE (если он подписан)
    if driver_id:
        import json as _json
        await driver_sse_manager.push(driver_id, _json.dumps(sse_payload))

    return {"id": req.id, "status": "pending"}


@app.get("/trip-requests/incoming/")
def get_incoming_requests(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Водитель получает входящие запросы (pending).
    Возвращает только запросы адресованные ТЕКУЩЕМУ пользователю.
    """
    from sqlalchemy.orm import joinedload as _jl

    # Один запрос с eager-load пользователей (без N+1)
    requests = (
        db.query(models.TripRequest)
        .options(
            _jl(models.TripRequest.requester),
            _jl(models.TripRequest.driver),
        )
        .filter(
            models.TripRequest.driver_id == current_user.id,
            models.TripRequest.status == "pending",
        )
        .all()
    )

    # Bulk-загрузка поездок водителя одним IN-запросом (вместо N запросов в цикле)
    trip_ids = list({r.trip_id for r in requests if r.trip_id})
    trips_map: dict[int, models.Trip] = {}
    if trip_ids:
        trips_map = {
            t.id: t
            for t in db.query(models.Trip).filter(models.Trip.id.in_(trip_ids)).all()
        }

    result = []
    for r in requests:
        requester = r.requester
        driver = r.driver
        driver_trip = trips_map.get(r.trip_id)
        result.append({
            "id": r.id,
            "trip_id": r.trip_id,
            "requester_trip_id": r.requester_trip_id,
            "requester_id": r.requester_id,
            "requester_name": requester.name if requester else "Пассажир",
            "requester_photo": requester.photo if requester else "",
            "requester_rating": requester.trust_rating if requester else 5.0,
            "origin": driver_trip.origin if driver_trip else "",
            "destination": driver_trip.destination if driver_trip else "",
            "time": driver_trip.time if driver_trip else "",
            "date": driver_trip.date if driver_trip else "",
            "status": r.status,
            "created_at": r.created_at,
            "driver_car_model": driver.car_model if driver else None,
            "driver_car_plate": driver.car_plate if driver else None,
            "driver_car_color": driver.car_color if driver else None,
        })
    return result


@app.patch("/trip-requests/{request_id}")
async def respond_to_request(
    request_id: int,
    payload: dict,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Водитель принимает (accepted) или отклоняет (declined) запрос.
    При accepted — мгновенно уведомляет пассажира через SSE.
    Только водитель (driver_id == current_user.id) может менять статус своего запроса.
    """
    req = db.query(models.TripRequest).filter(models.TripRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.driver_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")

    new_status = payload.get("status")  # 'accepted' or 'declined'
    req.status = new_status

    driver = current_user  # уже загружен через Depends

    # Если принят — увеличиваем счётчик мест явно
    if new_status == "accepted":
        trip = db.query(models.Trip).filter(models.Trip.id == req.trip_id).first()
        if trip:
            trip.seats_taken = (trip.seats_taken or 0) + 1
            # Если все места заняты — закрываем набор
            if trip.seats_taken >= (trip.seats or 3):
                trip.status = "matched"

    db.commit()

    response_payload = {
        "id": req.id,
        "status": new_status,
        "trip_id": req.trip_id,
        "requester_trip_id": req.requester_trip_id,
        "requester_id": req.requester_id,
        "driver_id": req.driver_id,
        "driver_name": driver.name,
        "driver_photo": driver.photo,
        "driver_car_model": driver.car_model,
        "driver_car_plate": driver.car_plate,
        "driver_car_color": driver.car_color,
    }

    # Мгновенно уведомляем пассажира через SSE (не ждём polling)
    if new_status in ("accepted", "declined") and req.requester_id:
        import json as _json
        await sse_manager.push(req.requester_id, _json.dumps(response_payload))

    return response_payload


@app.get("/trip-requests/status/{trip_id}")
def check_request_status(
    trip_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Пассажир проверяет статус своего запроса на поездку водителя (polling).
    requester_id берётся из JWT — пользователь не может запросить чужой статус.
    """
    req = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.requester_id == current_user.id,
    ).order_by(models.TripRequest.id.desc()).first()

    if not req:
        return {"status": "not_sent"}

    driver = db.query(models.User).filter(models.User.id == req.driver_id).first()
    return {
        "id": req.id,
        "status": req.status,
        "trip_id": req.trip_id,
        "requester_trip_id": req.requester_trip_id,
        "driver_id": req.driver_id,
        "driver_name": driver.name if driver else None,
        "driver_photo": driver.photo if driver else None,
        "driver_car_model": driver.car_model if driver else None,
        "driver_car_plate": driver.car_plate if driver else None,
        "driver_car_color": driver.car_color if driver else None,
    }


@app.get("/trips/{trip_id}/passengers")
def get_trip_passengers(trip_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Список принятых пассажиров для поездки водителя"""
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    accepted = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.status == "accepted"
    ).all()

    passengers = []
    for r in accepted:
        user = db.query(models.User).filter(models.User.id == r.requester_id).first()
        if user:
            passengers.append({
                "id": user.id,
                "name": user.name,
                "photo": user.photo,
                "trust_rating": user.trust_rating,
                "is_verified": user.is_verified,
                "request_id": r.id,
            })

    return {
        "trip_id": trip_id,
        "seats": trip.seats or 3,
        "seats_taken": trip.seats_taken or 0,
        "passengers": passengers,
    }


# --- TELEGRAM BOT WEBHOOK ---
@app.post("/bot/webhook")
async def telegram_webhook(payload: dict, db: Session = Depends(get_db)):
    """
    Telegram шлёт сюда все входящие сообщения.
    Пользователь пишет боту свой номер телефона → бот сохраняет phone↔chat_id.
    Формат: пользователь пишет: "996555123456" или "+996 555 123 456"
    """
    message = payload.get("message", {})
    if not message:
        return {"ok": True}

    chat_id: int = message.get("chat", {}).get("id")
    text: str = (message.get("text") or "").strip()
    first_name: str = message.get("from", {}).get("first_name", "")

    if not chat_id or not text:
        return {"ok": True}

    # Команда /start — отвечаем приветствием
    if text.startswith("/start"):
        await auth.send_telegram_message(
            chat_id,
            f"👋 Привет, {first_name}!\n\n"
            f"Я бот приложения *Birge* — сервиса совместных поездок в Бишкеке 🚗\n\n"
            f"Чтобы привязать твой номер, отправь его в формате:\n"
            f"`996XXXXXXXXX`\n\n"
            f"Например: `996555123456`"
        )
        return {"ok": True}

    # Пробуем распарсить номер телефона
    digits = "".join(filter(str.isdigit, text))
    if len(digits) >= 10:
        # Нормализуем к формату "+996 XXX XXX XXX"
        if digits.startswith("996") and len(digits) == 12:
            phone = f"+996 {digits[3:6]} {digits[6:9]} {digits[9:12]}"
        elif digits.startswith("0") and len(digits) == 10:
            phone = f"+996 {digits[1:4]} {digits[4:7]} {digits[7:10]}"
        elif len(digits) == 9:
            phone = f"+996 {digits[0:3]} {digits[3:6]} {digits[6:9]}"
        else:
            phone = f"+996 {digits[-9:-6]} {digits[-6:-3]} {digits[-3:]}"

        # Сохраняем или обновляем привязку (включая phone_normalized для быстрого поиска)
        from models import TelegramBinding
        phone_normalized_val = "".join(filter(str.isdigit, phone))
        binding = db.query(TelegramBinding).filter(TelegramBinding.phone == phone).first()
        if binding:
            binding.chat_id = chat_id
            binding.phone_normalized = phone_normalized_val  # обновляем при каждом входе
        else:
            db.add(TelegramBinding(
                phone=phone,
                phone_normalized=phone_normalized_val,
                chat_id=chat_id,
            ))
        db.commit()

        await auth.send_telegram_message(
            chat_id,
            f"✅ Номер *{phone}* привязан!\n\n"
            f"Теперь коды для входа в Birge будут приходить сюда 🎉"
        )
    else:
        await auth.send_telegram_message(
            chat_id,
            "❓ Не понял. Отправь свой номер телефона, например:\n`996555123456`"
        )

    return {"ok": True}


# Глобальный in-memory set удалён — состояние теперь хранится в БД (Trip.reminder_sent).
# Это означает что напоминания не дублируются при рестарте сервера.


async def _reminder_loop():
    """Background loop: каждую минуту проверяет запланированные поездки.
    Если до начала поездки осталось 50-70 минут — отправляем Telegram-напоминание
    водителю И всем принятым пассажирам.
    """
    import datetime as _dt
    from models import TelegramBinding

    while True:
        await asyncio.sleep(60)  # проверяем раз в минуту
        try:
            now = _dt.datetime.now()
            today = now.date().isoformat()

            # Окно проверки: от 50 до 70 минут до начала поездки
            window_from = (now + _dt.timedelta(minutes=50)).strftime("%H:%M")
            window_to   = (now + _dt.timedelta(minutes=70)).strftime("%H:%M")

            with database.SessionLocal() as db:
                # Запланированные поездки на сегодня в нужном окне времени
                trips = db.query(models.Trip).filter(
                    models.Trip.status == "scheduled",
                    models.Trip.date == today,
                    models.Trip.time >= window_from,
                    models.Trip.time <= window_to,
                ).all()

                for trip in trips:
                    if trip.reminder_sent:
                        continue  # уже отправлено (персистентно в БД, не теряется при рестарте)

                    # --- Напоминаем ВОДИТЕЛЯ ---
                    driver_user = db.query(models.User).filter(
                        models.User.id == trip.user_id
                    ).first()
                    if driver_user:
                        driver_chat_id = auth.get_telegram_chat_id(driver_user.phone, db)
                        if driver_chat_id:
                            msg = (
                                f"🚗 *Напоминание о поездке!*\n\n"
                                f"До отправления остался примерно *час*.\n"
                                f"↘️ {trip.origin}\n"
                                f"↗️ {trip.destination}\n"
                                f"🕕 {trip.time} — {trip.date}\n\n"
                                f"Роль: *Водитель* 🚘"
                            )
                            await auth.send_telegram_message(driver_chat_id, msg)
                            print(f"[Reminder] Водитель trip#{trip.id} → chat {driver_chat_id}")

                    # --- Напоминаем всех ПРИНЯТЫХ ПАССАЖИРОВ ---
                    accepted = db.query(models.TripRequest).filter(
                        models.TripRequest.trip_id == trip.id,
                        models.TripRequest.status == "accepted",
                    ).all()

                    for req in accepted:
                        passenger = db.query(models.User).filter(models.User.id == req.requester_id).first()
                        if not passenger:
                            continue
                        passenger_chat_id = auth.get_telegram_chat_id(passenger.phone, db)
                        if not passenger_chat_id:
                            continue

                        car_info = ""
                        if driver_user and driver_user.car_model:
                            car_info = f"🚙 {driver_user.car_model}"
                            if driver_user.car_color:
                                car_info += f" · {driver_user.car_color}"
                            if driver_user.car_plate:
                                car_info += f" `{driver_user.car_plate}`"
                            car_info = f"\n{car_info}"

                        msg = (
                            f"🚗 *Напоминание о поездке!*\n\n"
                            f"До отправления остался примерно *час*.\n"
                            f"↘️ {trip.origin}\n"
                            f"↗️ {trip.destination}\n"
                            f"🕕 {trip.time} — {trip.date}\n"
                            f"👤 Водитель: *{driver_user.name if driver_user else 'Неизвестно'}*"
                            f"{car_info}\n\n"
                            f"Роль: *Пассажир* 🤺"
                        )
                        await auth.send_telegram_message(passenger_chat_id, msg)
                        print(f"[Reminder] Пассажир uid={passenger.id} trip#{trip.id} → chat {passenger_chat_id}")

                    # Отмечаем в БД — персистентно переживёт любой рестарт
                    trip.reminder_sent = True
                    db.commit()

        except Exception as e:
            # Не даём задаче упасть — продолжаем циклить
            print(f"[Reminder] Ошибка в цикле: {e}")


async def startup_tasks():
    """1. Удаляем просроченные active-поездки (дата уже прошла).
    2. Регистрируем Telegram webhook.
    3. Запускаем фоновую задачу напоминаний за 1 час до поездки.

    НИКОГДА не трогаем scheduled-поездки — это будущие поездки, они важны.
    """
    import datetime as _dt
    today = _dt.date.today().isoformat()

    # --- Очистка просроченных active-поездок ---
    with database.SessionLocal() as db:
        # Случай 1: дата поездки < сегодня (вчерашние и старше)
        stale = db.query(models.Trip).filter(
            models.Trip.status == "active",
            models.Trip.date != None,
            models.Trip.date < today,
        ).all()
        # Случай 2: дата не заполнена (legacy-записи без даты) — тоже считаем просроченными,
        # если их ID ниже минимального ID поездки последних 7 дней (грубое, но без created_at лучше не сломать)
        stale_no_date = db.query(models.Trip).filter(
            models.Trip.status == "active",
            models.Trip.date == None,
        ).all()
        all_stale = stale + stale_no_date
        count = len(all_stale)
        for trip in all_stale:
            db.delete(trip)
        if count:
            db.commit()
            print(f"[Startup] Удалено {count} просроченных active-поездок")

    # --- Запуск фоновой задачи напоминаний ---
    asyncio.create_task(_reminder_loop())
    print("[Reminder] ✅ Фоновая задача напоминаний запущена (проверка каждые 60 сек)")

    # --- Telegram webhook ---
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not token or not render_url:
        print("[Telegram] BOT_TOKEN или RENDER_EXTERNAL_URL не заданы — webhook не зарегистрирован")
        return
    webhook_url = f"{render_url}/bot/webhook"
    async with __import__('httpx').AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url}
        )
        result = resp.json()
        if result.get("ok"):
            print(f"[Telegram] ✅ Webhook установлен: {webhook_url}")
        else:
            print(f"[Telegram] ❌ Ошибка webhook: {result}")


# debug/telegram-bindings endpoint removed — данные пользователей не должны быть публичными.
