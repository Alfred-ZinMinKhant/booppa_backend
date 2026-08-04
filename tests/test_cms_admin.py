"""Tests for the CMS admin CRUD ported off `booppa-cms`.

Two things are being asserted here, and they pull in opposite directions:

1. The *contract* the already-built admin UI depends on — the `{"results": []}`
   envelope, `images` as `{id, url, caption}` objects, the publish/unpublish
   `published_at` stamping the Django model's `save()` did.
2. The *fixes* applied while porting — token fields dropped from list output,
   generic error text, validated status/priority, PATCH touching only supplied
   fields. Each of these was a real defect in `cms_admin/cms/admin_api.py`, so
   they are pinned rather than left to drift back.
"""

import uuid
from datetime import date, datetime, timezone

import pytest

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import (
    BlogImage,
    BlogPost,
    CompliancePost,
    DemoBooking,
    RfpTip,
    SupportTicket,
    SupportTicketReply,
    VendorGuide,
)

BASE = "/api/admin/cms"
TOKEN = "test-admin-token-value"


@pytest.fixture
def auth(monkeypatch):
    """`_admin_auth` accepts a bearer admin JWT, this static header, or Basic.
    The header is the cheapest of the three to exercise and takes the same path
    the other admin tests use."""
    monkeypatch.setattr(settings, "ADMIN_TOKEN", TOKEN)
    return {"X-Admin-Token": TOKEN}


@pytest.fixture
def cms_db():
    db = SessionLocal()
    created = []
    try:
        yield db, created
    finally:
        for obj in created:
            try:
                db.delete(db.merge(obj))
            except Exception:
                db.rollback()
        db.commit()
        db.close()


def _now(model):
    now = datetime.now(timezone.utc)
    return now.replace(tzinfo=None) if model is BlogPost else now


def _mk(model, **kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        title="T",
        slug=f"s-{uuid.uuid4().hex[:8]}",
        content="body",
        author="A",
        published=True,
        published_at=_now(model),
    )
    defaults.update(kwargs)
    return model(**defaults)


# ── Auth ──────────────────────────────────────────────────────────────────────


def test_unauthenticated_is_rejected(client):
    assert client.get(f"{BASE}/blogs/").status_code == 401


def test_wrong_token_is_rejected(client, auth):
    res = client.get(f"{BASE}/blogs/", headers={"X-Admin-Token": "nope"})
    assert res.status_code == 401


# ── Routing ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["blogs", "rfp-tips", "compliance", "vendor-guides"])
def test_each_content_kind_lists(client, auth, kind):
    body = client.get(f"{BASE}/{kind}/", headers=auth).json()
    assert set(body) == {"results"}


def test_unknown_kind_is_404(client, auth):
    assert client.get(f"{BASE}/widgets/", headers=auth).status_code == 404


def test_bookings_and_tickets_are_not_swallowed_by_the_kind_catchall(client, auth):
    """`/{kind}/` matches `/bookings/` too. If registration order ever regresses
    these come back 404 "Unknown content type" instead of a list."""
    for path in ("bookings", "tickets"):
        body = client.get(f"{BASE}/{path}/", headers=auth).json()
        assert "results" in body, path


# ── Content CRUD ──────────────────────────────────────────────────────────────


