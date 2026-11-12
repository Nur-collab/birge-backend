import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import declarative_base

# Используем PostgreSQL на Render, SQLite локально
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./birge.db")

# Render отдаёт postgres:// или postgresql://.
# В SQLAlchemy 2.1+ дефолтный драйвер postgresql:// изменился на psycopg v3.
# Явно задаём драйвер +psycopg2 для установленного пакета psycopg2-binary:
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgresql+"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

# connect_args нужен только для SQLite
is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}

engine_kwargs = {
    "connect_args": connect_args,
    "pool_pre_ping": True,
}

if not is_sqlite:
    # Оптимизация пула соединений для PostgreSQL (на Render)
    engine_kwargs.update({
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 300,
        "pool_timeout": 30,
    })

engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
