from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, Depends, HTTPException, Header, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import Field
import database, models, schemas, auth
import asyncio
import os
import logging
import html
import secrets
import time

logger = logging.getLogger("birge")
IS_PRODUCTION = bool(os.environ.get("RENDER")) or os.environ.get("ENVIRONMENT", "").lower() == "production"

# Initialize DB tables
models.Base.metadata.create_all(bind=database.engine)

# Часовой пояс Бишкека (UTC+6, круглый год без перехода на летнее время)
BISHKEK_TZ = timezone(timedelta(hours=6))

def get_bishkek_now() -> datetime:
    return datetime.now(BISHKEK_TZ)

def get_bishkek_today_str() -> str:
    return datetime.now(BISHKEK_TZ).date().isoformat()

def parse_time_minutes(time_str: str) -> Optional[int]:
    """Безопасный парсинг времени в минуты от полуночи (поддерживает HH:MM, HH:MM:SS, AM/PM)."""
    if not time_str:
        return None
    try:
        clean = str(time_str).strip().upper()
        is_pm = "PM" in clean or "ПП" in clean
        is_am = "AM" in clean or "ДП" in clean
        digits_parts = [p for p in clean.replace(":", " ").split() if p.isdigit()]
        if len(digits_parts) >= 2:
            h = int(digits_parts[0])
            m = int(digits_parts[1])
            if is_pm and h < 12:
                h += 12
            elif is_am and h == 12:
                h = 0
            if 0 <= h < 24 and 0 <= m < 60:
                return h * 60 + m
    except Exception:
        pass
    return None

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
                "ALTER TABLE telegram_bindings ADD COLUMN phone_normalized TEXT",
                "ALTER TABLE trips ADD COLUMN reminder_sent BOOLEAN DEFAULT 0",
                "ALTER TABLE trips ADD COLUMN created_at TIMESTAMP",
                "ALTER TABLE sms_codes ADD COLUMN attempts INTEGER DEFAULT 0",
                "ALTER TABLE reviews ADD COLUMN author_id INTEGER",
                "ALTER TABLE reviews ADD COLUMN trip_id INTEGER",
                "ALTER TABLE users ADD COLUMN mbank_phone TEXT",
                "ALTER TABLE users ADD COLUMN elkart_number TEXT",
            ]
        else:
            # PostgreSQL поддерживает IF NOT EXISTS
            migrations = [
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats INTEGER DEFAULT 3",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS seats_taken INTEGER DEFAULT 0",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS date TEXT",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS price_per_seat INTEGER DEFAULT 0",
                "ALTER TABLE telegram_bindings ADD COLUMN IF NOT EXISTS phone_normalized TEXT",
                "ALTER TABLE telegram_bindings ALTER COLUMN chat_id TYPE BIGINT",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE",
                "ALTER TABLE trips ADD COLUMN IF NOT EXISTS created_at TIMESTAMP",
                "ALTER TABLE sms_codes ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0",
                "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS author_id INTEGER",
                "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS trip_id INTEGER",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS mbank_phone TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS elkart_number TEXT",
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
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:4173",
        "https://birge-nine.vercel.app",
        "https://birge.vercel.app",
        "https://birge-5c900.web.app",
        "https://birge-5c900.firebaseapp.com",
    ],
    allow_origin_regex=r"https://birge[-a-z0-9]*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency импортируется напрямую из database.py
from database import get_db, SessionLocal


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
class VerifyCodePayload(schemas.VerifyCodeRequest):
    name: Optional[str] = Field(default=None, max_length=80)

_SEND_CODE_WINDOW_SECONDS = 600
_SEND_CODE_MAX_PER_WINDOW = 10
_send_code_attempts: dict[str, list[float]] = {}

_VERIFY_CODE_WINDOW_SECONDS = 60
_VERIFY_CODE_MAX_PER_WINDOW = 15
_verify_code_attempts: dict[str, list[float]] = {}

