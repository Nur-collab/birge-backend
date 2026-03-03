from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.context import CryptContext

# В продакшене — храните секрет в переменной окружения!
SECRET_KEY = "birge-super-secret-key-bishkek-2024"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Временное хранилище SMS-кодов (в продакшене — Redis)
sms_codes: dict = {}

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str) -> int | None:
    """Проверяет JWT токен и возвращает user_id."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str = payload.get("sub")
        return int(user_id_str) if user_id_str else None
    except (JWTError, ValueError):
        return None

def save_sms_code(phone: str, code: str):
    """Сохраняет SMS-код для номера телефона."""
    sms_codes[phone] = code

def verify_sms_code(phone: str, code: str) -> bool:
    """Проверяет правильность SMS-кода."""
    stored = sms_codes.get(phone)
    if stored and stored == code:
        del sms_codes[phone]  # Использован — удаляем
        return True
    return False
