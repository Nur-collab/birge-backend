from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Literal

# --- Input Schemas ---
class SendCodeRequest(BaseModel):
    phone: str

class VerifyCodeRequest(BaseModel):
    phone: str
    code: str

class TripStatusUpdate(BaseModel):
    status: Literal["active", "matched", "in_progress", "completed", "cancelled", "passenger_cancelled"]

class TripRequestCreate(BaseModel):
    trip_id: int
    requester_trip_id: Optional[int] = None
    driver_id: Optional[int] = None

class TripRequestStatusUpdate(BaseModel):
    status: Literal["accepted", "declined"]

# --- User Schemas ---
class ReviewBase(BaseModel):
    text: str
    rating: int = Field(..., ge=1, le=5)

class ReviewCreate(ReviewBase):
    """Клиент передаёт target user_id, rating и text. Опционально trip_id."""
    user_id: int
    trip_id: Optional[int] = None

class Review(ReviewBase):
    id: int
    user_id: int
    author_id: Optional[int] = None
    author_name: str
    trip_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)

class UserBase(BaseModel):
    name: str
    phone: str
    photo: Optional[str] = "https://i.pravatar.cc/150"
    car_model: Optional[str] = None
    car_color: Optional[str] = None
    car_plate: Optional[str] = None

class UserPublic(BaseModel):
    """Публичный профиль без номера телефона (защита от утечки в поиске и чате)."""
    id: int
    name: str
    photo: Optional[str] = "https://i.pravatar.cc/150"
    trust_rating: float = 5.0
    is_verified: bool = False
    car_model: Optional[str] = None
    car_color: Optional[str] = None
    car_plate: Optional[str] = None
    registered_since: Optional[str] = "2024"

    model_config = ConfigDict(from_attributes=True)

class UserUpdate(BaseModel):
    name: Optional[str] = None
    photo: Optional[str] = None
    car_model: Optional[str] = None
    car_color: Optional[str] = None
    car_plate: Optional[str] = None

class UserCreate(UserBase):
    pass

class User(UserBase):
    id: int
    trust_rating: float
    is_verified: bool
    trips_today: int
    registered_since: str
    reviews: List[Review] = []

    model_config = ConfigDict(from_attributes=True)

# --- Trip Schemas ---
class TripBase(BaseModel):
    role: Literal["driver", "passenger"]
    origin: str
    destination: str
    time: str
    date: Optional[str] = None          # 'YYYY-MM-DD', None = без привязки к дате
    seats: int = 3                      # макс мест (для водителя)
    price_per_seat: Optional[int] = 0   # стоимость места в сомах (0 = бесплатно)

class TripCreate(TripBase):
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    dest_lat: Optional[float] = None
    dest_lng: Optional[float] = None

class Trip(TripBase):
    id: int
    user_id: int
    status: str
    seats_taken: int = 0
    user: Optional[UserPublic] = None   # Защита от утечки номеров телефонов в поиске!

    model_config = ConfigDict(from_attributes=True)

# --- Chat Schemas ---
class ChatMessageBase(BaseModel):
    trip_id: int
    text: str
    timestamp: str

class ChatMessageCreate(ChatMessageBase):
    pass

class ChatMessage(ChatMessageBase):
    id: int
    sender_id: int
    sender: Optional[UserPublic] = None

    model_config = ConfigDict(from_attributes=True)
