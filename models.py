from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, Float, DateTime, BigInteger
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime, timezone

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    phone = Column(String, unique=True, index=True)
    photo = Column(String, default="https://i.pravatar.cc/150")
    trust_rating = Column(Float, default=5.0)
    is_verified = Column(Boolean, default=False)
    trips_today = Column(Integer, default=0)
    registered_since = Column(String, default="2024")
    last_trip_date = Column(String) # For daily reset: 'YYYY-MM-DD'

    car_model = Column(String)
    car_color = Column(String)
    car_plate = Column(String)

    trips = relationship("Trip", back_populates="user")
    reviews = relationship("Review", foreign_keys="[Review.user_id]", back_populates="user")

class Trip(Base):
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String) # 'driver' or 'passenger'
    origin = Column(String)
    destination = Column(String)
    time = Column(String)
    status = Column(String, default="active") # active, matched, completed
    date = Column(String)                      # 'YYYY-MM-DD' дата поездки (опц.)
    seats = Column(Integer, default=3)         # макс мест (для водителя)
    seats_taken = Column(Integer, default=0)   # занято мест
    price_per_seat = Column(Integer, default=0) # стоимость одного места в сомах (0 = бесплатно)
    reminder_sent = Column(Boolean, default=False) # True = напоминание уже отправлено (не повторяем при рестарте)

    user = relationship("User", back_populates="trips")

class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    author_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    author_name = Column(String)
    trip_id = Column(Integer, ForeignKey("trips.id"), nullable=True)
    text = Column(String)
    rating = Column(Integer)

    user = relationship("User", foreign_keys=[user_id], back_populates="reviews")
    author = relationship("User", foreign_keys=[author_id])
    trip = relationship("Trip", foreign_keys=[trip_id])

class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    trip_id = Column(Integer, ForeignKey("trips.id"))
    sender_id = Column(Integer, ForeignKey("users.id"))
    text = Column(String)
    timestamp = Column(String)

    trip = relationship("Trip", backref="messages")
    sender = relationship("User")

# Запросы на поездку (пассажир → водитель)
class TripRequest(Base):
    __tablename__ = "trip_requests"

    id = Column(Integer, primary_key=True, index=True)
    trip_id = Column(Integer, ForeignKey("trips.id"))            # поездка водителя
    requester_trip_id = Column(Integer, ForeignKey("trips.id"))  # поездка пассажира
    requester_id = Column(Integer, ForeignKey("users.id"))       # кто запрашивает
    driver_id = Column(Integer, ForeignKey("users.id"))          # кому адресован запрос
    status = Column(String, default="pending")                   # pending, accepted, declined
    created_at = Column(String)

    requester = relationship("User", foreign_keys=[requester_id])
    driver = relationship("User", foreign_keys=[driver_id])


# SMS-коды: хранятся в БД (не теряются при рестарте)
class SmsCode(Base):
    __tablename__ = "sms_codes"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, index=True)
    code = Column(String)
    expires_at = Column(DateTime(timezone=True))       # Когда код истекает (5 минут)
    last_sent_at = Column(DateTime(timezone=True))     # Когда последний раз отправляли (rate limit)
    is_used = Column(Boolean, default=False)
    attempts = Column(Integer, default=0)             # Счётчик неверных попыток (защита от brute-force)


# Telegram: привязка номера телефона к chat_id
class TelegramBinding(Base):
    __tablename__ = "telegram_bindings"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, unique=True, index=True)          # "+996 555 123 456" (читаемый формат)
    phone_normalized = Column(String, index=True)             # "996555123456" (только цифры, для быстрого поиска)
    chat_id = Column(BigInteger, index=True)                  # Telegram chat_id (BigInteger для 64-бит ID в PostgreSQL)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
