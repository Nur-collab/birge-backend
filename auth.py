from datetime import datetime, timedelta
from jose import JWTError, jwt

# В продакшене — храните секрет в переменной окружения!
SECRET_KEY = "birge-super-secret-key-bishkek-2024"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

SMS_CODE_TTL_MINUTES = 5        # Код действителен 5 минут
SMS_RATE_LIMIT_SECONDS = 60     # Повторная отправка — не чаще 1 раза в 60 сек


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


# ---------- SMS через БД ----------

def save_sms_code(phone: str, code: str, db):
    """
    Сохраняет или обновляет SMS-код в БД.
    Возвращает (True, None) при успехе или (False, error_message) при rate limit.
    """
    from models import SmsCode

    now = datetime.utcnow()
    existing = db.query(SmsCode).filter(SmsCode.phone == phone).first()

    if existing:
        # Rate limiting: не чаще одного раза в SMS_RATE_LIMIT_SECONDS
        if existing.last_sent_at:
            seconds_since = (now - existing.last_sent_at).total_seconds()
            if seconds_since < SMS_RATE_LIMIT_SECONDS:
                wait = int(SMS_RATE_LIMIT_SECONDS - seconds_since)
                return False, f"Подождите {wait} сек. перед повторной отправкой"

        # Обновляем существующую запись
        existing.code = code
        existing.expires_at = now + timedelta(minutes=SMS_CODE_TTL_MINUTES)
        existing.last_sent_at = now
        existing.is_used = False
    else:
        # Первый раз — создаём новую запись
        new_entry = SmsCode(
            phone=phone,
            code=code,
            expires_at=now + timedelta(minutes=SMS_CODE_TTL_MINUTES),
            last_sent_at=now,
            is_used=False,
        )
        db.add(new_entry)

    db.commit()
    return True, None


def verify_sms_code(phone: str, code: str, db) -> bool:
    """
    Проверяет SMS-код из БД.
    После успешной проверки помечает код как использованный.
    """
    from models import SmsCode

    now = datetime.utcnow()
    entry = db.query(SmsCode).filter(SmsCode.phone == phone).first()

    if not entry:
        return False
    if entry.is_used:
        return False
    if entry.expires_at < now:
        return False  # Код устарел
    if entry.code != code:
        return False

    # Помечаем как использованный
    entry.is_used = True
    db.commit()
    return True
