"""Contract tests for the CMS public endpoints ported off `booppa-cms`.

These assert the *Django* behaviour, not what a fresh FastAPI endpoint would
naturally do — the frontend is already built against the Django responses, so
the quirks (the `{"results": []}` envelope, the lenient `?limit=`, the `Z`
timestamp suffix, `images` as bare URL strings) are the contract.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.db import SessionLocal
from app.core.models import BlogImage, BlogPost, CompliancePost, RfpTip, VendorGuide

# The endpoints are reachable at both mounts; `/api` is what the frontend calls.
BASE = "/api/public"


def _now(model):
    """`blog_posts` is naive `timestamp`, the other three are `timestamptz` —
    a real schema split inherited from Django, not a test convenience."""
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


@pytest.fixture
def cms_db():
    db = SessionLocal()
    created = []
    try:
        yield db, created
    finally:
        for obj in created:
            db.delete(db.merge(obj))
        db.commit()
        db.close()


def test_list_envelope_and_published_filter(client, cms_db):
    db, created = cms_db
    live = _mk(BlogPost, title="Live")
    draft = _mk(BlogPost, title="Draft", published=False, published_at=None)
    db.add_all([live, draft])
    db.commit()
    created += [live, draft]

    body = client.get(f"{BASE}/blogs/").json()
    assert set(body) == {"results"}
    slugs = [r["slug"] for r in body["results"]]
    assert live.slug in slugs
    assert draft.slug not in slugs


def test_list_ordered_by_published_at_desc(client, cms_db):
    db, created = cms_db
    now = _now(RfpTip)
    older = _mk(RfpTip, published_at=now - timedelta(days=2))
    newer = _mk(RfpTip, published_at=now - timedelta(days=1))
    db.add_all([older, newer])
    db.commit()
    created += [older, newer]

    slugs = [r["slug"] for r in client.get(f"{BASE}/rfp-tips/").json()["results"]]
    assert slugs.index(newer.slug) < slugs.index(older.slug)


@pytest.mark.parametrize("limit", ["0", "-1", "abc", ""])
def test_limit_is_ignored_when_not_a_positive_int(client, cms_db, limit):
    """Django swallowed these rather than erroring (`views.py:45-52`). A 422
    here would break `/resources`, which passes `?limit=` unvalidated."""
    db, created = cms_db
    posts = [_mk(BlogPost) for _ in range(3)]
    db.add_all(posts)
    db.commit()
    created += posts

    resp = client.get(f"{BASE}/blogs/", params={"limit": limit})
    assert resp.status_code == 200
    returned = {r["slug"] for r in resp.json()["results"]}
    assert {p.slug for p in posts} <= returned


def test_limit_applies_to_blogs(client, cms_db):
    db, created = cms_db
    posts = [_mk(BlogPost) for _ in range(3)]
    db.add_all(posts)
    db.commit()
    created += posts

    body = client.get(f"{BASE}/blogs/", params={"limit": "2"}).json()
    assert len(body["results"]) == 2


def test_aware_timestamps_render_with_z(client, cms_db):
    """`compliance_posts` is `timestamptz` (Django migration 0004, USE_TZ on).
    `DjangoJSONEncoder` rendered UTC as `Z`, never `+00:00`."""
    db, created = cms_db
    post = _mk(CompliancePost, published_at=datetime(2026, 8, 4, 9, 30, tzinfo=timezone.utc))
    db.add(post)
    db.commit()
    created.append(post)

    body = client.get(f"{BASE}/compliance/{post.slug}/").json()
    assert body["published_at"] == "2026-08-04T09:30:00Z"
    assert "+00:00" not in body["published_at"]


def test_blog_timestamps_render_without_suffix(client, cms_db):
    """`blog_posts` predates USE_TZ and is naive `timestamp`. The live service
    emits `2026-03-31T04:02:10.501` — no `Z`. Appending one here would both
    break byte-equality at cutover and claim a timezone the column never stored.
    Microseconds truncate to milliseconds, as DjangoJSONEncoder did."""
    db, created = cms_db
    post = _mk(BlogPost, published_at=datetime(2026, 3, 31, 4, 2, 10, 501234))
    db.add(post)
    db.commit()
    created.append(post)

    body = client.get(f"{BASE}/blogs/{post.slug}/").json()
    assert body["published_at"] == "2026-03-31T04:02:10.501"


def test_blog_images_are_absolute_url_strings(client, cms_db):
    """Not `{id,url,caption}` objects — `app/blog/page.tsx` does
    `<Image src={p.images[0]}>` directly. That shape is admin-only."""
    db, created = cms_db
    post = _mk(BlogPost)
    db.add(post)
    db.commit()
    db.add(BlogImage(blog_post_id=post.id, image="blog_images/x.png", caption="c"))
    db.commit()
    created.append(post)

    body = client.get(f"{BASE}/blogs/{post.slug}/").json()
    assert len(body["images"]) == 1
    assert isinstance(body["images"][0], str)
    assert body["images"][0].startswith("https://")
    # Django-era un-prefixed value. `booppa-cms` is deleted, so this must route
    # through the backend media route too — the old `/media/` host is gone.
    assert body["images"][0].endswith("/api/public/cms-media/blog_images/x.png")


def test_non_blog_types_have_no_category_or_images(client, cms_db):
    db, created = cms_db
    guide = _mk(VendorGuide)
    db.add(guide)
    db.commit()
    created.append(guide)

    body = client.get(f"{BASE}/vendor-guides/{guide.slug}/").json()
    assert "category" not in body
    assert "images" not in body


def test_detail_404s_on_unpublished_and_missing(client, cms_db):
    db, created = cms_db
    draft = _mk(BlogPost, published=False, published_at=None)
    db.add(draft)
    db.commit()
    created.append(draft)

    assert client.get(f"{BASE}/blogs/{draft.slug}/").status_code == 404
    assert client.get(f"{BASE}/blogs/no-such-slug/").status_code == 404


def test_backfilled_image_key_routes_through_the_backend(client, cms_db):
    """A `cms/` value is an S3 key, not a Django path — it must resolve to this
    backend's media route. `booppa-cms` is now deleted, so this is the only way
    a backfilled image resolves at all."""
    db, created = cms_db
    post = _mk(BlogPost)
    db.add(post)
    db.commit()
    db.add(BlogImage(blog_post_id=post.id, image="cms/blog_images/x.png"))
    db.commit()
    created.append(post)

    url = client.get(f"{BASE}/blogs/{post.slug}/").json()["images"][0]
    assert url.endswith("/api/public/cms-media/blog_images/x.png")
    assert "cms.booppa.io" not in url


@pytest.mark.parametrize("path", ["../reports/secret.pdf", "a/../../x", "/etc/passwd"])
def test_cms_media_rejects_traversal(client, path):
    """The bucket also holds customer reports. Without the `cms/` pin plus this
    check, the route is an unauthenticated reader for the whole bucket."""
    assert client.get(f"{BASE}/cms-media/{path}").status_code == 404


def test_upload_image_rejects_bad_type_and_oversize():
    """Validation the Django `ImageField` never did at the HTTP layer. Both are
    caller errors and must raise before any S3 call is attempted."""
    from app.adapters.s3_storage import S3StorageAdapter

    adapter = S3StorageAdapter.__new__(S3StorageAdapter)  # no boto3 client needed
    with pytest.raises(ValueError):
        adapter.upload_image(b"x", "cms/blog_images/a.svg", "image/svg+xml")
    with pytest.raises(ValueError):
        adapter.upload_image(b"", "cms/blog_images/a.png", "image/png")
    with pytest.raises(ValueError):
        adapter.upload_image(
            b"x" * (S3StorageAdapter.MAX_IMAGE_BYTES + 1),
            "cms/blog_images/a.png",
            "image/png",
        )


def test_v1_mount_also_resolves(client):
    """Nothing should depend on the bare `/api` mount alone."""
    assert client.get("/api/v1/public/blogs/").status_code == 200


def test_trailing_slash_is_not_a_redirect(client):
    """Django's APPEND_SLASH means every caller sends the slash; a 307 back to a
    slashless route is a regression, not a cosmetic difference."""
    resp = client.get(f"{BASE}/blogs/", follow_redirects=False)
    assert resp.status_code == 200