def test_create_returns_201_and_derives_slug(client, auth, cms_db):
    db, created = cms_db
    res = client.post(
        f"{BASE}/blogs/",
        headers=auth,
        json={"title": "Hello There World", "content": "x"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["slug"] == "hello-there-world"
    assert data["images"] == []
    created.append(db.query(BlogPost).filter(BlogPost.id == data["id"]).one())


def test_create_unpublished_has_no_published_at(client, auth, cms_db):
    db, created = cms_db
    res = client.post(
        f"{BASE}/blogs/", headers=auth, json={"title": "Draft One", "content": "x"}
    )
    data = res.json()
    created.append(db.query(BlogPost).filter(BlogPost.id == data["id"]).one())
    assert data["published"] is False
    assert data["published_at"] is None


@pytest.mark.parametrize("model", [BlogPost, RfpTip, CompliancePost, VendorGuide])
def test_publish_stamps_and_unpublish_clears_published_at(client, auth, cms_db, model):
    """The side-effect lived in the Django model's `save()`. It matters because
    the public endpoints order by `published_at desc` — losing the stamp buries
    a freshly published post at the end of the list."""
    kind = {
        BlogPost: "blogs",
        RfpTip: "rfp-tips",
        CompliancePost: "compliance",
        VendorGuide: "vendor-guides",
    }[model]
    db, created = cms_db
    row = _mk(model, published=False, published_at=None)
    db.add(row)
    db.commit()
    created.append(row)

    published = client.patch(
        f"{BASE}/{kind}/{row.id}/", headers=auth, json={"published": True}
    ).json()
    assert published["published_at"] is not None

    unpublished = client.patch(
        f"{BASE}/{kind}/{row.id}/", headers=auth, json={"published": False}
    ).json()
    assert unpublished["published_at"] is None


def test_duplicate_slug_returns_generic_400_without_db_text(client, auth, cms_db):
    """Django returned `str(exc)` — raw psycopg2 text naming the table, the
    constraint and the conflicting value."""
    db, created = cms_db
    existing = _mk(BlogPost)
    db.add(existing)
    db.commit()
    created.append(existing)

    res = client.post(
        f"{BASE}/blogs/",
        headers=auth,
        json={"title": "Dup", "slug": existing.slug, "content": "x"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "blog_posts" not in detail
    assert "DETAIL" not in detail


def test_get_and_delete_roundtrip(client, auth, cms_db):
    db, _ = cms_db
    row = _mk(BlogPost)
    db.add(row)
    db.commit()
    row_id = str(row.id)

    assert client.get(f"{BASE}/blogs/{row_id}/", headers=auth).status_code == 200
    assert client.delete(f"{BASE}/blogs/{row_id}/", headers=auth).status_code == 204
    assert client.get(f"{BASE}/blogs/{row_id}/", headers=auth).status_code == 404


def test_non_uuid_id_is_404_not_500(client, auth):
    assert client.get(f"{BASE}/blogs/not-a-uuid/", headers=auth).status_code == 404


def test_admin_images_are_objects_not_url_strings(client, auth, cms_db):
    """The public endpoint returns bare URL strings for the same rows. The
    asymmetry is what `CmsCrud.tsx` (`img.url`, `img.id`) and `app/blog/page.tsx`
    (`<Image src={p.images[0]}>`) each expect."""
    db, created = cms_db
    post = _mk(BlogPost)
    db.add(post)
    db.commit()
    img = BlogImage(blog_post_id=post.id, image="cms/blog_images/x.png", caption="c")
    db.add(img)
    db.commit()
    created += [img, post]

    data = client.get(f"{BASE}/blogs/{post.id}/", headers=auth).json()
    assert data["images"] == [
        {"id": img.id, "url": data["images"][0]["url"], "caption": "c"}
    ]
    assert data["images"][0]["url"].endswith("/api/public/cms-media/blog_images/x.png")


# ── Direct-to-S3 image upload ─────────────────────────────────────────────────
#
# Uploads bypass this service entirely: the browser POSTs to S3 with a presigned
# ticket, then calls `confirm` with the key. The multipart route still exists but
# cannot carry a real image — Cloudflare → CloudFront → Amplify caps the request
# body near 1MB and the blog's own images are 1–2MB.


@pytest.fixture
def blog(cms_db):
    db, created = cms_db
    post = _mk(BlogPost)
    db.add(post)
    db.commit()
    created.append(post)
    return post


def test_presign_rejects_disallowed_content_type(client, auth, blog):
    res = client.post(
        f"{BASE}/blogs/{blog.id}/images/presign/",
        headers=auth,
        json={"content_type": "image/gif", "size": 1000},
    )
    assert res.status_code == 400


def test_presign_rejects_oversized_file_before_s3_roundtrip(client, auth, blog):
    res = client.post(
        f"{BASE}/blogs/{blog.id}/images/presign/",
        headers=auth,
        json={"content_type": "image/png", "size": 50 * 1024 * 1024},
    )
    assert res.status_code == 400


@pytest.mark.parametrize(
    "key",
    [
        "reports/some-customer-report.pdf",
        "cms/blog_images/other-post-id/x.png",
        "../reports/x.pdf",
        "",
    ],
)
def test_confirm_refuses_keys_outside_this_post(client, auth, blog, key):
    """The bucket also holds customer PDPA reports and evidence packs. If
    `confirm` trusted the supplied key, an admin-token holder could register one
    as a blog image and have it served publicly and unauthenticated through
    `/api/public/cms-media/…`. The key must be re-derived, never accepted."""
    res = client.post(
        f"{BASE}/blogs/{blog.id}/images/confirm/",
        headers=auth,
        json={"key": key},
    )
    assert res.status_code == 400
    assert not res.json().get("id")


def test_confirm_refuses_nested_key_under_correct_prefix(client, auth, blog):
    """A key that starts with the right prefix but nests deeper is still not one
    we minted — the guard checks for a trailing path separator too."""
    res = client.post(
        f"{BASE}/blogs/{blog.id}/images/confirm/",
        headers=auth,
        json={"key": f"cms/blog_images/{blog.id}/../../../reports/x.pdf"},
    )
    assert res.status_code == 400


def test_confirm_refuses_key_with_no_object_behind_it(client, auth, blog):
    """A browser upload that silently failed must not leave a row pointing at a
    nonexistent object — that renders as a permanently broken image."""
    res = client.post(
        f"{BASE}/blogs/{blog.id}/images/confirm/",
        headers=auth,
        json={"key": f"cms/blog_images/{blog.id}/{uuid.uuid4().hex}.png"},
    )
    assert res.status_code == 400


def test_presign_and_confirm_are_admin_gated(client, blog):
    for path in ("presign", "confirm"):
        assert (
            client.post(f"{BASE}/blogs/{blog.id}/images/{path}/", json={}).status_code
            == 401
        )


# ── Bookings ──────────────────────────────────────────────────────────────────


@pytest.fixture
def booking(cms_db):
    db, created = cms_db
    row = DemoBooking(
        id=uuid.uuid4(),
        slot_id="s1",
        slot_date=date(2026, 9, 1),
        start_time="09:00",
        end_time="10:00",
        customer_name="C",
        customer_email="c@example.com",
        status="confirmed",
        booking_token=uuid.uuid4().hex[:32],
    )
    db.add(row)
    db.commit()
    created.append(row)
    return row


def test_booking_list_never_leaks_booking_token(client, auth, booking):
    """`booking_token` is the customer's own cancel/reschedule credential.
    Django emitted one per row on every list load."""
    body = client.get(f"{BASE}/bookings/", headers=auth).json()
    assert body["results"], "fixture booking missing"
    for row in body["results"]:
        assert "booking_token" not in row


def test_booking_status_is_validated(client, auth, booking):
    res = client.patch(
        f"{BASE}/bookings/{booking.id}/", headers=auth, json={"status": "banana"}
    )
    assert res.status_code == 400


def test_booking_status_updates(client, auth, booking):
    res = client.patch(
        f"{BASE}/bookings/{booking.id}/", headers=auth, json={"status": "completed"}
    )
    assert res.status_code == 200
    assert res.json()["status"] == "completed"


# ── Tickets ───────────────────────────────────────────────────────────────────


@pytest.fixture
def ticket(cms_db):
    db, created = cms_db
    row = SupportTicket(
        id=uuid.uuid4(),
        ticket_id=f"TKT-{uuid.uuid4().hex[:8]}",
        tracking_token=uuid.uuid4().hex,
        name="N",
        email="n@example.com",
        category="general",
        subject="S",
        message="M",
        status="open",
        priority="medium",
        assigned_to="alice",
    )
    db.add(row)
    db.commit()
    created.append(row)
    return row


def test_ticket_responses_never_leak_tracking_token(client, auth, ticket):
    """`tracking_token` lets anyone holding it read the customer's ticket
    without authenticating. It is dropped from both list and detail."""
    listed = client.get(f"{BASE}/tickets/", headers=auth).json()["results"]
    assert listed
    assert all("tracking_token" not in r for r in listed)

    detail = client.get(f"{BASE}/tickets/{ticket.id}/", headers=auth).json()
    assert "tracking_token" not in detail


def test_ticket_patch_touches_only_supplied_fields(client, auth, ticket):
    """Django's `save(update_fields=["status","priority","assigned_to",...])`
    rewrote all three on every PATCH. Harmless in the happy path, destructive
    when two admins edit the same ticket concurrently."""
    res = client.patch(
        f"{BASE}/tickets/{ticket.id}/", headers=auth, json={"status": "resolved"}
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "resolved"
    assert body["priority"] == "medium"
    assert body["assigned_to"] == "alice"


@pytest.mark.parametrize(
    "payload", [{"status": "nonsense"}, {"priority": "catastrophic"}]
)
def test_ticket_status_and_priority_are_validated(client, auth, ticket, payload):
    res = client.patch(f"{BASE}/tickets/{ticket.id}/", headers=auth, json=payload)
    assert res.status_code == 400


def test_reply_creation_joins_on_ticket_id_string(client, auth, ticket, cms_db):
    """Replies key off the `ticket_id` string rather than a FK — matching
    `app/api/tickets.py`, which reads them the same way."""
    db, created = cms_db
    res = client.post(
        f"{BASE}/tickets/{ticket.id}/replies/",
        headers=auth,
        json={"message": "  on it  "},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["message"] == "on it"
    assert body["ticket_id"] == ticket.ticket_id

    created.append(
        db.query(SupportTicketReply).filter(SupportTicketReply.id == body["id"]).one()
    )

    detail = client.get(f"{BASE}/tickets/{ticket.id}/", headers=auth).json()
    assert [r["message"] for r in detail["replies"]] == ["on it"]


def test_empty_reply_is_rejected(client, auth, ticket):
    res = client.post(
        f"{BASE}/tickets/{ticket.id}/replies/", headers=auth, json={"message": "   "}
    )
    assert res.status_code == 400


# ── Image upload ──────────────────────────────────────────────────────────────


def test_image_upload_rejects_disallowed_content_type(client, auth, cms_db):
    """Django's `ImageField` validated nothing at the HTTP layer. Rejection has
    to happen before the bytes reach S3 — this bucket also holds customer
    reports, so what lands in it is not a cosmetic concern."""
    db, created = cms_db
    post = _mk(BlogPost)
    db.add(post)
    db.commit()
    created.append(post)

    res = client.post(
        f"{BASE}/blogs/{post.id}/images/",
        headers=auth,
        files={"image": ("evil.svg", b"<svg/>", "image/svg+xml")},
    )
    assert res.status_code == 400


def test_image_upload_writes_a_cms_prefixed_key(client, auth, cms_db, monkeypatch):
    """The stored value must start with `cms/`: `_image_url` routes anything
    else to the legacy Django host, and `/api/public/cms-media/` only serves
    keys under that prefix."""
    db, created = cms_db
    post = _mk(BlogPost)
    db.add(post)
    db.commit()
    created.append(post)

    seen = {}

    def fake_upload(self, image_bytes, key, content_type):
        seen["key"] = key
        return key

    from app.adapters.s3_storage import S3StorageAdapter

    monkeypatch.setattr(S3StorageAdapter, "upload_image", fake_upload)

    res = client.post(
        f"{BASE}/blogs/{post.id}/images/",
        headers=auth,
        files={"image": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"caption": "cap"},
    )
    assert res.status_code == 201
    assert seen["key"].startswith(f"cms/blog_images/{post.id}/")
    assert seen["key"].endswith(".png")

    row = db.query(BlogImage).filter(BlogImage.id == res.json()["id"]).one()
    created.insert(0, row)
    assert row.image == seen["key"]
    assert row.caption == "cap"


def test_image_upload_to_missing_post_is_404(client, auth):
    res = client.post(
        f"{BASE}/blogs/{uuid.uuid4()}/images/",
        headers=auth,
        files={"image": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert res.status_code == 404
