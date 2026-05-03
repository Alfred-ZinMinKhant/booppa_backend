import logging
import redis as _redis_lib
from fastapi import APIRouter, Depends, HTTPException, status, Body, Security
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from typing import Optional
from sqlalchemy.orm import Session
from app.core.auth import (
    authenticate_user, register_user,
    create_access_token, create_refresh_token,
    verify_refresh_token, verify_access_token,
    create_password_reset_token, verify_password_reset_token,
    get_password_hash,
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
    plan: str = "free"
    role: str = "VENDOR"


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(BaseModel):
    email: str
    password: str
    company: str = ""
    website: Optional[str] = None
    uen: Optional[str] = None
    industry: Optional[str] = None


class ProcurementRegisterRequest(BaseModel):
    email: str
    password: str
    company: str
    uen: Optional[str] = None
    industry: Optional[str] = None


class MeOut(BaseModel):
    id: str
    email: str
    full_name: str | None = None
    company: str | None
    website: str | None = None
    company_description: str | None = None
    role: str
    plan: str = "free"
    is_admin: bool = False
    has_claimed_profile: bool = False
    is_verified: bool = False


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    company: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    company_description: Optional[str] = None


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
        access_token=access_token, token_type="bearer", refresh_token=refresh_token,
        plan=getattr(user, "plan", "free") or "free",
        role=getattr(user, "role", "VENDOR") or "VENDOR",
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
        access_token=access_token, token_type="bearer", refresh_token=refresh_token,
        plan=getattr(user, "plan", "free") or "free",
        role=getattr(user, "role", "VENDOR") or "VENDOR",
    )


# ── Registration ──────────────────────────────────────────────────────────────

@router.post("/register", status_code=201, response_model=TokenWithRefresh)
async def register(body: RegisterRequest, db: Session = Depends(get_db)):
    try:
        user = register_user(db, email=body.email, password=body.password, company=body.company, website=body.website, uen=body.uen, industry=body.industry)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    access_token  = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    _store_token(refresh_token)
    return TokenWithRefresh(
        access_token=access_token, token_type="bearer", refresh_token=refresh_token,
        plan="free",
    )


# ── Procurement Registration ─────────────────────────────────────────────────

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "me.com", "mail.com",
    "protonmail.com", "proton.me", "zoho.com", "yandex.com",
    "gmx.com", "gmx.net", "tutanota.com", "fastmail.com",
}


