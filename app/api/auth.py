import logging
import redis as _redis_lib
from fastapi import APIRouter, Depends, HTTPException, status, Body, Security
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from app.core.auth import (
    authenticate_user, register_user,
    create_access_token, create_refresh_token,
    verify_refresh_token, verify_access_token,
)
from app.core.db import get_db
from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

# ── Refresh token store (Redis with in-memory fallback) ───────────────────────
_REFRESH_TTL = 30 * 86400  # 30 days in seconds
_redis_client = None
_token_fallback: set[str] = set()  # only used when Redis is unavailable


def _get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            r = _redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
            r.ping()
            _redis_client = r
        except Exception as exc:
            logger.warning("[Auth] Redis unavailable, using in-memory token store: %s", exc)
    return _redis_client


def _store_token(token: str) -> None:
    r = _get_redis()
    if r:
        try:
            r.setex(f"refresh:{token}", _REFRESH_TTL, "1")
            return
        except Exception as exc:
            logger.warning("[Auth] Redis setex failed: %s", exc)
    _token_fallback.add(token)


def _token_exists(token: str) -> bool:
    r = _get_redis()
    if r:
        try:
            return bool(r.exists(f"refresh:{token}"))
        except Exception as exc:
            logger.warning("[Auth] Redis exists check failed: %s", exc)
    return token in _token_fallback


def _revoke_token(token: str) -> None:
    r = _get_redis()
    if r:
        try:
            r.delete(f"refresh:{token}")
            return
        except Exception as exc:
            logger.warning("[Auth] Redis delete failed: %s", exc)
    _token_fallback.discard(token)


# ── Schemas ───────────────────────────────────────────────────────────────────

class TokenWithRefresh(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    company: str = ""


class MeOut(BaseModel):
    id: str
    email: str
    company: str | None
    role: str
    is_admin: bool = False


# ── Form-based login (OAuth2 compatible) ─────────────────────────────────────

@router.post("/token", response_model=TokenWithRefresh)
async def login_form(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token  = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    _store_token(refresh_token)
    return TokenWithRefresh(
        access_token=access_token, token_type="bearer", refresh_token=refresh_token
    )


# ── JSON login (used by Next.js frontend) ────────────────────────────────────

@router.post("/login", response_model=TokenWithRefresh)
async def login_json(body: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    access_token  = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    _store_token(refresh_token)
    return TokenWithRefresh(
        access_token=access_token, token_type="bearer", refresh_token=refresh_token
    )


# ── Registration ──────────────────────────────────────────────────────────────

@router.post("/register", status_code=201, response_model=TokenWithRefresh)
async def register(body: RegisterRequest, db: Session = Depends(get_db)):
    try:
        user = register_user(db, email=body.email, password=body.password, company=body.company)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    access_token  = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    _store_token(refresh_token)
    return TokenWithRefresh(
        access_token=access_token, token_type="bearer", refresh_token=refresh_token
    )


# ── Token refresh ─────────────────────────────────────────────────────────────

@router.post("/refresh", response_model=TokenWithRefresh)
async def refresh_access_token(refresh_token: str = Body(..., embed=True)):
    if not _token_exists(refresh_token):
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token")
    payload = verify_refresh_token(refresh_token)
    if not payload:
        _revoke_token(refresh_token)
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    _revoke_token(refresh_token)
    new_refresh = create_refresh_token(data={"sub": payload["sub"]})
    _store_token(new_refresh)
    return TokenWithRefresh(
        access_token=create_access_token(data={"sub": payload["sub"]}),
        token_type="bearer",
        refresh_token=new_refresh,
    )


# ── Revoke ────────────────────────────────────────────────────────────────────

@router.post("/revoke", status_code=204)
async def revoke_all_refresh_tokens(email: str = Body(..., embed=True)):
    # With Redis, we can't enumerate by email without a separate index.
    # For now, log the intent — full revocation requires a token-to-email index.
    logger.info("[Auth] Revoke all tokens requested for %s (Redis: per-token revocation only)", email)


# ── Me ────────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=MeOut)
async def me(
    token: str = Security(oauth2_scheme),
    db: Session = Depends(get_db),
):
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    from app.core.models import User
    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    is_admin = bool(settings.ADMIN_USER and user.email == settings.ADMIN_USER)
    return MeOut(
        id=str(user.id),
        email=user.email,
        company=getattr(user, "company", None),
        role=getattr(user, "role", "VENDOR"),
        is_admin=is_admin,
    )
