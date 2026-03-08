from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
import database, models, schemas, auth
import random

# Initialize DB tables
models.Base.metadata.create_all(bind=database.engine)

# Миграция: добавляем новые колонки если их нет (для существующих баз)
def run_migrations():
    with database.engine.connect() as conn:
        migrations = [
            "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats INTEGER DEFAULT 3",
            "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats_taken INTEGER DEFAULT 0",
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
def send_code(payload: dict, db: Session = Depends(get_db)):
    """Имитирует отправку SMS-кода. В продакшене здесь будет реальный SMS провайдер."""
    phone = payload.get("phone", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number required")
    
    # Генерируем 6-значный код
    code = str(random.randint(100000, 999999))
    auth.save_sms_code(phone, code)
    
    # В продакшене: отправить SMS через Twilio/SMS.ru
    print(f"\n{'='*40}")
    print(f"📱 SMS ДЛЯ {phone}")
    print(f"👉 ВАШ КОД БИРГЕ: {code} 👈")
    print(f"{'='*40}\n")
    
    return {"message": "Code sent. Пожалуйста, проверьте SMS (или консоль сервера)."}

@app.post("/auth/verify-code")
def verify_code(payload: dict, db: Session = Depends(get_db)):
    """Проверяет SMS-код и выдает JWT токен."""
    phone = payload.get("phone", "").strip()
    code = payload.get("code", "").strip()
    name = payload.get("name", "").strip()
    
    if not auth.verify_sms_code(phone, code):
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
        
    token = authorization[7:]
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
        
    token = authorization[7:]
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
        
    token = authorization[7:]
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
    
    new_trip = models.Trip(**trip.model_dump())
    db.add(new_trip)
    db.commit()
    db.refresh(new_trip)
    return new_trip

@app.get("/trips/matches", response_model=List[schemas.Trip])
def find_matches(user_id: int, role: str, origin: str, destination: str, time: str, db: Session = Depends(get_db)):
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
    
    potential_trips = db.query(models.Trip).options(joinedload(models.Trip.user)).filter(
        models.Trip.role == target_role,
        models.Trip.status == "active",
        models.Trip.user_id != user_id
    ).all()

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
from typing import Dict, List
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
                del self.active_connections[trip_id]

    async def broadcast(self, message: str, trip_id: str):
        if trip_id in self.active_connections:
            for connection in self.active_connections[trip_id]:
                await connection.send_text(message)

manager = ConnectionManager()

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
    user.trust_rating = round(total_rating / count, 1)

    db.commit()
    db.refresh(new_review)
    return new_review


# --- TRIP REQUESTS (запросы на поездку) ---

from datetime import datetime

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
def respond_to_request(request_id: int, payload: dict, db: Session = Depends(get_db)):
    """Водитель принимает (accepted) или отклоняет (declined) запрос"""
    req = db.query(models.TripRequest).filter(models.TripRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    new_status = payload.get("status")  # 'accepted' or 'declined'
    req.status = new_status

    # Если принят — увеличиваем счётчик мест явно
    if new_status == "accepted":
        trip = db.query(models.Trip).filter(models.Trip.id == req.trip_id).first()
        if trip:
            trip.seats_taken = (trip.seats_taken or 0) + 1
            # Если все места заняты — закрываем набор
            if trip.seats_taken >= (trip.seats or 3):
                trip.status = "matched"

    db.commit()
    return {
        "id": req.id,
        "status": new_status,
        "trip_id": req.trip_id,
        "requester_trip_id": req.requester_trip_id,
        "requester_id": req.requester_id,
        "driver_id": req.driver_id,
    }


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
