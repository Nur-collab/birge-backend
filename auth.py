import os
import secrets
import httpx
from datetime import datetime, timedelta, timezone
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
        "[Auth] [!] Переменная окружения SECRET_KEY не задана. "
        "JWT-токены будут сбрасываться при каждом рестарте сервера. "
        "Задайте SECRET_KEY в настройках Render."
    )

SECRET_KEY: str = _env_secret
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней

SMS_CODE_TTL_MINUTES = 5        # Код действителен 5 минут
SMS_RATE_LIMIT_SECONDS = 60     # Повторная отправка — не чаще 1 раза в 60 сек

# Telegram Bot
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "8730455344:AAHQ3rj5r9kaGVTdDZH0T0CtMpwHyegNqMo"
)
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


KG_OPERATOR_PREFIXES = {
    "55": "Mega", "99": "Mega",
    "77": "Beeline", "22": "Beeline",
    "70": "О!", "50": "О!",
    "88": "Salam",
}


def normalize_kg_phone(raw_phone: str) -> tuple[bool, str, str, str, str]:
    """
    Нормализует и валидирует кыргызский мобильный номер.
    Возвращает: (is_valid, phone_e164, phone_digits, phone_formatted, operator_name_or_error)
    """
    digits = "".join(filter(str.isdigit, raw_phone or ""))
    if not digits:
        return False, "", "", "", "Номер не указан."

    if len(digits) == 10 and digits.startswith("0"):
        digits = "996" + digits[1:]
    elif len(digits) == 9:
        digits = "996" + digits

    if len(digits) != 12 or not digits.startswith("996"):
        return False, "", "", "", "Номер должен содержать 9 цифр после кода страны (+996)."

    operator_code = digits[3:5]
    operator_name = KG_OPERATOR_PREFIXES.get(operator_code)
    if not operator_name:
        return (
            False, "", "", "",
            f"Неизвестный код оператора '{operator_code}'. Поддерживаются: Mega (55x, 99x), Beeline (77x, 22x), О! (70x, 50x), Salam (88x)."
        )

    phone_e164 = f"+{digits}"
    phone_formatted = f"+996 {digits[3:6]} {digits[6:9]} {digits[9:12]}"
    return True, phone_e164, digits, phone_formatted, operator_name


def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
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
    Проверяет точное совпадение по phone_normalized или phone.
    """
    from models import TelegramBinding

    phone_digits = "".join(filter(str.isdigit, phone or ""))
    if not phone_digits:
        return None

    # Поиск по индексированному полю phone_normalized
    binding = (
        db.query(TelegramBinding)
        .filter(
            (TelegramBinding.phone_normalized == phone_digits)
            | (TelegramBinding.phone == phone)
            | (TelegramBinding.phone_normalized.endswith(phone_digits[-9:]))
        )
        .first()
    )
    if binding:
        return binding.chat_id

    # Fallback для старых записей
    old_bindings = (
        db.query(TelegramBinding)
        .filter(TelegramBinding.phone_normalized == None)  # noqa: E711
        .all()
    )
    for b in old_bindings:
        b_digits = "".join(filter(str.isdigit, b.phone))
        b.phone_normalized = b_digits
        if b_digits == phone_digits or b_digits.endswith(phone_digits[-9:]):
            db.commit()
            return b.chat_id

    if old_bindings:
        db.commit()

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


async def send_telegram_message(
    chat_id: int | str,
    text: str,
    reply_markup: Optional[dict] = None
) -> bool:
    """Отправляет сообщение через Telegram Bot с поддержкой Markdown и клавиатуры."""
    token = TELEGRAM_BOT_TOKEN or "8730455344:AAHQ3rj5r9kaGVTdDZH0T0CtMpwHyegNqMo"
    api_url = f"https://api.telegram.org/bot{token}"
    try:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{api_url}/sendMessage", json=payload)
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

    now = datetime.now(timezone.utc)
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

    now = datetime.now(timezone.utc)
    entry = db.query(SmsCode).filter(SmsCode.phone == phone).first()

    if not entry:
        return False
    if entry.is_used:
        return False
    # Нормализуем expires_at: если naive — считаем UTC
    exp = entry.expires_at
    if exp is not None and exp.tzinfo is None:
        from datetime import timezone as _tz
        exp = exp.replace(tzinfo=_tz.utc)
    if exp is None or exp < now:
        return False  # Код устарел
    if entry.code != code:
        return False

    # Помечаем как использованный
    entry.is_used = True
    db.commit()
    return True

