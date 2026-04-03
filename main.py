from fastapi import FastAPI, Depends, HTTPException, Header
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
    with database.engine.connect() as conn:
        migrations = [
            "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats INTEGER DEFAULT 3",
            "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats_taken INTEGER DEFAULT 0",
            "ALTER TABLE trips ADD COLUMN IF NOT EXISTS date TEXT",
        ]
        for sql in migrations:
            try:
                conn.execute(database.text(sql))
            except Exception:
                pass
        conn.commit()

try:
    from sqlalchemy import text as _text
    database.text = _text
    run_migrations()
except Exception as e:
    print(f"Migration warning: {e}")

app = FastAPI(title="Birge API - MVP Backend")

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


# --- USERS ---
@app.get("/users/", response_model=List[schemas.User])
def read_users(db: Session = Depends(get_db)):
    users = db.query(models.User).all()
    # Populate mock reviews if they don't have any, just for testing
    for user in users:
        pass # In a real app we might load relationships here explicitly if needed
    return users

@app.post("/users/", response_model=schemas.User)
def create_user(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.phone == user.phone).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Phone already registered")
    
    new_user = models.User(name=user.name, phone=user.phone, photo=user.photo)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

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
def read_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Возвращает профиль текущего пользователя по JWT токену."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = _extract_token(authorization)
    user_id = auth.verify_token(token)
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
        
    user = db.query(models.User).filter(models.User.id == user_id).first()
    
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
        
    return user

