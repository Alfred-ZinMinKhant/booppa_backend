"""Public read endpoints for the four CMS content types.

Ported from the Django service `booppa-cms` (`cms_admin/cms/views.py`), which is
being retired into this app. `app/main.py` dual-mounts the composite router at
both `/api/v1` and `/api`, and this router is included with `prefix="/public"`,
so these land at `/api/public/blogs/` — byte-identical to the path the frontend
already calls. The `next.config.js` rewrite therefore only changes host.

Two things here look wrong and are deliberate:

1. **Trailing slashes are declared on every route.** Django ran with
   `APPEND_SLASH`, so every caller in `booppa-nextjs` sends the slash. FastAPI
   would answer a slashless declaration with a 307, and a 307 is not free — some
   clients drop the body/method on redirect. Declared with the slash, the
   incoming URLs match exactly and nothing redirects.

2. **Timestamps are formatted by hand** (`_dj_dt`) rather than left to FastAPI's
   encoder. `JsonResponse` used `DjangoJSONEncoder`, which truncates microseconds
   to milliseconds and renders UTC as a `Z` suffix instead of `+00:00`. The
   frontend parses these, and the cutover is verified by diffing this output
   against the live Django output, so the rendering has to match to the byte.

The other asymmetry worth knowing: `images` is a list of **absolute URL strings**
here, but a list of `{id, url, caption}` objects on the admin endpoints. That is
load-bearing — `booppa-nextjs/app/blog/page.tsx` does `<Image src={p.images[0]}>`
while `CmsCrud.tsx` reads `img.url`. Do not "harmonise" them.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.models import BlogPost, CompliancePost, RfpTip, VendorGuide
from app.core.route_classes import RetryAPIRoute

router = APIRouter(route_class=RetryAPIRoute)


def _dj_dt(value: Optional[datetime]) -> Optional[str]:
    """Render a datetime the way `DjangoJSONEncoder` did.

    Awareness is preserved rather than normalised, because the CMS tables are
    split (see the section note in `app/core/models.py`): `blog_posts` columns
    are naive and serialize with no suffix (`2026-03-31T04:02:10.501`), while
    `rfp_tips`/`compliance_posts`/`vendor_guides` are `timestamptz` and get a
    `Z`. Forcing UTC onto the naive ones would append a `Z` the live service
    never emitted and assert a timezone the column does not record.

    Aware values *are* converted to UTC first: psycopg2 renders `timestamptz` in
    the session timezone, which is not guaranteed to be UTC and would otherwise
    print a different offset for an identical instant. Django, with `USE_TZ`,
    always handed the encoder UTC.
    """
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc)
    rendered = value.isoformat()
    if value.microsecond:
        # ECMA-262: milliseconds, not microseconds.
        rendered = rendered[:23] + rendered[26:]
    if rendered.endswith("+00:00"):
        rendered = rendered[:-6] + "Z"
    return rendered


def _image_url(stored: str) -> str:
    """Absolute, publicly-fetchable URL for a `blog_post_images.image` value.

    Every value resolves through this backend's media route, which 302s to a
    freshly minted presign — the stored URL stays permanent instead of expiring
    with a 7-day presign.

    The column holds two generations of value:

    - **Post-backfill**: an S3 object key under `cms/`.
    - **Django-era**: a `MEDIA_ROOT`-relative path (`blog_images/foo.png`) left
      over from `booppa-cms`. Every such file was copied to S3 under `cms/`
      before the service was deleted, and `/api/public/cms-media/` pins the
      `cms/` prefix itself, so stripping/omitting it lands on the same object.
      Django is gone — there is no host left to serve the old form.

    Already-absolute values are passed through untouched.
    """
    if not stored:
        return ""
    if stored.startswith("http://") or stored.startswith("https://"):
        return stored
    key = stored[len("cms/"):] if stored.startswith("cms/") else stored.lstrip("/")
    base = (settings.API_PUBLIC_BASE_URL or settings.VERIFY_BASE_URL).rstrip("/")
    return f"{base}/api/public/cms-media/{key}"


def _serialize(post, *, include_category: bool = False) -> dict:
    data = {
        "id": str(post.id),
        "title": post.title,
        "slug": post.slug,
        "content": post.content,
        "author": post.author,
    }
    if include_category:
        data["category"] = post.category
    data.update(
        {
            "cta1_text": post.cta1_text,
            "cta1_url": post.cta1_url,
            "cta2_text": post.cta2_text,
            "cta2_url": post.cta2_url,
            "published_at": _dj_dt(post.published_at),
            "created_at": _dj_dt(post.created_at),
            "updated_at": _dj_dt(post.updated_at),
        }
    )
    return data


def _published_query(db, model):
    return (
        db.query(model)
        .filter(model.published.is_(True))
        .order_by(model.published_at.desc())
    )


def _list(model) -> dict:
    db = SessionLocal()
    try:
        rows = _published_query(db, model).all()
        return {"results": [_serialize(row) for row in rows]}
    finally:
        db.close()


def _detail(model, slug: str) -> dict:
    db = SessionLocal()
    try:
        row = (
            db.query(model)
            .filter(model.slug == slug, model.published.is_(True))
            .first()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        return _serialize(row)
    finally:
        db.close()


# ── Blogs ─────────────────────────────────────────────────────────────────────
# Blogs are the only type with images, and the only one honouring `?limit=`.


@router.get("/blogs/")
def public_blogs(limit: Optional[str] = Query(default=None)):
    db = SessionLocal()
    try:
        # `selectinload` replaces the Django version's per-post re-query
        # (`views.py:58-63`), which was an N+1 over the whole published set.
        query = _published_query(db, BlogPost).options(
            selectinload(BlogPost.images)
        )
        # Django ignored an unparseable or non-positive `limit` rather than
        # erroring (`views.py:45-52`); callers rely on that leniency, so this is
        # a plain `str` and not an `int` query param — FastAPI would 422 it.
        if limit:
            try:
                n = int(limit)
                if n > 0:
                    query = query.limit(n)
            except (TypeError, ValueError):
                pass

        results = []
        for post in query.all():
            data = _serialize(post, include_category=True)
            data["images"] = [_image_url(img.image) for img in post.images]
            results.append(data)
        return {"results": results}
    finally:
        db.close()


@router.get("/blogs/{slug}/")
def public_blog_detail(slug: str):
    db = SessionLocal()
    try:
        post = (
            db.query(BlogPost)
            .options(selectinload(BlogPost.images))
            .filter(BlogPost.slug == slug, BlogPost.published.is_(True))
            .first()
        )
        if post is None:
            raise HTTPException(status_code=404, detail="Not found")
        data = _serialize(post, include_category=True)
        data["images"] = [_image_url(img.image) for img in post.images]
        return data
    finally:
        db.close()


# ── Media ─────────────────────────────────────────────────────────────────────


@router.get("/cms-media/{path:path}")
def public_cms_media(path: str):
    """Serve a CMS image by its stable public URL.

    The bucket is private (it also holds customer reports), so this mints a
    short-lived presigned URL per request and redirects to it. The URL the
    frontend stores and caches — `/api/public/cms-media/blog_images/…` — never
    expires, which a presign handed straight to `<Image>` would.

    Only keys under `cms/` are reachable. Without that prefix pin this endpoint
    would be an unauthenticated reader for every object in `S3_BUCKET`, which
    includes generated PDPA reports and evidence packs.
    """
    # `{path:path}` matches greedily and does not decode `..` for us.
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=404, detail="Not found")

    from app.adapters.s3_storage import S3StorageAdapter

    url = S3StorageAdapter().presign_key(f"cms/{path}")
    if not url:
        raise HTTPException(status_code=404, detail="Not found")
    return RedirectResponse(
        url=url,
        status_code=302,
        # Well under the presign's 7-day life, so a cached redirect can never
        # outlive the URL it points at.
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ── RFP tips / compliance / vendor guides ────────────────────────────────────


@router.get("/rfp-tips/")
def public_rfp_tips():
    return _list(RfpTip)


@router.get("/rfp-tips/{slug}/")
def public_rfp_tip_detail(slug: str):
    return _detail(RfpTip, slug)


@router.get("/compliance/")
def public_compliance():
    return _list(CompliancePost)


@router.get("/compliance/{slug}/")
def public_compliance_detail(slug: str):
    return _detail(CompliancePost, slug)


@router.get("/vendor-guides/")
def public_vendor_guides():
    return _list(VendorGuide)


@router.get("/vendor-guides/{slug}/")
def public_vendor_guide_detail(slug: str):
    return _detail(VendorGuide, slug)
