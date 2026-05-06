import os
import secrets
import httpx
from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt

# Берём секрет из переменной окружения.
# На Render: Settings → Environment → Add SECRET_KEY
_env_secret = os.environ.get("SECRET_KEY", "")
if not _env_secret:
    # Генерируем случайный ключ как временный fallback (сессии не переживут рестарт).
    # Это нормально для разработки, но недопустимо в продакшене.
    _env_secret = secrets.token_hex(32)
    print(
        "[Auth] ⚠️  Переменная окружения SECRET_KEY не задана. "
        "JWT-токены будут сбрасываться при каждом рестарте сервера. "
        "Задайте SECRET_KEY в настройках Render."
    )

SECRET_KEY: str = _env_secret
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

SMS_CODE_TTL_MINUTES = 5        # Код действителен 5 минут
SMS_RATE_LIMIT_SECONDS = 60     # Повторная отправка — не чаще 1 раза в 60 сек

# Telegram Bot
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[int]:
    """Проверяет JWT токен и возвращает user_id."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id_str = payload.get("sub")
        return int(user_id_str) if user_id_str else None
    except (JWTError, ValueError):
        return None


# ---------- Telegram: найти chat_id по номеру телефона ----------

def get_telegram_chat_id(phone: str, db) -> Optional[int]:
    """Ищет Telegram chat_id по нормализованному номеру телефона.

    Использует индексированное поле phone_normalized (только цифры)
    для O(log N) поиска вместо полного скана таблицы.

    Для старых записей без phone_normalized выполняет self-healing миграцию:
    находит по старому методу и сразу заполняет поле.
    """
    from models import TelegramBinding

    phone_digits = "".join(filter(str.isdigit, phone))

    # Быстрый путь: поиск по индексированному полю (основной сценарий)
    binding = (
        db.query(TelegramBinding)
        .filter(TelegramBinding.phone_normalized == phone_digits)
        .first()
    )
    if binding:
        return binding.chat_id

    # Fallback: ищем старые записи, у которых phone_normalized ещё не заполнен
    # (записи созданные до миграции). Исправляем их на лету.
    old_bindings = (
        db.query(TelegramBinding)
        .filter(TelegramBinding.phone_normalized == None)  # noqa: E711
        .all()
    )
    for b in old_bindings:
        b_digits = "".join(filter(str.isdigit, b.phone))
        # Всегда заполняем поле для найденной записи (self-healing)
        b.phone_normalized = b_digits
        if b_digits == phone_digits:
            db.commit()
            return b.chat_id

    if old_bindings:
        db.commit()  # сохраняем все нормализованные поля разом

    return None


async def send_telegram_code(chat_id: int, code: str) -> bool:
    """Отправляет код верификации через Telegram Bot."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    message = (
        f"🔐 *Ваш код для входа в Birge:*\n\n"
        f"➡️ `{code}`\n\n"
        f"_Код действителен 5 минут. Не передавайте его никому._"
    )
    return await send_telegram_message(chat_id, message)


async def send_telegram_message(chat_id: int, text: str) -> bool:
    """Отправляет любое сообщение через Telegram Bot."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{TELEGRAM_API_URL}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
            return resp.status_code == 200 and resp.json().get("ok", False)
    except Exception as e:
        print(f"[Telegram] Ошибка отправки: {e}")
        return False


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

