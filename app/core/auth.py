from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Global access-token revocation cutoff ─────────────────────────────────────
# Access tokens are stateless and long-lived (24h), so revoking refresh tokens
# alone leaves outstanding access tokens usable. We stamp every access token with
# `iat` and record a per-user cutoff epoch in Redis; any access token issued
# before the cutoff is rejected. This is best-effort: if Redis is unreachable we
# fail OPEN (accept the token) rather than lock every user out on a Redis blip.
_revoke_redis = None
_revoke_redis_tried = False


def _get_revoke_redis():
    global _revoke_redis, _revoke_redis_tried
    if _revoke_redis is None and not _revoke_redis_tried:
        _revoke_redis_tried = True
        try:
            import redis as _redis_lib
            r = _redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
            r.ping()
            _revoke_redis = r
        except Exception as exc:
            logger.warning("[Auth] revoke-cutoff Redis unavailable: %s", exc)
    return _revoke_redis


def revoke_user_tokens(email: str) -> None:
    """Invalidate every access token issued to `email` before now."""
    r = _get_revoke_redis()
    if not r:
        logger.warning("[Auth] revoke_user_tokens: Redis unavailable, access-token cutoff not set for %s", email)
        return
    try:
        # Cutoff = now (whole seconds, matching how `iat` is encoded). Tokens with
        # iat < cutoff are rejected; a fresh login in the same second (iat == cutoff)
        # still works, so re-login immediately after "revoke all" is not blocked.
        cutoff = int(datetime.now(timezone.utc).timestamp())
        r.set(f"revoked_before:{email}", cutoff)
    except Exception as exc:
        logger.warning("[Auth] revoke_user_tokens failed for %s: %s", email, exc)


def _revoked_before(email: str) -> int:
    r = _get_revoke_redis()
    if not r:
        return 0
    try:
        v = r.get(f"revoked_before:{email}")
        return int(v) if v else 0
    except Exception as exc:
        logger.warning("[Auth] revoked_before lookup failed for %s: %s", email, exc)
        return 0


def verify_password(plain_password: str, hashed_password: str) -> bool:
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > 72:
        plain_password = password_bytes[:72].decode("utf-8", errors="ignore")
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    # bcrypt hard-limits to 72 bytes; truncate silently so bcrypt>=4.x never raises
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 72:
        password = password_bytes[:72].decode("utf-8", errors="ignore")
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(hours=24))
    # `iat` lets us revoke outstanding access tokens via a per-user cutoff.
    to_encode.update({"exp": expire, "iat": now, "type": "access"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def create_refresh_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(days=30))
    to_encode.update({"exp": expire, "type": "refresh"})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm="HS256")


def verify_refresh_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != "refresh":
            raise JWTError("Invalid token type")
        return payload
    except JWTError as e:
        logger.error(f"Refresh token verification failed: {e}")
        return None


def create_admin_token(username: str, expires_delta: timedelta = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=12))
    return jwt.encode(
        {"sub": username, "exp": expire, "type": "admin"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def verify_admin_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != "admin":
            raise JWTError("Invalid token type")
        return payload
    except JWTError as e:
        logger.warning(f"Admin token verification failed: {e}")
        return None


def create_password_reset_token(email: str, expires_delta: timedelta = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=30))
    return jwt.encode(
        {"sub": email, "exp": expire, "type": "password_reset"},
        settings.SECRET_KEY,
        algorithm="HS256",
    )


def verify_password_reset_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != "password_reset":
            raise JWTError("Invalid token type")
        return payload
    except JWTError as e:
        logger.warning(f"Password reset token verification failed: {e}")
        return None


def verify_access_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise JWTError("Invalid token type")
        # Reject tokens issued before the user's revocation cutoff (e.g. after a
        # "revoke all sessions"). Fails open if Redis is down (cutoff == 0).
        sub = payload.get("sub")
        iat = payload.get("iat")
        if sub and iat is not None:
            cutoff = _revoked_before(sub)
            if cutoff and int(iat) < cutoff:
                raise JWTError("Token revoked")
        return payload
    except JWTError as e:
        logger.error(f"Access token verification failed: {e}")
        return None


def authenticate_user(db, email: str, password: str):
    """Authenticate user against the database."""
    from app.core.models import User

    user = db.query(User).filter(User.email == email).first()
    if not user:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def register_user(
    db,
    email: str,
    password: str,
    company: str = None,
    website: str = None,
    uen: str = None,
    industry: str = None,
    role: str = "VENDOR",
):
    """Create a new user. Returns the user or raises ValueError on duplicate."""
    from app.core.models import User

    if db.query(User).filter(User.email == email).first():
        raise ValueError("Email already registered")
    user = User(
        email=email,
        hashed_password=get_password_hash(password),
        company=company or "",
        website=website or None,
        uen=uen or None,
        industry=industry or None,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Auto-claim or auto-create a MarketplaceVendor entry for vendor accounts
    if role == "VENDOR" and company:
        try:
            from datetime import timezone
            from app.core.models import MarketplaceVendor
            from app.services.marketplace import generate_slug

            mv = None

            # 1. Match by UEN (most reliable)
            if uen:
                mv = (
                    db.query(MarketplaceVendor)
                    .filter(
                        MarketplaceVendor.uen == uen,
                        MarketplaceVendor.claimed_by_user_id.is_(None),
                    )
                    .first()
                )

            # 2. Match by company name (case-insensitive)
            if mv is None:
                mv = (
                    db.query(MarketplaceVendor)
                    .filter(
                        MarketplaceVendor.company_name.ilike(company),
                        MarketplaceVendor.claimed_by_user_id.is_(None),
                    )
                    .first()
                )

            if mv:
                # Claim the existing entry
                mv.claimed_by_user_id = user.id
                mv.claimed_at = datetime.utcnow()
                if industry and not mv.industry:
                    mv.industry = industry
                if website and not mv.website:
                    mv.website = website
            else:
                # Create a fresh entry — ensure slug uniqueness
                base_slug = generate_slug(company)
                slug = base_slug
                counter = 1
                while (
                    db.query(MarketplaceVendor)
                    .filter(MarketplaceVendor.slug == slug)
                    .first()
                ):
                    slug = f"{base_slug}-{counter}"
                    counter += 1

                mv = MarketplaceVendor(
                    company_name=company,
                    slug=slug,
                    website=website or None,
                    uen=uen or None,
                    industry=industry or None,
                    country="Singapore",
                    source="manual",
                    claimed_by_user_id=user.id,
                    claimed_at=datetime.utcnow(),
                )
                db.add(mv)

            db.commit()
        except Exception:
            # Non-fatal: user account still created successfully
            pass

    return user