def _client_ip(request: Request) -> str:
    """IP клиента с учётом прокси Render; нужен только для анти-спам лимита."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _prune_rate_dict(d: dict[str, list[float]], window_seconds: int) -> None:
    """Очищает устаревшие IP из словаря лимитов, предотвращая утечку памяти."""
    now = time.time()
    if len(d) > 200:
        expired_keys = [k for k, timestamps in d.items() if not timestamps or (now - timestamps[-1] > window_seconds)]
        for k in expired_keys:
            d.pop(k, None)

def _check_send_code_rate(ip: str) -> None:
    now = time.time()
    _prune_rate_dict(_send_code_attempts, _SEND_CODE_WINDOW_SECONDS)
    attempts = [t for t in _send_code_attempts.get(ip, []) if now - t < _SEND_CODE_WINDOW_SECONDS]
    if len(attempts) >= _SEND_CODE_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Слишком много запросов кода. Попробуйте позже.")
    attempts.append(now)
    _send_code_attempts[ip] = attempts

def _check_verify_code_rate(ip: str) -> None:
    now = time.time()
    _prune_rate_dict(_verify_code_attempts, _VERIFY_CODE_WINDOW_SECONDS)
    attempts = [t for t in _verify_code_attempts.get(ip, []) if now - t < _VERIFY_CODE_WINDOW_SECONDS]
    if len(attempts) >= _VERIFY_CODE_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Слишком много попыток ввода кода. Подождите минуту.")
    attempts.append(now)
    _verify_code_attempts[ip] = attempts

@app.post("/auth/send-code")
async def send_code(req: schemas.SendCodeRequest, request: Request, db: Session = Depends(get_db)):
    """Отправка кода через Telegram Bot. В production без доставки возвращает 503."""
    _check_send_code_rate(_client_ip(request))

    raw_phone = req.phone.strip()
    if not raw_phone:
        raise HTTPException(status_code=400, detail="Phone number required")

    # Нормализуем кыргызский номер
    is_valid, p_e164, p_digits, p_formatted, op_or_err = auth.normalize_kg_phone(raw_phone)
    if not is_valid:
        raise HTTPException(status_code=400, detail=op_or_err)

    # Генерируем 6-значный код криптографически стойким генератором
    code = str(secrets.randbelow(900000) + 100000)

    # Сохраняем в БД по нормализованному формату (с проверкой rate limit)
    ok, error_msg = auth.save_sms_code(p_e164, code, db)
    if not ok:
        raise HTTPException(status_code=429, detail=error_msg)

    # Пробуем отправить через Telegram Bot (ищем по normalized digits)
    chat_id = auth.get_telegram_chat_id(p_digits, db)
    tg_sent = False
    if chat_id:
        tg_sent = await auth.send_telegram_code(chat_id, code)

    if not tg_sent:
        if IS_PRODUCTION:
            # Не оставляем валидный код в БД, если его невозможно доставить.
            db.query(models.SmsCode).filter(models.SmsCode.phone == p_e164).delete(synchronize_session=False)
            db.commit()
            raise HTTPException(
                status_code=503,
                detail="Не удалось доставить код. Привяжите Telegram через @BirgeAuthBot и повторите.",
            )
        # Локальная разработка без Telegram: код печатается только в консоль.
        print(f"\n{'='*40}")
        print(f"📱 КОД ДЛЯ {p_formatted} ({p_e164})")
        print(f"👉 ВАШ КОД БИРГЕ: {code} 👈")
        print(f"{'='*40}\n")

    channel = "telegram" if tg_sent else "console"
    return {"message": f"Code sent via {channel}", "tg_linked": bool(chat_id)}

@app.post("/auth/verify-code")
def verify_code(req: VerifyCodePayload, request: Request, db: Session = Depends(get_db)):
    """Проверяет SMS-код и выдает JWT токен."""
    _check_verify_code_rate(_client_ip(request))
    raw_phone = req.phone.strip()
    code = req.code.strip()
    name = (req.name or "").strip()

    is_valid, p_e164, p_digits, p_formatted, op_or_err = auth.normalize_kg_phone(raw_phone)
    if not is_valid:
        raise HTTPException(status_code=400, detail=op_or_err)

    if not auth.verify_sms_code(p_e164, code, db):
        raise HTTPException(status_code=400, detail="Invalid, expired, or locked code")
    
    # Ищем существующего пользователя по любому представлению номера
    user = (
        db.query(models.User)
        .filter(
            (models.User.phone == p_e164)
            | (models.User.phone == p_formatted)
            | (models.User.phone == p_digits)
            | (models.User.phone.like(f"%{p_digits[-9:]}%"))
        )
        .first()
    )
    if not user:
        # Автоматическая верификация если контакт уже был подтверждён в Telegram боте
        has_tg = db.query(models.TelegramBinding).filter(
            (models.TelegramBinding.phone_normalized == p_digits)
            | (models.TelegramBinding.phone == p_e164)
            | (models.TelegramBinding.phone == p_formatted)
        ).first()
        user = models.User(
            name=name or "Новый пользователь",
            phone=p_e164,
            is_verified=bool(has_tg),
            photo=f"https://i.pravatar.cc/150?u={p_digits}"
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    
    # Создаём JWT токен (sub строго строковый). Профиль отдаём сразу,
    # чтобы клиент не делал второй запрос и не зависел от гонки сети.
    token = auth.create_access_token({"sub": str(user.id)})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "user": schemas.User.model_validate(user).model_dump(),
    }

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

@app.get("/users/{user_id}", response_model=schemas.UserPublic)
def read_user_public(
    user_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Публичный профиль без телефона и платёжных реквизитов."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


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
    """Возвращает историю поездок пользователя (и как водителя, и как пассажира)."""
    created_trips = (
        db.query(models.Trip)
        .filter(models.Trip.user_id == current_user.id)
        .all()
    )
    created_ids = {t.id for t in created_trips}

    # Поездки других водителей, где пользователь был принят или завершил поездку как пассажир
    passenger_requests = (
        db.query(models.TripRequest)
        .filter(
            models.TripRequest.requester_id == current_user.id,
            models.TripRequest.status.in_(["accepted", "completed"]),
        )
        .all()
    )
    joined_trip_ids = [r.trip_id for r in passenger_requests if r.trip_id and r.trip_id not in created_ids]

    joined_trips = []
    if joined_trip_ids:
        joined_trips = (
            db.query(models.Trip)
            .filter(models.Trip.id.in_(joined_trip_ids))
            .all()
        )

    all_trips = created_trips + joined_trips
    all_trips.sort(key=lambda t: t.id, reverse=True)
    return all_trips


@app.get("/users/me/active-trip")
def get_my_active_trip(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    today = get_bishkek_today_str()
    from sqlalchemy import or_ as _or

    # 1. Сначала проверяем: является ли пользователь принятым пассажиром в активной поездке водителя
    accepted_request = (
        db.query(models.TripRequest)
        .filter(
            models.TripRequest.requester_id == current_user.id,
            models.TripRequest.status == "accepted",
        )
        .order_by(models.TripRequest.id.desc())
        .first()
    )

    if accepted_request:
        driver_trip = (
            db.query(models.Trip)
            .filter(
                models.Trip.id == accepted_request.trip_id,
                models.Trip.status.in_(["active", "matched", "in_progress"]),
            )
            .first()
        )
        if driver_trip:
            return {
                "found": True,
                "trip_id": driver_trip.id,
                "role": "passenger",
                "my_role": "passenger",
                "is_driver": False,
                "origin": driver_trip.origin,
                "destination": driver_trip.destination,
                "time": driver_trip.time,
                "date": driver_trip.date,
                "seats": driver_trip.seats or 3,
                "driver_id": driver_trip.user_id,
            }

    # 2. Поездка, созданная самим пользователем (как водитель или пассажир в поиске)
    # in_progress активна независимо от смены суток; active/matched — на сегодня или без даты
    trip = (
        db.query(models.Trip)
        .filter(
            models.Trip.user_id == current_user.id,
            _or(
                models.Trip.status == "in_progress",
                (
                    models.Trip.status.in_(["active", "matched"])
                    & _or(models.Trip.date == today, models.Trip.date.is_(None))
                ),
            ),
        )
        .order_by(models.Trip.id.desc())
        .first()
    )

    if trip:
        return {
            "found": True,
            "trip_id": trip.id,
            "role": trip.role,
            "my_role": trip.role,
            "is_driver": trip.role == "driver",
            "origin": trip.origin,
            "destination": trip.destination,
            "time": trip.time,
            "date": trip.date,
            "seats": trip.seats or 3,
        }

    return {"found": False}

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
    today_str = get_bishkek_today_str()
    user_id = current_user.id

    result = []

    # 1. Поездки где пользователь — ВОДИТЕЛЬ.
    # "matched" для будущей поездки означает «все места заняты», но поездка
    # остаётся запланированной и не должна исчезать из списка.
    driver_trips = db.query(models.Trip).filter(
        models.Trip.user_id == user_id,
        models.Trip.role == "driver",
        models.Trip.status.in_(["scheduled", "matched"]),
        models.Trip.date > today_str
    ).order_by(models.Trip.date, models.Trip.time).all()

    # Bulk-загрузка пассажиров для всех поездок водителя (без N+1)
    driver_trip_ids = [t.id for t in driver_trips]
    accepted_by_trip: dict[int, list[models.TripRequest]] = {}
    passengers_map: dict[int, models.User] = {}
    if driver_trip_ids:
        accepted_reqs = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id.in_(driver_trip_ids),
            models.TripRequest.status == "accepted",
        ).all()
        passenger_ids = {r.requester_id for r in accepted_reqs if r.requester_id}
        if passenger_ids:
            passengers_map = {
                u.id: u for u in db.query(models.User).filter(models.User.id.in_(passenger_ids)).all()
            }
        for r in accepted_reqs:
            accepted_by_trip.setdefault(r.trip_id, []).append(r)

    for trip in driver_trips:
        passengers = []
        for r in accepted_by_trip.get(trip.id, []):
            passenger_user = passengers_map.get(r.requester_id)
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
        # Только будущие поездки, которые ещё не отменены и не завершены
        if driver_trip.status not in ("scheduled", "matched"):
            continue
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

    today_str = get_bishkek_today_str()

    # Сбрасываем лимит, если начался новый день
    if user.last_trip_date != today_str:
        user.trips_today = 0
        user.last_trip_date = today_str

    # Проверка лимита водителя
    if trip.role == "driver":
        if user.trips_today >= 3:
            raise HTTPException(status_code=400, detail="Daily driver trip limit (3) exceeded")

    trip_data = trip.model_dump()
    trip_data["user_id"] = user.id

    # Если дата поездки в будущем — ставим статус 'scheduled', иначе 'active'
    trip_date = trip_data.get("date")
    if trip_date and trip_date > today_str:
        trip_data["status"] = "scheduled"
    else:
        trip_data["status"] = "active"

    valid_trip_fields = {c.name for c in models.Trip.__table__.columns}
    filtered_data = {k: v for k, v in trip_data.items() if k in valid_trip_fields}

    new_trip = models.Trip(**filtered_data)
    if trip.role == "driver":
        user.trips_today += 1
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
        models.Trip.user_id != current_user.id,
        # Не показываем машины, в которых уже нет свободных мест
        _or(
            models.Trip.seats.is_(None),
            models.Trip.seats_taken.is_(None),
            models.Trip.seats_taken < models.Trip.seats,
        ),
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
        my_total = parse_time_minutes(time)
        t_total = parse_time_minutes(trip.time)
        if my_total is not None and t_total is not None:
            diff = abs(my_total - t_total)
            circular_diff = min(diff, 1440 - diff)
            if circular_diff <= 30:
                matches.append(trip)

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
            dead_connections = []
            for connection in list(self.active_connections[trip_id]):
                try:
                    await connection.send_text(message)
                except Exception as e:
                    logger.warning(f"[WS] Error broadcasting to client on trip {trip_id}: {e}")
                    dead_connections.append(connection)
            for dead in dead_connections:
                self.disconnect(dead, trip_id)

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
        """Создаёт новую ограниченную очередь и добавляет её в список для user_id."""
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
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
        """Отправляет событие во все активные очереди пользователя (non-blocking)."""
        for q in list(self._queues.get(user_id, [])):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(data)
                except asyncio.QueueFull:
                    pass


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
        raise HTTPException(status_code=401, detail="Authentication token required")

    token_user_id = auth.verify_token(raw_token)
    if not token_user_id or token_user_id != driver_id:
        raise HTTPException(status_code=403, detail="Access forbidden")

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
        raise HTTPException(status_code=401, detail="Authentication token required")

    token_user_id = auth.verify_token(raw_token)
    if not token_user_id or token_user_id != requester_id:
        raise HTTPException(status_code=403, detail="Access forbidden")

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

def is_trip_participant(db: Session, trip_id: int, user_id: int) -> bool:
    """Проверяет, имеет ли пользователь право доступа к чату поездки (водитель или принятый пассажир)."""
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        return False
    # 1. Создатель поездки (водитель или пассажир)
    if trip.user_id == user_id:
        return True
    # 2. Принятый или завершивший поездку пассажир
    accepted_as_passenger = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.requester_id == user_id,
        models.TripRequest.status.in_(["accepted", "completed"]),
    ).first()
    if accepted_as_passenger:
        return True
    # 3. Водитель, принявший или завершивший поездку пассажира
    accepted_as_driver = db.query(models.TripRequest).filter(
        models.TripRequest.requester_trip_id == trip_id,
        models.TripRequest.driver_id == user_id,
        models.TripRequest.status.in_(["accepted", "completed"]),
    ).first()
    return bool(accepted_as_driver)


@app.websocket("/ws/chat/{trip_id}/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    trip_id: str,
    user_id: int,
    token: Optional[str] = None,   # ?token= из query-строки (Chat.jsx передаёт при connect)
):
    """WebSocket чат поездки с JWT-авторизацией и защитой от исчерпания пула соединений БД.

    Клиент подключается как:
        wss://host/ws/chat/{trip_id}/{user_id}?token=<jwt>
    Токен проверяется до подключения; несовпадение user_id → закрытие 1008.
    """
    # --- Авторизация токена ---
    if not token:
        await websocket.close(code=1008, reason="Token required")
        return
    verified_user_id = auth.verify_token(token)
    if not verified_user_id or verified_user_id != user_id:
        await websocket.close(code=1008, reason="Invalid or forbidden token")
        return

    try:
        trip_int_id = int(trip_id)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid trip_id")
        return

    # Проверяем права доступа к чату поездки (точечная сессия, не блокирующая пул)
    with SessionLocal() as db:
        if not is_trip_participant(db, trip_int_id, user_id):
            await websocket.close(code=1008, reason="Access denied to this trip chat")
            return

    await manager.connect(websocket, trip_id)
    try:
        while True:
            data = await websocket.receive_text()

            # Парсим JSON: ожидаем {"text": "привет"}
            try:
                msg_data = json.loads(data)
                text = (msg_data.get("text") or "").strip() if isinstance(msg_data, dict) else ""
            except (json.JSONDecodeError, AttributeError):
                text = str(data).strip()

            if not text:
                continue
            if len(text) > 1000:
                await websocket.send_text(json.dumps({"error": "message_too_long"}))
                continue

            timestamp = get_bishkek_now().strftime("%I:%M %p")

            # Сохраняем в БД точечно через SessionLocal (соединение освобождается сразу)
            with SessionLocal() as db:
                sender = db.query(models.User).filter(models.User.id == user_id).first()
                sender_name = sender.name if sender else "Пользователь"
                new_msg = models.ChatMessage(
                    trip_id=trip_int_id,
                    sender_id=user_id,
                    text=text,
                    timestamp=timestamp,
                )
                db.add(new_msg)
                db.commit()
                db.refresh(new_msg)
                msg_id = new_msg.id

            # Рассылаем всем участникам комнаты
            response_data = {
                "id": msg_id,
                "text": text,
                "sender_id": user_id,
                "sender_name": sender_name,
                "timestamp": timestamp,
                "trip_id": trip_int_id,
            }
            await manager.broadcast(json.dumps(response_data), trip_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[WS] WebSocket error on trip {trip_id}, user {user_id}: {e}")
    finally:
        manager.disconnect(websocket, trip_id)


@app.get("/trips/{trip_id}/messages", response_model=List[schemas.ChatMessage])
def get_trip_messages(trip_id: int, current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Возвращает историю сообщений поездки с обязательной проверкой прав доступа."""
    if not is_trip_participant(db, trip_id, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied to this trip chat")
    messages = db.query(models.ChatMessage).filter(models.ChatMessage.trip_id == trip_id).all()
    return messages


@app.patch("/trips/{trip_id}/status")
async def update_trip_status(
    trip_id: int,
    status_update: schemas.TripStatusUpdate,
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

    new_status = status_update.status
    today = get_bishkek_today_str()

    def _update_passenger_trip(requester_trip_id: Optional[int], passenger_id: int, completed: bool) -> None:
        """Синхронизирует собственную поездку пассажира с решением водителя."""
        if not requester_trip_id:
            return
        p_trip = db.query(models.Trip).filter(
            models.Trip.id == requester_trip_id,
            models.Trip.user_id == passenger_id,
        ).first()
        if not p_trip:
            return
        if completed:
            p_trip.status = "completed"
        elif p_trip.status in ("active", "matched", "scheduled"):
            p_trip.status = "scheduled" if (p_trip.date and p_trip.date > today) else "active"

    # --- Пассажир отменяет участие ---
    if (new_status in ("passenger_cancelled", "cancelled")) and trip.user_id != current_user.id:
        req = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id == trip_id,
            models.TripRequest.requester_id == current_user.id,
            models.TripRequest.status == "accepted",
        ).first()
        if not req:
            raise HTTPException(status_code=403, detail="Вы не участник этой поездки")

        req.status = "cancelled"
        if trip.seats_taken and trip.seats_taken > 0:
            trip.seats_taken -= 1
        # Если поездка была "matched", но теперь есть свободные места — возвращаем в active / scheduled
        if trip.status == "matched" and (trip.seats_taken or 0) < (trip.seats or 3):
            trip.status = "scheduled" if (trip.date and trip.date > today) else "active"

        _update_passenger_trip(req.requester_trip_id, current_user.id, completed=False)
        db.commit()

        await driver_sse_manager.push(trip.user_id, _json.dumps({
            "event": "passenger_cancelled",
            "trip_id": trip_id,
            "passenger_name": current_user.name,
        }))
        return {"id": trip_id, "status": "passenger_cancelled"}

    # Пассажир завершает поездку со своей стороны (после отправки отзыва)
    if new_status == "completed" and trip.user_id != current_user.id:
        req = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id == trip_id,
            models.TripRequest.requester_id == current_user.id,
            models.TripRequest.status == "accepted",
        ).first()
        if not req:
            raise HTTPException(status_code=403, detail="Вы не участник этой поездки")

        req.status = "completed"
        _update_passenger_trip(req.requester_trip_id, current_user.id, completed=True)
        db.commit()
        return {"id": trip_id, "status": "passenger_completed"}

    # --- Водитель управляет статусом ---
    if trip.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the driver can change trip status")
    if new_status == "passenger_cancelled":
        raise HTTPException(status_code=400, detail="passenger_cancelled is only for passengers")

    trip.status = new_status
    notifications: list[tuple[int, str]] = []

    if new_status == "completed":
        accepted = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id == trip_id,
            models.TripRequest.status == "accepted",
        ).all()
        event_data = _json.dumps({"event": "trip_completed", "trip_id": trip_id})
        for r in accepted:
            r.status = "completed"
            _update_passenger_trip(r.requester_trip_id, r.requester_id, completed=True)
            if r.requester_id:
                notifications.append((r.requester_id, event_data))

    elif new_status == "cancelled":
        accepted = db.query(models.TripRequest).filter(
            models.TripRequest.trip_id == trip_id,
            models.TripRequest.status == "accepted",
        ).all()
        event_data = _json.dumps({
            "event": "trip_cancelled",
            "trip_id": trip_id,
            "driver_name": current_user.name,
        })
        for r in accepted:
            r.status = "cancelled"
            _update_passenger_trip(r.requester_trip_id, r.requester_id, completed=False)
            if r.requester_id:
                notifications.append((r.requester_id, event_data))
        trip.seats_taken = 0

    # Сначала фиксируем изменения в БД, затем уведомляем: обрыв SSE не должен
    # откатывать уже принятое пользователем решение.
    db.commit()

    for user_id, payload in notifications:
        await sse_manager.push(user_id, payload)

    return {"id": trip_id, "status": new_status}


