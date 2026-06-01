"""Shared helpers for the gating / quota / seat / cancel tests.

Underscore-prefixed so pytest doesn't collect this module as a test file.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.auth import create_access_token, get_password_hash
from app.core.models import User
from app.core.models_enterprise import Organisation, OrganisationMember


def make_user(
    db: Session,
    *,
    email: str | None = None,
    plan: str = "free",
    role: str = "VENDOR",
    company: str | None = "Test Co",
) -> User:
    """Insert a User and return the row."""
    user = User(
        id=uuid.uuid4(),
        email=email or f"test-{uuid.uuid4().hex[:8]}@booppa.io",
        hashed_password=get_password_hash("not-used-in-tests"),
        role=role,
        plan=plan,
        company=company,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_org(
    db: Session,
    *,
    owner: User,
    name: str | None = None,
    slug: str | None = None,
    tier: str = "standard",
    max_seats: int | None = None,
) -> Organisation:
    """Insert an Organisation owned by `owner`, plus the owner-member row."""
    suffix = uuid.uuid4().hex[:6]
    org = Organisation(
        id=uuid.uuid4(),
        name=name or f"Test Org {suffix}",
        slug=slug or f"test-org-{suffix}",
        tier=tier,
        owner_user_id=owner.id,
        max_seats=max_seats,
    )
    db.add(org)
    db.flush()
    db.add(OrganisationMember(
        id=uuid.uuid4(),
        organisation_id=org.id,
        user_id=owner.id,
        role="owner",
    ))
    db.commit()
    db.refresh(org)
    return org


def auth_headers(user: User) -> dict[str, str]:
    """Bearer-JWT headers for `user`. Token is signed with the real SECRET_KEY."""
    token = create_access_token({"sub": user.email})
    return {"Authorization": f"Bearer {token}"}


def request_json(client: Any, method: str, path: str, user: User, **kwargs: Any):
    """Helper: call client.<method>(path) with the user's auth headers merged in."""
    headers = kwargs.pop("headers", {})
    headers.update(auth_headers(user))
    return getattr(client, method.lower())(path, headers=headers, **kwargs)
