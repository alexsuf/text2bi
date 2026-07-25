import hashlib
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.schema import CreateSchema
from contextlib import contextmanager

CONFIG_DB_USER = os.environ.get("CONFIG_DB_USER", "postgres")
CONFIG_DB_PASSWORD = os.environ.get("CONFIG_DB_PASSWORD", "secret")
CONFIG_DB_HOST = os.environ.get("CONFIG_DB_HOST", "dash_db")
CONFIG_DB_PORT = os.environ.get("CONFIG_DB_PORT", "5432")
CONFIG_DB_NAME = os.environ.get("CONFIG_DB", "dash_config")

DATABASE_URL = f"postgresql://{CONFIG_DB_USER}:{CONFIG_DB_PASSWORD}@{CONFIG_DB_HOST}:{CONFIG_DB_PORT}/{CONFIG_DB_NAME}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


class Base(DeclarativeBase):
    pass


def init_db():
    try:
        with engine.connect() as conn:
            conn.execute(CreateSchema("app", if_not_exists=True))
            conn.commit()
    except Exception as e:
        print(f"[init_db] schema creation: {e}")

    # Импортируем все модели перед create_all
    from models import User, ConnectionSetting, SystemConfig, Prompt, SavedQuery, QueryHistory, ReportPromptCase, LLMProvider, LLMModel, LLMFallback

    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"[init_db] create_all: {e}")

    db = SessionLocal()
    try:
        for username in ["alex", "max"]:
            u = db.query(User).filter(User.username == username).first()
            if not u:
                db.add(User(username=username, password_hash=hash_password("secret")))
        db.commit()
    except Exception as e:
        print(f"[init_db] user seed: {e}")
    finally:
        db.close()


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


init_db()
