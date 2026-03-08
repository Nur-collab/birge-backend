from pydantic import BaseModel
from typing import List, Optional

# --- User Schemas ---
class ReviewBase(BaseModel):
    author_name: str
    text: str
    rating: int

class ReviewCreate(ReviewBase):
    user_id: int

class Review(ReviewBase):
    id: int
    user_id: int

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
    seats: int = 3  # макс мест (для водителя)

class TripCreate(TripBase):
    user_id: int

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
    sender_id: int
    text: str
    timestamp: str

class ChatMessageCreate(ChatMessageBase):
    pass

class ChatMessage(ChatMessageBase):
    id: int
    sender: Optional[User] = None

    class Config:
        from_attributes = True