@router.post("/register/procurement", status_code=201, response_model=TokenWithRefresh)
async def register_procurement(body: ProcurementRegisterRequest, db: Session = Depends(get_db)):
    # Reject free email providers
    domain = body.email.rsplit("@", 1)[-1].lower()
    if domain in FREE_EMAIL_DOMAINS:
        raise HTTPException(
            status_code=422,
            detail="Please use your company email address. Free email providers (Gmail, Yahoo, etc.) are not accepted for procurement accounts.",
        )

    try:
        user = register_user(
            db,
            email=body.email,
            password=body.password,
            company=body.company,
            uen=body.uen,
            industry=body.industry,
            role="PROCUREMENT",
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    access_token = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    _store_token(refresh_token)
    return TokenWithRefresh(
        access_token=access_token, token_type="bearer", refresh_token=refresh_token,
        plan="free",
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

    from app.core.models_v10 import MarketplaceVendor
    from app.core.models_v6 import VerifyRecord, LifecycleStatus
    has_claimed_profile = db.query(MarketplaceVendor).filter(
        MarketplaceVendor.claimed_by_user_id == user.id
    ).first() is not None
    is_verified = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == user.id,
        VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
    ).first() is not None

    return MeOut(
        id=str(user.id),
        email=user.email,
        full_name=getattr(user, "full_name", None),
        company=getattr(user, "company", None),
        website=getattr(user, "website", None),
        company_description=getattr(user, "company_description", None),
        role=getattr(user, "role", "VENDOR"),
        plan=getattr(user, "plan", "free") or "free",
        is_admin=is_admin,
        has_claimed_profile=has_claimed_profile,
        is_verified=is_verified,
    )


@router.patch("/me", response_model=MeOut)
async def update_me(
    body: ProfileUpdate,
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

    if body.full_name is not None:
        user.full_name = body.full_name
    if body.company is not None:
        user.company = body.company
    if body.website is not None:
        user.website = body.website
    if body.industry is not None:
        user.industry = body.industry
    if body.company_description is not None:
        user.company_description = body.company_description

    if body.email is not None and body.email != user.email:
        # Check if email is already taken
        existing = db.query(User).filter(User.email == body.email).first()
        if existing:
            raise HTTPException(status_code=409, detail="Email already in use")
        user.email = body.email
        # Note: In a real app, we might want to re-issue tokens or require verification

    db.commit()
    db.refresh(user)

    # If it's a vendor, also update MarketplaceVendor industry if needed
    if body.industry is not None:
        try:
            from app.core.models_v10 import MarketplaceVendor
            mv = db.query(MarketplaceVendor).filter(
                MarketplaceVendor.claimed_by_user_id == user.id
            ).first()
            if mv:
                mv.industry = body.industry
                db.commit()
        except Exception:
            pass

    is_admin = bool(settings.ADMIN_USER and user.email == settings.ADMIN_USER)

    from app.core.models_v10 import MarketplaceVendor
    from app.core.models_v6 import VerifyRecord, LifecycleStatus
    has_claimed_profile = db.query(MarketplaceVendor).filter(
        MarketplaceVendor.claimed_by_user_id == user.id
    ).first() is not None
    is_verified = db.query(VerifyRecord).filter(
        VerifyRecord.vendor_id == user.id,
        VerifyRecord.lifecycle_status == LifecycleStatus.ACTIVE,
    ).first() is not None

    return MeOut(
        id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        company=user.company,
        website=user.website,
        role=user.role,
        plan=getattr(user, "plan", "free") or "free",
        is_admin=is_admin,
        has_claimed_profile=has_claimed_profile,
        is_verified=is_verified,
    )


# ── Password reset ────────────────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: str


def _reset_token_used_key(jti: str) -> str:
    return f"pwreset:used:{jti}"


def _mark_reset_token_used(token: str, ttl_seconds: int) -> None:
    r = _get_redis()
    if r:
        try:
            r.setex(_reset_token_used_key(token), max(ttl_seconds, 60), "1")
            return
        except Exception as exc:
            logger.warning("[Auth] Redis setex failed (reset): %s", exc)
    _token_fallback.add(_reset_token_used_key(token))


def _reset_token_already_used(token: str) -> bool:
    r = _get_redis()
    if r:
        try:
            return bool(r.exists(_reset_token_used_key(token)))
        except Exception as exc:
            logger.warning("[Auth] Redis exists failed (reset): %s", exc)
    return _reset_token_used_key(token) in _token_fallback


@router.post("/forgot-password", status_code=202)
async def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Always returns 202 to prevent email enumeration. Sends a reset link if the user exists."""
    from app.core.models import User
    from app.services.email_service import EmailService
    import os as _os

    user = db.query(User).filter(User.email == body.email).first()
    if user:
        token = create_password_reset_token(user.email)
        base = (
            _os.environ.get("NEXT_PUBLIC_BASE_URL")
            or _os.environ.get("BACKEND_BASE_URL")
            or "http://localhost:3000"
        )
        reset_url = f"{base.rstrip('/')}/reset-password?token={token}"
        body_html = f"""
        <html><body style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#0f172a">
            <h2>Reset your BOOPPA password</h2>
            <p>We received a request to reset the password for <b>{user.email}</b>.</p>
            <p>Click the button below to choose a new password. This link expires in 30 minutes and can only be used once.</p>
            <p><a href="{reset_url}" style="background:#10b981;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600">Reset password</a></p>
            <p>If you didn&apos;t request this, you can safely ignore this email.</p>
            <p style="color:#64748b;font-size:12px">Or copy this link: {reset_url}</p>
        </body></html>
        """
        try:
            await EmailService().send_html_email(user.email, "Reset your BOOPPA password", body_html)
        except Exception as exc:
            logger.error(f"[Auth] Failed to send password reset email: {exc}")
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    from app.core.models import User

    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters.")

    payload = verify_password_reset_token(body.token)
    if not payload:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Please request a new one.")

    if _reset_token_already_used(body.token):
        raise HTTPException(status_code=400, detail="This reset link has already been used. Please request a new one.")

    email = payload.get("sub")
    user = db.query(User).filter(User.email == email).first() if email else None
    if not user:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Please request a new one.")

    user.hashed_password = get_password_hash(body.password)
    db.commit()

    exp = payload.get("exp")
    try:
        from datetime import datetime as _dt, timezone as _tz
        ttl = int(exp - _dt.now(_tz.utc).timestamp()) if exp else 1800
    except Exception:
        ttl = 1800
    _mark_reset_token_used(body.token, ttl)

    return {"ok": True}