@app.delete("/trips/{trip_id}")
async def cancel_trip(
    trip_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Отмена/удаление своей поездки с синхронизацией связанных заявок."""
    import json as _json

    trip = db.query(models.Trip).filter(
        models.Trip.id == trip_id,
        models.Trip.user_id == current_user.id,
    ).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found or access denied")

    today = get_bishkek_today_str()
    driver_notifications: list[tuple[int, str]] = []
    passenger_notifications: list[tuple[int, str]] = []

    # 1. Пользователь удаляет свою поисковую поездку как пассажир.
    #    Нужно освободить места в поездках водителей, которые уже приняли его.
    own_requests = db.query(models.TripRequest).filter(
        models.TripRequest.requester_trip_id == trip_id,
        models.TripRequest.requester_id == current_user.id,
        models.TripRequest.status == "accepted",
    ).all()
    for r in own_requests:
        driver_trip = db.query(models.Trip).filter(models.Trip.id == r.trip_id).first()
        if driver_trip:
            if driver_trip.seats_taken and driver_trip.seats_taken > 0:
                driver_trip.seats_taken -= 1
            if driver_trip.status == "matched" and (driver_trip.seats_taken or 0) < (driver_trip.seats or 3):
                driver_trip.status = "scheduled" if (driver_trip.date and driver_trip.date > today) else "active"
            if driver_trip.user_id:
                driver_notifications.append((
                    driver_trip.user_id,
                    _json.dumps({
                        "event": "passenger_cancelled",
                        "trip_id": driver_trip.id,
                        "passenger_name": current_user.name,
                    }),
                ))

    # 2. Пользователь удаляет свою поездку как водитель — уведомляем пассажиров.
    accepted = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.status == "accepted",
    ).all()
    for r in accepted:
        if r.requester_trip_id:
            p_trip = db.query(models.Trip).filter(
                models.Trip.id == r.requester_trip_id,
                models.Trip.user_id == r.requester_id,
            ).first()
            if p_trip and p_trip.status in ("active", "matched", "scheduled"):
                p_trip.status = "scheduled" if (p_trip.date and p_trip.date > today) else "active"
        if r.requester_id:
            passenger_notifications.append((
                r.requester_id,
                _json.dumps({
                    "event": "trip_cancelled",
                    "trip_id": trip_id,
                    "driver_name": current_user.name,
                }),
            ))

    # Безопасная очистка зависимых сущностей перед удалением
    db.query(models.ChatMessage).filter(models.ChatMessage.trip_id == trip_id).delete(synchronize_session=False)
    db.query(models.TripRequest).filter(
        (models.TripRequest.trip_id == trip_id) | (models.TripRequest.requester_trip_id == trip_id)
    ).delete(synchronize_session=False)
    db.delete(trip)
    db.commit()

    for user_id, payload in driver_notifications:
        await driver_sse_manager.push(user_id, payload)
    for user_id, payload in passenger_notifications:
        await sse_manager.push(user_id, payload)

    return {"id": trip_id, "deleted": True}


# Кулдаун тревожной кнопки: trip_id -> datetime
_last_panic_time: dict[int, datetime] = {}

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
    if not trip or not is_trip_participant(db, trip_id, current_user.id):
        raise HTTPException(status_code=403, detail="Только участник поездки может отправить сигнал тревоги")

    # Защита от флуда и исчерпания лимитов Telegram API (кулдаун 30 сек)
    now_utc = datetime.now(timezone.utc)
    last_sent = _last_panic_time.get(trip_id)
    if last_sent and (now_utc - last_sent).total_seconds() < 30:
        raise HTTPException(status_code=429, detail="Сигнал тревоги уже отправлен. Пожалуйста, подождите.")
    _last_panic_time[trip_id] = now_utc

    admin_chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not bot_token:
        return {"sent": False, "reason": "bot_token_missing"}

    # Если нет admin chat — fallback: отправляем самому пользователю
    if not admin_chat_id:
        admin_chat_id = auth.get_telegram_chat_id(current_user.phone, db)

    if not admin_chat_id:
        return {"sent": False, "reason": "no_chat_id"}

    safe_name = html.escape(current_user.name or "Пользователь")
    safe_phone = html.escape(current_user.phone or "")
    safe_origin = html.escape(trip.origin or "")
    safe_dest = html.escape(trip.destination or "")
    safe_time = html.escape(trip.time or "")

    msg_parts = [
        "🚨 <b>ТРЕВОГА — ПАССАЖИР В ОПАСНОСТИ!</b>",
        "",
        f"👤 <b>{safe_name}</b>",
        f"📱 <code>{safe_phone}</code>",
        f"🚗 Поездка #<b>{trip_id}</b>",
        f"📍 {safe_origin} → {safe_dest}",
        f"🕐 {safe_time}",
        "",
        "⚠️ Срочно свяжитесь с пассажиром!",
    ]
    message = "\n".join(msg_parts)

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": admin_chat_id,
                    "text": message,
                    "parse_mode": "HTML",
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

    user = db.query(models.User).filter(models.User.id == review.user_id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Проверка совместной поездки: отзыв доступен только после завершённой поездки.
    joint_query = db.query(models.TripRequest).filter(
        (
            (models.TripRequest.requester_id == current_user.id)
            & (models.TripRequest.driver_id == review.user_id)
        )
        | (
            (models.TripRequest.requester_id == review.user_id)
            & (models.TripRequest.driver_id == current_user.id)
        )
    )

    if review.trip_id:
        trip = db.query(models.Trip).filter(models.Trip.id == review.trip_id).first()
        if not trip:
            raise HTTPException(status_code=404, detail="Trip not found")
        if not is_trip_participant(db, review.trip_id, current_user.id) or not is_trip_participant(db, review.trip_id, review.user_id):
            raise HTTPException(
                status_code=403,
                detail="Отзыв можно оставить только после совместной поездки",
            )

        joint = joint_query.filter(
            models.TripRequest.trip_id == review.trip_id,
            models.TripRequest.status == "completed",
        ).first()
        # Совместимость со старыми записями: поездка завершена, но заявка
        # осталась в статусе accepted.
        if not joint and trip.status != "completed":
            raise HTTPException(
                status_code=403,
                detail="Отзыв можно оставить только после завершённой поездки",
            )
    else:
        joint = joint_query.filter(models.TripRequest.status == "completed").first()
        if not joint:
            raise HTTPException(
                status_code=403,
                detail="Отзыв можно оставить только после завершённой поездки",
            )

    # Защита от дублирующих отзывов. author_name используется только для
    # legacy-записей, где author_id ещё не был заполнен.
    query = db.query(models.Review).filter(
        models.Review.user_id == review.user_id,
        (models.Review.author_id == current_user.id)
        | (
            models.Review.author_id.is_(None)
            & (models.Review.author_name == current_user.name)
        ),
    )
    if review.trip_id:
        query = query.filter(models.Review.trip_id == review.trip_id)
    existing = query.first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You have already reviewed this user",
        )

    new_review = models.Review(
        user_id=review.user_id,
        author_id=current_user.id,
        author_name=current_user.name,
        trip_id=review.trip_id,
        text=review.text,
        rating=review.rating,
    )
    db.add(new_review)

    # Пересчитываем средний рейтинг (включая новый)
    all_reviews = db.query(models.Review).filter(models.Review.user_id == review.user_id).all()
    total_rating = sum((r.rating or 0) for r in all_reviews) + review.rating
    count = len(all_reviews) + 1
    user.trust_rating = int(total_rating * 10 / count) / 10.0

    db.commit()
    db.refresh(new_review)
    return new_review


# --- TRIP REQUESTS (запросы на поездку) ---

@app.post("/trip-requests/")
async def send_trip_request(
    req_in: schemas.TripRequestCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Пассажир отправляет запрос водителю на поездку.
    requester_id берётся из JWT (защита от spoofing).
    После сохранения в БД немедленно уведомляет водителя через SSE.
    """
    trip_id = req_in.trip_id                     # ID поездки водителя
    requester_trip_id = req_in.requester_trip_id # ID поездки пассажира
    requester_id = current_user.id               # ID пассажира — только из токена!

    driver_trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not driver_trip:
        raise HTTPException(status_code=404, detail="Поездка не найдена")
    if driver_trip.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Нельзя отправить запрос на собственную поездку")
    if driver_trip.role != "driver":
        raise HTTPException(status_code=400, detail="Можно отправить запрос только на поездку водителя")
    if driver_trip.status not in ("active", "scheduled"):
        raise HTTPException(status_code=400, detail="Эта поездка больше не принимает запросы")

    today = get_bishkek_today_str()
    if driver_trip.date and driver_trip.date < today:
        raise HTTPException(status_code=400, detail="Нельзя отправить запрос на прошедшую поездку")

    # requester_trip_id приходит от клиента — проверяем владельца, чтобы нельзя
    # было подставить чужую поездку и повлиять на её статус.
    if requester_trip_id:
        requester_trip = db.query(models.Trip).filter(
            models.Trip.id == requester_trip_id,
            models.Trip.user_id == current_user.id,
        ).first()
        if not requester_trip or requester_trip.role != "passenger":
            raise HTTPException(status_code=403, detail="Некорректная поездка пассажира")
        if requester_trip.status not in ("active", "scheduled"):
            raise HTTPException(status_code=400, detail="Ваша поездка уже не активна")

    if (driver_trip.seats_taken or 0) >= (driver_trip.seats or 3):
        raise HTTPException(status_code=400, detail="В автомобиле не осталось свободных мест")

    driver_id = driver_trip.user_id              # Берём строго из БД, а не из клиентского пейлоада

    # Проверяем, не отправлял ли уже активную заявку
    existing = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.requester_id == requester_id,
        models.TripRequest.status.in_(["pending", "accepted"]),
    ).first()
    if existing:
        return {"id": existing.id, "status": existing.status, "message": "Already sent"}

    req = models.TripRequest(
        trip_id=trip_id,
        requester_trip_id=requester_trip_id,
        requester_id=requester_id,
        driver_id=driver_id,
        status="pending",
        created_at=get_bishkek_now().strftime("%Y-%m-%d %H:%M:%S"),
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
        # Заявка на отменённую/завершённую или уже заполненную поездку
        # не должна показываться.
        if not driver_trip or driver_trip.status not in ("active", "scheduled"):
            continue
        if (driver_trip.seats_taken or 0) >= (driver_trip.seats or 3):
            continue
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
    status_update: schemas.TripRequestStatusUpdate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Водитель принимает (accepted) или отклоняет (declined) запрос.
    При accepted — мгновенно уведомляет пассажира через SSE.
    Только водитель (driver_id == current_user.id) может менять статус своего запроса.
    """
    import json as _json

    req = db.query(models.TripRequest).filter(models.TripRequest.id == request_id).first()
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
    if req.driver_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    if req.status != "pending":
        raise HTTPException(status_code=400, detail="Request has already been processed")

    new_status = status_update.status
    req.status = new_status

    driver = current_user  # уже загружен через Depends
    today = get_bishkek_today_str()
    declined_events: list[tuple[int, str]] = []

    # Если принят — увеличиваем счётчик мест явно с блокировкой строки от овербукинга
    if new_status == "accepted":
        trip = (
            db.query(models.Trip)
            .filter(models.Trip.id == req.trip_id)
            .with_for_update()
            .first()
        )
        if not trip:
            raise HTTPException(status_code=404, detail="Trip not found")
        if trip.role != "driver":
            raise HTTPException(status_code=400, detail="Принять запрос можно только на поездку водителя")
        if trip.status not in ("active", "scheduled"):
            raise HTTPException(status_code=400, detail="Эта поездка больше не принимает пассажиров")
        if trip.date and trip.date < today:
            raise HTTPException(status_code=400, detail="Нельзя принять запрос на прошедшую поездку")
        if (trip.seats_taken or 0) >= (trip.seats or 3):
            raise HTTPException(status_code=400, detail="Все места в автомобиле уже заняты")

        trip.seats_taken = (trip.seats_taken or 0) + 1
        is_full = trip.seats_taken >= (trip.seats or 3)
        # "matched" закрывает набор только для поездки на сегодня.
        # Будущая поездка остаётся "scheduled", иначе она исчезает из
        # запланированных и из напоминаний за час.
        if is_full and (not trip.date or trip.date <= today):
            trip.status = "matched"

        # Если мест больше нет — автоматически отклоняем остальные заявки,
        # чтобы они не висели у водителя и не блокировали пассажиров.
        if is_full:
            other_pending = db.query(models.TripRequest).filter(
                models.TripRequest.trip_id == trip.id,
                models.TripRequest.id != req.id,
                models.TripRequest.status == "pending",
            ).all()
            for other in other_pending:
                other.status = "declined"
                declined_events.append((
                    other.requester_id,
                    _json.dumps({
                        "id": other.id,
                        "status": "declined",
                        "trip_id": other.trip_id,
                        "requester_id": other.requester_id,
                        "driver_id": current_user.id,
                        "driver_name": current_user.name,
                    }),
                ))

        # Синхронизируем собственную поисковую поездку пассажира: после
        # принятия она больше не должна попадаться другим водителям.
        if req.requester_trip_id:
            p_trip = db.query(models.Trip).filter(
                models.Trip.id == req.requester_trip_id,
                models.Trip.user_id == req.requester_id,
            ).first()
            if p_trip and p_trip.status in ("active", "scheduled", "matched"):
                p_trip.status = "scheduled" if (p_trip.date and p_trip.date > today) else "matched"

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

    # Мгновенно уведомляем пассажиров через SSE (не ждём polling)
    for user_id, payload in declined_events:
        if user_id:
            await sse_manager.push(user_id, payload)
    if new_status in ("accepted", "declined") and req.requester_id:
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

    if not is_trip_participant(db, trip_id, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied to passenger list")

    accepted = db.query(models.TripRequest).filter(
        models.TripRequest.trip_id == trip_id,
        models.TripRequest.status == "accepted"
    ).all()

    requester_ids = {r.requester_id for r in accepted if r.requester_id}
    users_map: dict[int, models.User] = {}
    if requester_ids:
        users_map = {
            u.id: u for u in db.query(models.User).filter(models.User.id.in_(requester_ids)).all()
        }

    passengers = []
    for r in accepted:
        user = users_map.get(r.requester_id)
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


@app.get("/trips/{trip_id}/payment")
def get_trip_payment(
    trip_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Платёжные реквизиты водителя — только участникам конкретной поездки."""
    trip = db.query(models.Trip).filter(models.Trip.id == trip_id).first()
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")
    if not is_trip_participant(db, trip_id, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied to payment details")

    owner = db.query(models.User).filter(models.User.id == trip.user_id).first()
    if not owner:
        raise HTTPException(status_code=404, detail="Driver not found")

    return {
        "driver_id": owner.id,
        "driver_name": owner.name,
        "mbank_phone": owner.mbank_phone,
        "elkart_number": owner.elkart_number,
        "phone": owner.phone,
    }


@app.post("/trips/{trip_id}/messages", response_model=schemas.ChatMessage)
async def send_trip_message(
    trip_id: int,
    message: schemas.ChatMessageInput,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Отправка сообщения через REST с теми же правами, что и WebSocket."""
    import json as _json

    if not is_trip_participant(db, trip_id, current_user.id):
        raise HTTPException(status_code=403, detail="Access denied to this trip chat")

    text = message.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    timestamp = get_bishkek_now().strftime("%I:%M %p")
    new_msg = models.ChatMessage(
        trip_id=trip_id,
        sender_id=current_user.id,
        text=text,
        timestamp=timestamp,
    )
    db.add(new_msg)
    db.commit()
    db.refresh(new_msg)

    await manager.broadcast(_json.dumps({
        "id": new_msg.id,
        "text": text,
        "sender_id": current_user.id,
        "sender_name": current_user.name,
        "timestamp": timestamp,
        "trip_id": trip_id,
    }), str(trip_id))

    return new_msg


TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

# --- TELEGRAM BOT WEBHOOK ---
@app.post("/bot/webhook")
async def telegram_webhook(
    payload: dict,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
):
    """
    Telegram шлёт сюда все входящие сообщения.
    Защищён проверкой X-Telegram-Bot-Api-Secret-Token.
    Обрабатывает:
      - Передачу контакта через кнопку «Поделиться номером» (с проверкой ownership)
      - Команды /status, /check, /test, /help
    """
    import secrets
    if not TELEGRAM_WEBHOOK_SECRET:
        # Fail-closed: без секрета webhook нельзя открывать в production.
        if IS_PRODUCTION:
            raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    elif not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Forbidden: Invalid webhook secret")

    try:
        message = payload.get("message", {})
        if not message:
            return {"ok": True}

        chat_id: int = message.get("chat", {}).get("id")
        text: str = (message.get("text") or "").strip()
        contact: dict = message.get("contact", {})
        sender: dict = message.get("from", {})
        first_name: str = sender.get("first_name", "Пользователь")
        username: str = sender.get("username", "")

        if not chat_id:
            return {"ok": True}

        main_keyboard = {
            "keyboard": [
                [{"text": "📱 Поделиться номером", "request_contact": True}],
                [{"text": "🔍 Проверить статус"}, {"text": "🔔 Тест связи"}],
                [{"text": "❓ Помощь"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

        async def _reply(reply_text: str, custom_kb: dict | None = main_keyboard) -> None:
            await auth.send_telegram_message(chat_id, reply_text, reply_markup=custom_kb)

        async def _process_phone_binding(raw_phone: str) -> None:
            is_valid, p_e164, p_digits, p_formatted, op_or_err = auth.normalize_kg_phone(raw_phone)
            if not is_valid:
                await _reply(
                    f"❌ *Ошибка в номере телефона:*\n{op_or_err}\n\n"
                    f"💡 Пример: `0555 123 456` или `+996 555 123 456`."
                )
                return

            from models import TelegramBinding, User
            # 1. Проверяем наличие пользователя в БД Birge
            user = (
                db.query(User)
                .filter(
                    (User.phone == p_formatted)
                    | (User.phone == p_e164)
                    | (User.phone == p_digits)
                    | (User.phone.like(f"%{p_digits[-9:]}%"))
                )
                .first()
            )

            # 2. Сохраняем/обновляем привязку Telegram (поиск по телефону ИЛИ по chat_id)
            binding = db.query(TelegramBinding).filter(
                (TelegramBinding.phone_normalized == p_digits)
                | (TelegramBinding.phone == p_formatted)
                | (TelegramBinding.phone == p_e164)
                | (TelegramBinding.chat_id == chat_id)
            ).first()

            if binding:
                binding.chat_id = chat_id
                binding.phone = p_formatted
                binding.phone_normalized = p_digits
            else:
                binding = TelegramBinding(
                    phone=p_formatted,
                    phone_normalized=p_digits,
                    chat_id=chat_id,
                )
                db.add(binding)

            if user:
                user.is_verified = True

            try:
                db.commit()
            except Exception as e:
                logger.error(f"[Telegram] Ошибка сохранения привязки: {e}")
                db.rollback()
                try:
                    db.query(TelegramBinding).filter(
                        (TelegramBinding.chat_id == chat_id)
                        | (TelegramBinding.phone == p_formatted)
                        | (TelegramBinding.phone_normalized == p_digits)
                    ).delete(synchronize_session=False)
                    new_b = TelegramBinding(
                        phone=p_formatted,
                        phone_normalized=p_digits,
                        chat_id=chat_id,
                    )
                    db.add(new_b)
                    if user:
                        user.is_verified = True
                    db.commit()
                except Exception as e2:
                    logger.error(f"[Telegram] Критическая ошибка повторной привязки: {e2}")
                    db.rollback()

            if user:
                await _reply(
                    f"✅ *Номер подтверждён и найден в системе Birge!*\n\n"
                    f"👤 *Профиль:* {user.name}\n"
                    f"📱 *Телефон:* `{p_formatted}` ({op_or_err})\n"
                    f"⭐️ *Рейтинг доверия:* {user.trust_rating or 5.0} ★\n"
                    f"🛡 *Верификация:* Аккаунт успешно верифицирован (бейдж ✅ активен)\n\n"
                    f"🎉 *Теперь все сообщения гарантированно приходят сюда:*\n"
                    f"• Коды для быстрого входа\n"
                    f"• Запросы от попутчиков\n"
                    f"• Напоминания за 1 час до выезда\n"
                    f"• Экстренные уведомления (SOS)"
                )
            else:
                await _reply(
                    f"⚠️ *Номер {p_formatted} ({op_or_err}) распознан, но пока НЕ зарегистрирован в Birge.*\n\n"
                    f"Мы успешно привязали ваш Telegram-аккаунт к этому номеру.\n\n"
                    f"👉 Зарегистрируйтесь на сайте или в приложении Birge с этим номером, "
                    f"и ваш профиль автоматически станет верифицированным ✅, "
                    f"а все уведомления начнут мгновенно приходить сюда!"
                )

        # 1. Если отправлен контакт через кнопку «Поделиться номером»
        if contact:
            contact_user_id = contact.get("user_id")
            sender_id = sender.get("id")
            if contact_user_id != sender_id:
                await _reply("❌ Вы можете отправить только свой собственный контакт через кнопку «Поделиться номером».")
                return {"ok": True}

            contact_phone = contact.get("phone_number", "").strip()
            if contact_phone:
                await _process_phone_binding(contact_phone)
                return {"ok": True}

        # 2. Команда /start
        if text.startswith("/start"):
            await _reply(
                f"👋 Привет, *{first_name}*!\n\n"
                f"Я официальный бот сервиса совместных поездок *Birge* (Бишкек и Кыргызстан) 🚗\n\n"
                f"Я отправляю вам:\n"
                f"• 🔐 Коды входа в приложение\n"
                f"• 🚗 Мгновенные запросы от попутчиков\n"
                f"• ⏰ Напоминания за час до поездки\n\n"
                f"Чтобы безопасно привязать номер, нажмите кнопку ниже:\n"
                f"*«📱 Поделиться номером»*."
            )
            return {"ok": True}

        # 3. Команда /status или "Проверить статус"
        if text in ("/status", "🔍 Проверить статус", "проверить статус", "статус"):
            from models import TelegramBinding, User
            binding = db.query(TelegramBinding).filter(TelegramBinding.chat_id == chat_id).first()
            if binding:
                phone_tail = (binding.phone_normalized or "".join(filter(str.isdigit, binding.phone or "")))[-9:]
                query = db.query(User).filter(User.phone == binding.phone)
                if phone_tail:
                    query = db.query(User).filter((User.phone == binding.phone) | (User.phone.like(f"%{phone_tail}%")))
                user = query.first()
                if user:
                    await _reply(
                        f"📊 *Статус подключения Birge:*\n\n"
                        f"✅ Telegram привязан и активен!\n"
                        f"📱 Номер: `{binding.phone}`\n"
                        f"👤 Профиль в Birge: *{user.name}*\n"
                        f"⭐️ Рейтинг: *{user.trust_rating or 5.0} ★*\n"
                        f"🛡 Верификация: *Подтверждена ✅*\n"
                        f"🔔 Доставка уведомлений: *Работает штатно*"
                    )
                else:
                    await _reply(
                        f"📊 *Статус подключения:*\n\n"
                        f"⚠️ Telegram привязан к номеру `{binding.phone}`, но профиль в базе Birge ещё не создан.\n"
                        f"Создайте аккаунт в приложении, чтобы связать его."
                    )
            else:
                await _reply(
                    "❌ Ваш Telegram ещё не привязан к номеру телефона.\n\n"
                    "Нажмите кнопку *«📱 Поделиться номером»* ниже или напишите ваш номер телефона сообщением."
                )
            return {"ok": True}

        # 4. Команда /check <phone>
        if text.startswith("/check") or text.lower().startswith("проверить "):
            args = text.replace("/check", "").replace("проверить", "").strip()
            if not args:
                await _reply("💡 Укажите номер для проверки. Пример:\n`/check 0555123456`")
                return {"ok": True}
            is_val, pe164, pdig, pfmt, op_err = auth.normalize_kg_phone(args)
            if not is_val:
                await _reply(f"❌ Номер невалиден: {op_err}")
                return {"ok": True}

            from models import TelegramBinding, User
            sender_binding = db.query(TelegramBinding).filter(TelegramBinding.chat_id == chat_id).first()
            if not sender_binding or sender_binding.phone_normalized != pdig:
                await _reply(
                    "❌ Проверять можно только свой номер.\n\n"
                    "Привяжите Telegram через кнопку *«📱 Поделиться номером»*."
                )
                return {"ok": True}

            user = db.query(User).filter(User.phone.like(f"%{pdig[-9:]}%")).first()
            binding = db.query(TelegramBinding).filter(TelegramBinding.phone_normalized == pdig).first()

            await _reply(
                f"🔍 *Результат проверки номера {pfmt} ({op_err}):*\n\n"
                f"• Регистрация в Birge: {'✅ Да (Пользователь: ' + user.name + ')' if user else '❌ Нет'}\n"
                f"• Telegram-бот подключен: {'✅ Да' if binding else '❌ Нет (сообщения не доставляются)'}\n"
                f"• Статус верификации: {'✅ Верифицирован' if user and user.is_verified else '⚠️ Не верифицирован'}"
            )
            return {"ok": True}

        # 5. Команда /test или "Тест связи"
        if text in ("/test", "🔔 Тест связи", "тест", "проверка"):
            now_time = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            await _reply(
                f"🔔 *Тест доставки сообщений пройден!*\n\n"
                f"✅ Связь с ботом Birge активна.\n"
                f"🆔 Ваш Chat ID: `{chat_id}`\n"
                f"⏱ Время проверки: `{now_time}`\n\n"
                f"Уведомления о поездках и коды подтверждения будут доставляться без задержек."
            )
            return {"ok": True}

        # 6. Команда /help или "Помощь"
        if text in ("/help", "❓ Помощь", "помощь"):
            await _reply(
                "ℹ️ *Справка по боту Birge:*\n\n"
                "• Нажмите *«📱 Поделиться номером»* для безопасной привязки аккаунта\n"
                "• `/status` — проверить ваш статус привязки и верификации\n"
                "• `/check <номер>` — проверить наличие номера в системе Birge\n"
                "• `/test` — проверить доставку сообщений\n\n"
                "🌐 Веб-версия приложения: [birge-5c900.web.app](https://birge-5c900.web.app)\n"
                "🆘 Служба поддержки: @birge_support"
            )
            return {"ok": True}

        # 7. Любое другое сообщение — для безопасности привязка возможна только через Telegram-контакт
        await _reply(
            "🔒 Для защиты вашего аккаунта привязка номера телефона выполняется *только* через подтверждённый Telegram-контакт.\n\n"
            "👇 Нажмите кнопку *«📱 Поделиться номером»* внизу для безопасной привязки."
        )
        return {"ok": True}

    except Exception as e:
        logger.exception(f"[Telegram Webhook] Ошибка обработки обновления: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": True}


@app.get("/internal/get-binding")
def internal_get_binding(
    phone: str,
    x_telegram_bot_api_secret_token: Optional[str] = Header(None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
):
    """Внутренний защищенный эндпоинт для синхронизации привязок Telegram между локальным dev и production."""
    import secrets
    if not TELEGRAM_WEBHOOK_SECRET or not x_telegram_bot_api_secret_token or not secrets.compare_digest(
        x_telegram_bot_api_secret_token, TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Forbidden: Invalid secret")

    phone_digits = "".join(filter(str.isdigit, phone or ""))
    if not phone_digits:
        return {"found": False}

    binding = (
        db.query(models.TelegramBinding)
        .filter(
            (models.TelegramBinding.phone_normalized == phone_digits)
            | (models.TelegramBinding.phone == phone)
            | (models.TelegramBinding.phone_normalized.endswith(phone_digits[-9:]))
        )
        .first()
    )
    if not binding:
        return {"found": False}

    return {
        "found": True,
        "phone": binding.phone,
        "phone_normalized": binding.phone_normalized,
        "chat_id": binding.chat_id,
    }


# Глобальный in-memory set удалён — состояние теперь хранится в БД (Trip.reminder_sent).
# Это означает что напоминания не дублируются при рестарте сервера.


async def _reminder_loop():
    """Background loop: каждую минуту проверяет запланированные поездки.
    Если до начала поездки осталось 50-70 минут — отправляем Telegram-напоминание
    водителю И всем принятым пассажирам.
    Учитывает часовой пояс Бишкека (UTC+6) и корректный переход через полночь.
    """
    from models import TelegramBinding

    while True:
        await asyncio.sleep(60)  # проверяем раз в минуту
        try:
            now = get_bishkek_now()

            # Окно проверки: от 50 до 70 минут до начала поездки
            window_from = now + timedelta(minutes=50)
            window_to = now + timedelta(minutes=70)

            # Кандидатные даты (учитывает переход через полночь)
            candidate_dates = list({window_from.date().isoformat(), window_to.date().isoformat()})

            with SessionLocal() as db:
                trips = db.query(models.Trip).filter(
                    models.Trip.status.in_(["scheduled", "matched"]),
                    models.Trip.date.in_(candidate_dates),
                    models.Trip.reminder_sent == False,  # noqa: E712
                ).all()

                for trip in trips:
                    if not trip.date or not trip.time:
                        continue

                    # Парсим дату и время поездки в часовом поясе Бишкека
                    try:
                        t_year, t_month, t_day = map(int, trip.date.split("-"))
                        tm = parse_time_minutes(trip.time)
                        if tm is None:
                            continue
                        t_hour, t_min = divmod(tm, 60)
                        trip_dt = datetime(t_year, t_month, t_day, t_hour, t_min, tzinfo=BISHKEK_TZ)
                    except (ValueError, AttributeError):
                        continue

                    if not (window_from <= trip_dt <= window_to):
                        continue

                    # --- Напоминаем ВОДИТЕЛЮ ---
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


def _get_trip_datetime(trip: models.Trip) -> Optional[datetime]:
    """Возвращает datetime поездки в часовом поясе Бишкека (BISHKEK_TZ)."""
    if not trip.date:
        return None
    try:
        parts = [int(p) for p in str(trip.date).strip().split("-") if p.isdigit()]
        if len(parts) != 3:
            return None
        y, m, d = parts
        tm = parse_time_minutes(trip.time)
        h, mn = (tm // 60, tm % 60) if tm is not None else (23, 59)
        return datetime(y, m, d, h, mn, tzinfo=BISHKEK_TZ)
    except Exception:
        return None


def cleanup_stale_trips(db: Session) -> dict[str, int]:
    """Периодическая автоочистка и архивация старых поездок:
    1. Поездки водителей с пассажирами (время вышло > 4ч назад):
       - Переводятся в 'completed' (сохраняет историю поездок и отзывы).
       - Принятые пассажирские запросы переходят в 'completed'.
       - Привязанные поездки пассажиров переходят в 'completed'.
       - Висящие 'pending' запросы закрываются как 'declined'.
    2. Поездки водителей без пассажиров (время вышло > 5ч назад):
       - Переводятся в 'cancelled'.
       - Висящие 'pending' запросы закрываются как 'declined'.
    3. Неактуальные поиски пассажиров (время вышло > 4ч назад):
       - Переводятся в 'cancelled'.
       - Исходящие 'pending' запросы отклоняются.
    4. Физическая очистка древних cancelled записей (> 14 дней без сообщений и отзывов).
    """
    now_bishkek = get_bishkek_now()
    now_utc = datetime.now(timezone.utc)
    stats = {
        "completed_driver_trips": 0,
        "cancelled_driver_trips": 0,
        "cancelled_passenger_trips": 0,
        "declined_stale_requests": 0,
        "pruned_legacy_trips": 0,
    }

    try:
        candidate_trips = db.query(models.Trip).filter(
            models.Trip.status.in_(["active", "scheduled", "matched", "in_progress"])
        ).all()

        stale_driver_completed: list[models.Trip] = []
        stale_driver_cancelled: list[models.Trip] = []
        stale_passenger_cancelled: list[models.Trip] = []

        for trip in candidate_trips:
            buffer_hours = 12 if trip.status == "in_progress" else 4
            trip_dt = _get_trip_datetime(trip)

            is_stale = False
            if trip_dt:
                if now_bishkek - trip_dt >= timedelta(hours=buffer_hours):
                    is_stale = True
            elif trip.created_at:
                created_utc = trip.created_at if trip.created_at.tzinfo else trip.created_at.replace(tzinfo=timezone.utc)
                if now_utc - created_utc >= timedelta(hours=buffer_hours):
                    is_stale = True
            elif trip.date and trip.date < now_bishkek.date().isoformat():
                is_stale = True

            if not is_stale:
                continue

            if trip.role == "driver":
                has_passengers = bool(trip.seats_taken and trip.seats_taken > 0)
                if not has_passengers:
                    has_passengers = db.query(models.TripRequest).filter(
                        models.TripRequest.trip_id == trip.id,
                        models.TripRequest.status.in_(["accepted", "completed"]),
                    ).first() is not None

                if has_passengers:
                    stale_driver_completed.append(trip)
                else:
                    stale_driver_cancelled.append(trip)
            else:
                stale_passenger_cancelled.append(trip)

        # 1. Завершаем поездки водителей с пассажирами
        for d_trip in stale_driver_completed:
            d_trip.status = "completed"
            stats["completed_driver_trips"] += 1

            accepted_reqs = db.query(models.TripRequest).filter(
                models.TripRequest.trip_id == d_trip.id,
                models.TripRequest.status == "accepted",
            ).all()
            for req in accepted_reqs:
                req.status = "completed"
                if req.requester_trip_id:
                    p_trip = db.query(models.Trip).filter(models.Trip.id == req.requester_trip_id).first()
                    if p_trip and p_trip.status not in ("completed", "cancelled"):
                        p_trip.status = "completed"

            pending_reqs = db.query(models.TripRequest).filter(
                models.TripRequest.trip_id == d_trip.id,
                models.TripRequest.status == "pending",
            ).all()
            for p_req in pending_reqs:
                p_req.status = "declined"
                stats["declined_stale_requests"] += 1

        # 2. Отменяем несостоявшиеся поездки водителей
        for d_trip in stale_driver_cancelled:
            d_trip.status = "cancelled"
            stats["cancelled_driver_trips"] += 1

            pending_reqs = db.query(models.TripRequest).filter(
                models.TripRequest.trip_id == d_trip.id,
                models.TripRequest.status == "pending",
            ).all()
            for p_req in pending_reqs:
                p_req.status = "declined"
                stats["declined_stale_requests"] += 1

        # 3. Отменяем устаревшие поиски пассажиров
        for p_trip in stale_passenger_cancelled:
            was_accepted = db.query(models.TripRequest).filter(
                models.TripRequest.requester_trip_id == p_trip.id,
                models.TripRequest.status.in_(["accepted", "completed"]),
            ).first()
            if was_accepted:
                continue

            p_trip.status = "cancelled"
            stats["cancelled_passenger_trips"] += 1

            out_pending = db.query(models.TripRequest).filter(
                models.TripRequest.requester_trip_id == p_trip.id,
                models.TripRequest.status == "pending",
            ).all()
            for req in out_pending:
                req.status = "declined"
                stats["declined_stale_requests"] += 1

        # 4. Физическая очистка древних cancelled записей (> 14 дней без сообщений и отзывов)
        cutoff_date = (now_bishkek - timedelta(days=14)).date().isoformat()
        cutoff_utc = now_utc - timedelta(days=14)

        legacy_trips = db.query(models.Trip).filter(
            models.Trip.status == "cancelled",
            (
                (models.Trip.date.is_not(None) & (models.Trip.date < cutoff_date))
                | (models.Trip.created_at.is_not(None) & (models.Trip.created_at < cutoff_utc))
            ),
        ).limit(100).all()

        if legacy_trips:
            legacy_ids = [t.id for t in legacy_trips]
            chat_trip_ids = {
                m.trip_id for m in db.query(models.ChatMessage.trip_id).filter(
                    models.ChatMessage.trip_id.in_(legacy_ids)
                ).all()
            }
            review_trip_ids = {
                r.trip_id for r in db.query(models.Review.trip_id).filter(
                    models.Review.trip_id.in_(legacy_ids)
                ).all()
            }
            safe_to_delete = [
                tid for tid in legacy_ids
                if tid not in chat_trip_ids and tid not in review_trip_ids
            ]
            if safe_to_delete:
                db.query(models.TripRequest).filter(
                    models.TripRequest.trip_id.in_(safe_to_delete)
                    | models.TripRequest.requester_trip_id.in_(safe_to_delete)
                ).delete(synchronize_session=False)
                deleted_count = db.query(models.Trip).filter(
                    models.Trip.id.in_(safe_to_delete)
                ).delete(synchronize_session=False)
                stats["pruned_legacy_trips"] = deleted_count

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"[cleanup_stale_trips] Ошибка транзакции: {e}", exc_info=True)
        raise

    return stats


async def _stale_cleanup_loop():
    """Фоновая периодическая автоочистка старых поездок (каждые 15 минут)."""
    while True:
        try:
            await asyncio.sleep(900)  # 15 минут
            with database.SessionLocal() as db:
                stats = cleanup_stale_trips(db)
                total_actions = sum(stats.values())
                if total_actions > 0:
                    logger.info(f"[AutoCleanup] Периодическая очистка выполнена: {stats}")
                    print(f"[AutoCleanup] ✅ Выполнена автоочистка поездок: {stats}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[AutoCleanup] Ошибка в цикле автоочистки: {e}")
            print(f"[AutoCleanup] ⚠️ Ошибка: {e}")


@app.post("/admin/cleanup-trips")
def run_admin_cleanup(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Принудительный запуск очистки старых поездок администратором."""
    stats = cleanup_stale_trips(db)
    return {"ok": True, "stats": stats}


async def startup_tasks():
    """1. Автоочистка и архивация просроченных поездок.
    2. Запуск периодического цикла автоочистки каждые 15 минут.
    3. Запуск фоновой задачи напоминаний за 1 час до поездки.
    4. Регистрация Telegram webhook с secret_token.
    """
    # --- Автоочистка просроченных поездок при старте ---
    try:
        with database.SessionLocal() as db:
            startup_stats = cleanup_stale_trips(db)
            print(f"[Startup] ✅ Автоочистка поездок выполнена: {startup_stats}")
    except Exception as e:
        logger.error(f"[Startup] Ошибка при автоочистке: {e}")

    # --- Запуск периодического цикла автоочистки ---
    asyncio.create_task(_stale_cleanup_loop())
    print("[AutoCleanup] ✅ Фоновая задача автоочистки поездок запущена (проверка каждые 15 минут)")

    # --- Запуск фоновой задачи напоминаний ---
    asyncio.create_task(_reminder_loop())
    print("[Reminder] ✅ Фоновая задача напоминаний запущена (проверка каждые 60 сек)")

    # --- Telegram webhook ---
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://birge-backend.onrender.com")
    if not token or not render_url:
        print("[Telegram] TELEGRAM_BOT_TOKEN или RENDER_EXTERNAL_URL не заданы — webhook не зарегистрирован")
        return
    webhook_url = f"{render_url}/bot/webhook"
    webhook_payload = {"url": webhook_url}
    if TELEGRAM_WEBHOOK_SECRET:
        webhook_payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET

    async with __import__('httpx').AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json=webhook_payload
        )
        result = resp.json()
        if result.get("ok"):
            print(f"[Telegram] ✅ Webhook установлен: {webhook_url}")
        else:
            print(f"[Telegram] ❌ Ошибка webhook: {result}")


# debug/telegram-bindings endpoint removed — данные пользователей не должны быть публичными.
