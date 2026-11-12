from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional, Literal
from datetime import datetime

# --- Input Schemas ---
class SendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=4, max_length=32)

class VerifyCodeRequest(BaseModel):
    phone: str = Field(..., min_length=4, max_length=32)
    code: str = Field(..., min_length=4, max_length=8)

class TripStatusUpdate(BaseModel):
    status: Literal["active", "matched", "in_progress", "completed", "cancelled", "passenger_cancelled"]

class TripRequestCreate(BaseModel):
    trip_id: int = Field(..., gt=0)
    requester_trip_id: Optional[int] = Field(None, gt=0)
    driver_id: Optional[int] = Field(None, gt=0)

class TripRequestStatusUpdate(BaseModel):
    status: Literal["accepted", "declined"]

# --- User Schemas ---
class ReviewBase(BaseModel):
    text: str = Field(default="", max_length=1000)
    rating: int = Field(..., ge=1, le=5)

class ReviewCreate(ReviewBase):
    """Клиент передаёт target user_id, rating и text. Опционально trip_id."""
    user_id: int = Field(..., gt=0)
    trip_id: Optional[int] = Field(None, gt=0)

class Review(BaseModel):
    """Ответ API; без input-валидаторов для совместимости со старыми отзывами."""
    id: int
    user_id: int
    author_id: Optional[int] = None
    author_name: Optional[str] = ""
    trip_id: Optional[int] = None
    text: Optional[str] = ""
    rating: Optional[int] = 5

    model_config = ConfigDict(from_attributes=True)

class UserBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    phone: str = Field(..., min_length=4, max_length=32)
    photo: Optional[str] = Field("https://i.pravatar.cc/150", max_length=1000)
    car_model: Optional[str] = Field(None, max_length=80)
    car_color: Optional[str] = Field(None, max_length=40)
    car_plate: Optional[str] = Field(None, max_length=20)
    mbank_phone: Optional[str] = Field(None, max_length=32)
    elkart_number: Optional[str] = Field(None, max_length=32)

class UserPublic(BaseModel):
    """Публичный профиль без телефона и платёжных реквизитов."""
    id: int
    name: Optional[str] = ""
    photo: Optional[str] = "https://i.pravatar.cc/150"
    trust_rating: float = 5.0
    is_verified: bool = False
    car_model: Optional[str] = None
    car_color: Optional[str] = None
    car_plate: Optional[str] = None
    registered_since: Optional[str] = "2024"

    model_config = ConfigDict(from_attributes=True)

class UserUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    photo: Optional[str] = Field(None, max_length=1000)
    car_model: Optional[str] = Field(None, max_length=80)
    car_color: Optional[str] = Field(None, max_length=40)
    car_plate: Optional[str] = Field(None, max_length=20)
    mbank_phone: Optional[str] = Field(None, max_length=32)
    elkart_number: Optional[str] = Field(None, max_length=32)

class UserCreate(UserBase):
    pass

class User(BaseModel):
    """Профиль владельца; допускает NULL в legacy-записях."""
    id: int
    name: Optional[str] = ""
    phone: Optional[str] = ""
    photo: Optional[str] = "https://i.pravatar.cc/150"
    trust_rating: float = 5.0
    is_verified: bool = False
    trips_today: int = 0
    registered_since: Optional[str] = "2024"
    car_model: Optional[str] = None
    car_color: Optional[str] = None
    car_plate: Optional[str] = None
    mbank_phone: Optional[str] = None
    elkart_number: Optional[str] = None
    reviews: List[Review] = []

    model_config = ConfigDict(from_attributes=True)

# --- Trip Schemas ---
class TripBase(BaseModel):
    role: Literal["driver", "passenger"]
    origin: str = Field(..., min_length=2, max_length=120)
    destination: str = Field(..., min_length=2, max_length=120)
    time: str = Field(..., pattern=r"^\d{1,2}:\d{2}$")
    date: Optional[str] = None          # 'YYYY-MM-DD', None = без привязки к дате
    seats: int = Field(3, ge=1, le=8)   # макс мест (для водителя)
    price_per_seat: int = Field(0, ge=0, le=100000)  # стоимость места в сомах (0 = бесплатно)

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("date must be in YYYY-MM-DD format") from exc
        return parsed.isoformat()

    @field_validator("time")
    @classmethod
    def _normalize_time(cls, value: str) -> str:
        hours, minutes = (int(part) for part in value.split(":"))
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            raise ValueError("time must be a valid 24-hour time")
        return f"{hours:02d}:{minutes:02d}"

class TripCreate(TripBase):
    origin_lat: Optional[float] = Field(None, ge=-90, le=90)
    origin_lng: Optional[float] = Field(None, ge=-180, le=180)
    dest_lat: Optional[float] = Field(None, ge=-90, le=90)
    dest_lng: Optional[float] = Field(None, ge=-180, le=180)

class Trip(BaseModel):
    """Ответ API. Без input-валидаторов: в БД могут быть legacy-форматы времени."""
    id: int
    user_id: int
    role: str
    origin: str
    destination: str
    time: str
    date: Optional[str] = None
    seats: int = 3
    seats_taken: int = 0
    price_per_seat: int = 0
    status: str
    user: Optional[UserPublic] = None   # Защита от утечки телефонов и платёжных данных в поиске!

    model_config = ConfigDict(from_attributes=True)

# --- Chat Schemas ---
class ChatMessageInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)

class ChatMessageBase(BaseModel):
    trip_id: int = Field(..., gt=0)
    text: str = Field(..., min_length=1, max_length=1000)
    timestamp: str = Field(..., max_length=40)

class ChatMessageCreate(ChatMessageBase):
    pass

class ChatMessage(ChatMessageBase):
    id: int
    sender_id: int
    sender: Optional[UserPublic] = None

    model_config = ConfigDict(from_attributes=True)
