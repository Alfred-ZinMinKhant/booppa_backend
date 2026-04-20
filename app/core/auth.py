from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta, timezone
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


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
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=24))
    to_encode.update({"exp": expire, "type": "access"})
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


def verify_access_token(token: str):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise JWTError("Invalid token type")
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
            from app.core.models_v10 import MarketplaceVendor
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
