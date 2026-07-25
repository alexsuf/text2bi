import os
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from db import Base, hash_password
from models import User

CONFIG_DB_USER = os.environ.get("CONFIG_DB_USER", "postgres")
CONFIG_DB_PASSWORD = os.environ.get("CONFIG_DB_PASSWORD", "secret")
CONFIG_DB_HOST = os.environ.get("CONFIG_DB_HOST", "dash_db")
CONFIG_DB_PORT = os.environ.get("CONFIG_DB_PORT", "5432")
CONFIG_DB_NAME = os.environ.get("CONFIG_DB", "dash_config")

DATABASE_URL = f"postgresql://{CONFIG_DB_USER}:{CONFIG_DB_PASSWORD}@{CONFIG_DB_HOST}:{CONFIG_DB_PORT}/{CONFIG_DB_NAME}"

engine = create_engine(DATABASE_URL)
Session = scoped_session(sessionmaker(bind=engine))


def init_config_db():
    try:
        Base.metadata.create_all(bind=engine)
        session = Session()
        alex_user = session.query(User).filter_by(username='alex').first()
        if not alex_user:
            alex = User(username='alex')
            alex.password_hash = hash_password('secret')
            session.add(alex)
        max_user = session.query(User).filter_by(username='max').first()
        if not max_user:
            max = User(username='max')
            max.password_hash = hash_password('secret')
            session.add(max)
        session.commit()
        session.close()
        return True
    except Exception as e:
        print(f"Database init error: {e}")
        return False


def get_config_session():
    return Session()


def hash_password(password: str) -> str:
    import hashlib
    return hashlib.sha256(password.encode()).hexdigest()
