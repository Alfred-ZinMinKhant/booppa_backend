from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=True,
    pool_recycle=settings.DB_POOL_RECYCLE,
    echo=settings.LOG_LEVEL == "DEBUG",
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


import contextlib

@contextlib.contextmanager
def transactional_session():
    """
    Context manager that yields a database session and safely handles transactions.
    Commits automatically on success, rolls back on exception, and always closes the session.
    Prevents connection pool exhaustion from unclosed sessions or leaked locks.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_tables():
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Error creating tables: {e}")
        raise


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """FastAPI dependency — returns authenticated User or raises 401.

    Accepts either:
      - JWT access token (issued by /auth/login), or
      - Bearer API key prefixed with `bp_` (issued via /vendor/api-keys)
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # ── API key path ──────────────────────────────────────────────────────────
    if token.startswith("bp_"):
        import hashlib
        from datetime import datetime as _dt
        from app.core.models import User
        from app.core.models import ApiKey

        hashed = hashlib.sha256(token.encode("utf-8")).hexdigest()
        key = (
            db.query(ApiKey)
            .filter(ApiKey.hashed_key == hashed, ApiKey.revoked_at.is_(None))
            .first()
        )
        if not key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = db.query(User).filter(User.id == key.user_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        try:
            key.last_used_at = _dt.utcnow()
            db.commit()
        except Exception:
            db.rollback()
        return user

    # ── JWT path ──────────────────────────────────────────────────────────────
    from app.core.auth import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    email = payload.get("sub")
    from app.core.models import User
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_optional_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    """FastAPI dependency — returns authenticated User or None (no 401)."""
    if not token:
        return None
    from app.core.auth import verify_access_token
    payload = verify_access_token(token)
    if not payload:
        return None
    email = payload.get("sub")
    from app.core.models import User
    return db.query(User).filter(User.email == email).first()