@app.patch("/users/me", response_model=schemas.User)
def update_current_user(user_update: schemas.UserUpdate, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Обновляет профиль и данные машины текущего пользователя."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = _extract_token(authorization)
    user_id = auth.verify_token(token)
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
        
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    update_data = user_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(user, key, value)
        
    db.commit()
    db.refresh(user)
    return user

@app.get("/users/me/trips", response_model=List[schemas.Trip])
def read_current_user_trips(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Возвращает историю поездок пользователя."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = _extract_token(authorization)
    user_id = auth.verify_token(token)
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
        
    # Возвращаем поездки (только завершенные или заматченные можно оставить, но пока все)
    trips = db.query(models.Trip).filter(models.Trip.user_id == user_id).order_by(models.Trip.id.desc()).all()
    return trips

# --- TRIPS ---
@app.post("/trips/", response_model=schemas.Trip)
def create_trip(trip: schemas.TripCreate, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == trip.user_id).first()
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
def find_matches(user_id: int, role: str, origin: str, destination: str, time: str, date: Optional[str] = None, db: Session = Depends(get_db)):
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
        models.Trip.user_id != user_id
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
    """Хранит asyncio.Queue для каждого requester_id.
    Когда водитель принимает запрос — кладём event в очередь,
    пассажир получает его мгновенно через открытый EventSource.
    """
    def __init__(self):
        self._queues: dict[int, asyncio.Queue] = {}

    def subscribe(self, user_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues[user_id] = q
        return q

    def unsubscribe(self, user_id: int) -> None:
        self._queues.pop(user_id, None)

    async def push(self, user_id: int, data: str) -> None:
        q = self._queues.get(user_id)
        if q:
            await q.put(data)


sse_manager = SSEManager()


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
            sse_manager.unsubscribe(requester_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.websocket("/ws/chat/{trip_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, trip_id: str, user_id: int, db: Session = Depends(get_db)):
    await manager.connect(websocket, trip_id)
    try:
        while True:
            data = await websocket.receive_text()
            
            # Парсим JSON чтобы получить текст сообщения (ожидаем {"text": "привет"})
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
                timestamp=timestamp
            )
            db.add(new_msg)
            db.commit()
            db.refresh(new_msg)

            # Рассылаем всем участникам в формате JSON (вместе с ID отправителя и временем)
            response_data = {
                "id": new_msg.id,
                "text": text,
                "sender_id": user_id,
                "timestamp": timestamp,
                "trip_id": int(trip_id)
            }
            await manager.broadcast(json.dumps(response_data), trip_id)

    except WebSocketDisconnect:
        manager.disconnect(websocket, trip_id)

@app.get("/trips/{trip_id}/messages", response_model=List[schemas.ChatMessage])
def get_trip_messages(trip_id: int, db: Session = Depends(get_db)):
    """Возвращает историю сообщений поездки"""
    messages = db.query(models.ChatMessage).filter(models.ChatMessage.trip_id == trip_id).all()
    return messages


@app.patch("/trips/{trip_id}/status")
def update_trip_status(trip_id: int, payload: dict, db: Session = Depends(get_db)):
    """Обновляет статус поездки: active | matched | completed"""
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    new_status = payload.get("status", "active")
    trip.status = new_status
    db.commit()
    return {"id": trip_id, "status": new_status}


@app.delete("/trips/{trip_id}")
def cancel_trip(trip_id: int, authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Отмена активной поездки текущим пользователем."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = _extract_token(authorization)
    user_id = auth.verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    trip = db.query(models.Trip).filter(
        models.Trip.id == trip_id,
        models.Trip.user_id == user_id
    ).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found or access denied")

    db.delete(trip)
    db.commit()
    return {"id": trip_id, "deleted": True}


# --- REVIEWS ---
@app.post("/reviews/", response_model=schemas.Review)
def create_review(review: schemas.ReviewCreate, db: Session = Depends(get_db)):
    """Сохраняет отзыв о пользователе после поездки"""
    # Проверяем что пользователь существует
    user = db.query(models.User).filter(models.User.id == review.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Сохраняем отзыв
    new_review = models.Review(
        user_id=review.user_id,
        author_name=review.author_name,
        text=review.text,
        rating=review.rating
    )
    db.add(new_review)

    # Пересчитываем средний рейтинг
    all_reviews = db.query(models.Review).filter(models.Review.user_id == review.user_id).all()
    total_rating = sum(r.rating for r in all_reviews) + review.rating
    count = len(all_reviews) + 1
    # Округляем до 1 знака без round(float, int) — Pyre2 не резолвит этот overload
    user.trust_rating = int(total_rating * 10 / count) / 10.0

    db.commit()
    db.refresh(new_review)
    return new_review


# --- TRIP REQUESTS (запросы на поездку) ---

@app.post("/trip-requests/")
def send_trip_request(payload: dict, db: Session = Depends(get_db)):
    """Пассажир отправляет запрос водителю на поездку"""
    trip_id = payload.get("trip_id")          # ID поездки водителя
    requester_trip_id = payload.get("requester_trip_id")  # ID поездки пассажира
    requester_id = payload.get("requester_id")  # ID пассажира
    driver_id = payload.get("driver_id")       # ID водителя

    # Проверяем не отправлял ли уже
    existing = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.requester_id == requester_id,
        models.TripRequest.status == "pending"
    ).first()
    if existing:
        return {"id": existing.id, "status": "pending", "message": "Already sent"}

    req = models.TripRequest(
        trip_id=trip_id,
        requester_trip_id=requester_trip_id,
        requester_id=requester_id,
        driver_id=driver_id,
        status="pending",
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return {"id": req.id, "status": "pending"}


@app.get("/trip-requests/incoming/{user_id}")
def get_incoming_requests(user_id: int, db: Session = Depends(get_db)):
    """Водитель получает входящие запросы (pending)"""
    requests = db.query(models.TripRequest).filter(
        models.TripRequest.driver_id == user_id,
        models.TripRequest.status == "pending"
    ).all()

    result = []
    for r in requests:
        requester = db.query(models.User).filter(models.User.id == r.requester_id).first()
        driver = db.query(models.User).filter(models.User.id == r.driver_id).first()
        driver_trip = db.query(models.Trip).filter(models.Trip.id == r.trip_id).first()
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
            "status": r.status,
            "created_at": r.created_at,
            # Данные машины водителя
            "driver_car_model": driver.car_model if driver else None,
            "driver_car_plate": driver.car_plate if driver else None,
            "driver_car_color": driver.car_color if driver else None,
        })
    return result


@app.patch("/trip-requests/{request_id}")
async def respond_to_request(request_id: int, payload: dict, db: Session = Depends(get_db)):
    """Водитель принимает (accepted) или отклоняет (declined) запрос.
    При accepted — мгновенно уведомляет пассажира через SSE.
    """
    req = db.query(models.TripRequest).filter(models.TripRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    new_status = payload.get("status")  # 'accepted' or 'declined'
    req.status = new_status

    driver = db.query(models.User).filter(models.User.id == req.driver_id).first()

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
        "driver_name": driver.name if driver else None,
        "driver_photo": driver.photo if driver else None,
        "driver_car_model": driver.car_model if driver else None,
        "driver_car_plate": driver.car_plate if driver else None,
        "driver_car_color": driver.car_color if driver else None,
    }

    # Мгновенно уведомляем пассажира через SSE (не ждём polling)
    if new_status in ("accepted", "declined") and req.requester_id:
        import json as _json
        await sse_manager.push(req.requester_id, _json.dumps(response_payload))

    return response_payload


@app.get("/trip-requests/status/{requester_id}/{trip_id}")
def check_request_status(requester_id: int, trip_id: int, db: Session = Depends(get_db)):
    """Пассажир проверяет статус своего запроса (polling)"""
    req = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.requester_id == requester_id,
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
def get_trip_passengers(trip_id: int, db: Session = Depends(get_db)):
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

        # Сохраняем или обновляем привязку
        from models import TelegramBinding
        binding = db.query(TelegramBinding).filter(TelegramBinding.phone == phone).first()
        if binding:
            binding.chat_id = chat_id
        else:
            db.add(TelegramBinding(phone=phone, chat_id=chat_id))
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


@app.on_event("startup")
async def set_telegram_webhook():
    """Автоматически регистрирует webhook в Telegram при старте сервера."""
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


# --- DEBUG: проверка привязок Telegram (временный эндпоинт) ---
@app.get("/debug/telegram-bindings")
def debug_telegram_bindings(db: Session = Depends(get_db)):
    """Показывает все записи TelegramBinding в БД. Удалить после отладки."""
    from models import TelegramBinding
    bindings = db.query(TelegramBinding).all()
    return [
        {
            "id": b.id,
            "phone": b.phone,
            "phone_digits": "".join(filter(str.isdigit, b.phone)),
            "chat_id": b.chat_id,
            "created_at": str(b.created_at),
        }
        for b in bindings
    ]
