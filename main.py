from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from typing import List, Optional
import database, models, schemas, auth
import random

# Initialize DB tables
models.Base.metadata.create_all(bind=database.engine)

app = FastAPI(title="Birge API - MVP Backend")

# Разрешаем запросы с нашего React приложения (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
    Алгоритм поиска попутчиков. 
    Ищет поездки с противоположной ролью, близкими точками (с нормализацией текста) и в то же время.
    """
    target_role = "passenger" if role == "driver" else "driver"
    
    from sqlalchemy.orm import joinedload
    
    # 1. Фильтруем все активные поездки с нужной ролью и JOIN-им пользователя
    potential_trips = db.query(models.Trip).options(joinedload(models.Trip.user)).filter(
        models.Trip.role == target_role,
        models.Trip.status == "active",
        models.Trip.user_id != user_id # не предлагаем себя
    ).all()

    def normalize(text: str) -> str:
        # Убираем лишние пробелы и приводим к нижнему регистру
        return " ".join(text.lower().split())

    norm_origin = normalize(origin)
    norm_dest = normalize(destination)

    matches = []
    for trip in potential_trips:
        # 2. Сравниваем локации (Текстовый substring + нормализация)
        trip_origin = normalize(trip.origin)
        trip_dest = normalize(trip.destination)
        
        from_match = trip_origin in norm_origin or norm_origin in trip_origin
        to_match = trip_dest in norm_dest or norm_dest in trip_dest

        if not (from_match and to_match):
            continue
        
        # 3. Сравниваем время (разница не более 30 минут)
        try:
            my_hour, my_min = map(int, time.split(":"))
            t_hour, t_min = map(int, trip.time.split(":"))
            
            if my_hour == t_hour and abs(my_min - t_min) <= 30:
                matches.append(trip)
        except ValueError:
            pass # игнорируем ошибки парсинга времени

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
