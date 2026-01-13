from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from app.core.auth import authenticate_user, create_access_token
from app.core.db import get_db
from sqlalchemy.orm import Session
from fastapi import Security
from app.core.auth import verify_access_token
from app.core.config import settings

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenWithRefresh(Token):
    refresh_token: str


from fastapi import Body
from app.core.auth import verify_refresh_token, create_refresh_token

# In-memory store for valid refresh tokens (for demo; use DB in production)
valid_refresh_tokens = set()


@router.post("/token", response_model=TokenWithRefresh)
async def login_for_access_token(
    form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)
):
    user = authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": user.email})
    refresh_token = create_refresh_token(data={"sub": user.email})
    valid_refresh_tokens.add(refresh_token)
    return TokenWithRefresh(
        access_token=access_token, token_type="bearer", refresh_token=refresh_token
    )


@router.post("/refresh", response_model=TokenWithRefresh)
async def refresh_access_token(refresh_token: str = Body(..., embed=True)):
    if refresh_token not in valid_refresh_tokens:
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token")
    payload = verify_refresh_token(refresh_token)
    if not payload:
        valid_refresh_tokens.discard(refresh_token)
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    # Revoke old refresh token and issue a new one
    valid_refresh_tokens.discard(refresh_token)
    new_refresh_token = create_refresh_token(data={"sub": payload["sub"]})
    valid_refresh_tokens.add(new_refresh_token)
    access_token = create_access_token(data={"sub": payload["sub"]})
    return TokenWithRefresh(
        access_token=access_token, token_type="bearer", refresh_token=new_refresh_token
    )


@router.post("/revoke", status_code=204)
async def revoke_all_refresh_tokens(email: str = Body(..., embed=True)):
    # In production, filter by user/email in DB
    tokens_to_remove = [t for t in valid_refresh_tokens if email in t]
    for t in tokens_to_remove:
        valid_refresh_tokens.discard(t)
    return


class MeOut(BaseModel):
    email: str
    is_admin: bool = False


@router.get("/me", response_model=MeOut)
async def me(token: str = Security(oauth2_scheme)):
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    email = payload.get("sub")
    is_admin = False
    # Determine admin by matching to ADMIN_USER or ADMIN_TOKEN presence
    if settings.ADMIN_USER and email and email == settings.ADMIN_USER:
        is_admin = True
    return MeOut(email=email, is_admin=is_admin)
