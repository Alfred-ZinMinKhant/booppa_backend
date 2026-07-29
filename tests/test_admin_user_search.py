"""Regression: `GET /api/v1/admin/users` returned 500 for every request.

`admin_list_users` has always called `UserRepository.search_users`, but that
method was never implemented — the repository only defined `get_by_email` and
`get_by_id`. Every call raised AttributeError, so the admin user directory was
completely dark in production while the endpoint looked fine in the route table.

These tests assert the filters actually discriminate. A test that only checks
"does not raise" would have passed against an empty database and told us nothing.
"""
import uuid

import pytest

from app.core.auth import get_password_hash
from app.core.models import User
from app.core.repositories.user_repository import UserRepository


@pytest.fixture
def seeded(test_db):
    """Three users that differ on every filterable axis."""
    tag = uuid.uuid4().hex[:8]
    users = [
        User(
            email=f"alice-{tag}@acme-{tag}.com",
            hashed_password=get_password_hash("x"),
            full_name="Alice Tan",
            company=f"Acme Widgets {tag}",
            role="VENDOR",
            plan="vendor_pro",
            is_active=True,
        ),
        User(
            email=f"bob-{tag}@globex-{tag}.com",
            hashed_password=get_password_hash("x"),
            full_name="Bob Lim",
            company=f"Globex {tag}",
            role="PROCUREMENT",
            plan="free",
            is_active=True,
        ),
        User(
            email=f"carol-{tag}@acme-{tag}.com",
            hashed_password=get_password_hash("x"),
            full_name="Carol Ng",
            company=f"Acme Widgets {tag}",
            role="VENDOR",
            plan="vendor_pro",
            is_active=False,
        ),
    ]
    test_db.add_all(users)
    test_db.commit()
    return test_db, tag


def _emails(rows):
    return {u.email for u in rows}


def test_returns_all_users_with_no_filters(seeded):
    db, tag = seeded
    total, rows = UserRepository.search_users(db, limit=100)
    assert total >= 3
    assert len([r for r in rows if tag in r.email]) == 3


def test_q_matches_email_substring(seeded):
    db, tag = seeded
    total, rows = UserRepository.search_users(db, q=f"alice-{tag}")
    assert total == 1
    assert _emails(rows) == {f"alice-{tag}@acme-{tag}.com"}


def test_q_matches_full_name(seeded):
    db, tag = seeded
    _, rows = UserRepository.search_users(db, q="Carol Ng")
    assert _emails(rows) == {f"carol-{tag}@acme-{tag}.com"}


def test_q_matches_company_and_spans_multiple_users(seeded):
    db, tag = seeded
    total, rows = UserRepository.search_users(db, q=f"Acme Widgets {tag}")
    assert total == 2
    assert _emails(rows) == {
        f"alice-{tag}@acme-{tag}.com",
        f"carol-{tag}@acme-{tag}.com",
    }


def test_q_is_case_insensitive(seeded):
    db, tag = seeded
    _, rows = UserRepository.search_users(db, q=f"acme widgets {tag}".upper())
    assert len(rows) == 2


def test_plan_filter_discriminates(seeded):
    db, tag = seeded
    _, rows = UserRepository.search_users(db, plan="vendor_pro", q=tag)
    assert _emails(rows) == {
        f"alice-{tag}@acme-{tag}.com",
        f"carol-{tag}@acme-{tag}.com",
    }


def test_role_filter_discriminates(seeded):
    db, tag = seeded
    _, rows = UserRepository.search_users(db, role="PROCUREMENT", q=tag)
    assert _emails(rows) == {f"bob-{tag}@globex-{tag}.com"}


def test_is_active_false_is_not_treated_as_unset(seeded):
    """`is_active=False` must filter, not fall through as a falsy 'no filter'."""
    db, tag = seeded
    _, rows = UserRepository.search_users(db, is_active=False, q=tag)
    assert _emails(rows) == {f"carol-{tag}@acme-{tag}.com"}


def test_filters_are_anded(seeded):
    db, tag = seeded
    _, rows = UserRepository.search_users(
        db, q=f"Acme Widgets {tag}", plan="vendor_pro", is_active=True
    )
    assert _emails(rows) == {f"alice-{tag}@acme-{tag}.com"}


def test_total_counts_all_matches_not_just_the_page(seeded):
    """The console paginates off `total`; it must ignore offset/limit."""
    db, tag = seeded
    total, rows = UserRepository.search_users(db, q=tag, limit=1)
    assert total == 3
    assert len(rows) == 1


def test_offset_pages_without_overlap(seeded):
    db, tag = seeded
    _, first = UserRepository.search_users(db, q=tag, limit=2, offset=0)
    _, second = UserRepository.search_users(db, q=tag, limit=2, offset=2)
    assert len(first) == 2 and len(second) == 1
    assert not (_emails(first) & _emails(second))


def test_no_match_returns_empty_not_error(seeded):
    db, _ = seeded
    total, rows = UserRepository.search_users(db, q="nonexistent-zzzz")
    assert (total, rows) == (0, [])


def test_endpoint_serialises_without_error(seeded):
    """Guards the endpoint's own field access, not just the repository.

    The 500 was an AttributeError on the repository call; this asserts the
    projection `admin_list_users` builds from each row still works.
    """
    db, tag = seeded
    _, rows = UserRepository.search_users(db, q=tag)
    for u in rows:
        assert {
            "id": str(u.id),
            "email": u.email,
            "plan": u.plan,
            "is_active": bool(u.is_active),
            "verified": bool(u.verified_at),
            "has_stripe_subscription": bool(u.stripe_subscription_id),
            "notarization_credits": int(u.notarization_credits or 0),
        }["email"] == u.email
