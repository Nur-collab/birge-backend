from pydantic import BaseModel
from typing import List, Optional

# --- User Schemas ---
class ReviewBase(BaseModel):
    text: str
    rating: int

class ReviewCreate(ReviewBase):
    """Клиент передаёт только target user_id, rating и text.
    author_name всегда берётся из JWT-токена на бэкенде.
    """
    user_id: int

class Review(ReviewBase):
    id: int
    user_id: int
    author_name: str

    class Config:
        from_attributes = True

class UserBase(BaseModel):
    name: str
    phone: str
    photo: Optional[str] = "https://i.pravatar.cc/150"
    car_model: Optional[str] = None
    car_color: Optional[str] = None
    car_plate: Optional[str] = None

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

    class Config:
        from_attributes = True

# --- Trip Schemas ---
class TripBase(BaseModel):
    role: str
    origin: str
    destination: str
    time: str
    date: Optional[str] = None      # 'YYYY-MM-DD', None = без привязки к дате
    seats: int = 3                  # макс мест (для водителя)
    price_per_seat: Optional[int] = 0  # стоимость места в сомах (0 = договориться)

class TripCreate(TripBase):
    pass

class Trip(TripBase):
    id: int
    user_id: int
    status: str
    seats_taken: int = 0
    user: Optional[User] = None

    class Config:
        from_attributes = True

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
    sender: Optional[User] = None

    class Config:
        from_attributes = True
