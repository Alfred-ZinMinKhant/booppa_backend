"""
Authenticated CRUD JSON API for the CMS, mounted at /api/admin/*.

Auth: shared secret in `X-Admin-Token` header (must match `CMS_ADMIN_TOKEN` env var,
or fall back to Django `ADMIN_TOKEN`). The Next.js admin proxy forwards this header.
"""
import json
import os
import uuid

from django.http import JsonResponse, HttpResponse, HttpResponseNotAllowed
from django.utils.text import slugify
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import (
    BlogPost, BlogImage, RfpTip, CompliancePost, VendorGuide,
    DemoBooking, SupportTicket, SupportTicketReply,
)


# ── Auth ──────────────────────────────────────────────────────────────────────

def _expected_token() -> str | None:
    return os.environ.get("CMS_ADMIN_TOKEN") or os.environ.get("ADMIN_TOKEN")


def _check_auth(request) -> bool:
    expected = _expected_token()
    if not expected:
        return False
    sent = request.headers.get("X-Admin-Token") or request.META.get("HTTP_X_ADMIN_TOKEN")
    return bool(sent) and sent == expected


def _unauth():
    return JsonResponse({"detail": "Unauthorized"}, status=401)


# ── Serialization ─────────────────────────────────────────────────────────────

def _serialize_post(post, *, include_images=False):
    data = {
        "id": str(post.id),
        "title": post.title,
        "slug": post.slug,
        "content": post.content,
        "author": post.author,
        "cta1_text": post.cta1_text,
        "cta1_url": post.cta1_url,
        "cta2_text": post.cta2_text,
        "cta2_url": post.cta2_url,
        "published": post.published,
        "published_at": post.published_at.isoformat() if post.published_at else None,
        "created_at": post.created_at.isoformat() if post.created_at else None,
        "updated_at": post.updated_at.isoformat() if post.updated_at else None,
    }
    if include_images and hasattr(post, "images"):
        data["images"] = [
            {"id": img.id, "url": img.image.url, "caption": img.caption}
            for img in post.images.all()
        ]
    return data


# ── Generic CRUD for the four content types ──────────────────────────────────

CONTENT_MODELS = {
    "blogs": (BlogPost, True),
    "rfp-tips": (RfpTip, False),
    "compliance": (CompliancePost, False),
    "vendor-guides": (VendorGuide, False),
}

WRITABLE_FIELDS = (
    "title", "slug", "content", "author",
    "cta1_text", "cta1_url", "cta2_text", "cta2_url",
    "published",
)


def _get_model(kind: str):
    return CONTENT_MODELS.get(kind)


@csrf_exempt
def content_list(request, kind):
    if not _check_auth(request):
        return _unauth()
    entry = _get_model(kind)
    if not entry:
        return JsonResponse({"detail": "Unknown content type"}, status=404)
    Model, has_images = entry

    if request.method == "GET":
        qs = Model.objects.all().order_by("-created_at")
        return JsonResponse({
            "results": [_serialize_post(p, include_images=has_images) for p in qs]
        })

    if request.method == "POST":
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)
        post = Model()
        for f in WRITABLE_FIELDS:
            if f in payload:
                setattr(post, f, payload[f])
        if not post.slug and post.title:
            post.slug = slugify(post.title)[:240]
        try:
            post.save()
        except Exception as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        return JsonResponse(_serialize_post(post, include_images=has_images), status=201)

    return HttpResponseNotAllowed(["GET", "POST"])


@csrf_exempt
def content_detail(request, kind, pk):
    if not _check_auth(request):
        return _unauth()
    entry = _get_model(kind)
    if not entry:
        return JsonResponse({"detail": "Unknown content type"}, status=404)
    Model, has_images = entry
    try:
        post = Model.objects.get(pk=pk)
    except Model.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)

    if request.method == "GET":
        return JsonResponse(_serialize_post(post, include_images=has_images))

    if request.method in ("PUT", "PATCH"):
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)
        for f in WRITABLE_FIELDS:
            if f in payload:
                setattr(post, f, payload[f])
        try:
            post.save()
        except Exception as exc:
            return JsonResponse({"detail": str(exc)}, status=400)
        return JsonResponse(_serialize_post(post, include_images=has_images))

    if request.method == "DELETE":
        post.delete()
        return HttpResponse(status=204)

    return HttpResponseNotAllowed(["GET", "PUT", "PATCH", "DELETE"])


# ── Blog image upload ─────────────────────────────────────────────────────────

@csrf_exempt
def blog_images(request, pk):
    if not _check_auth(request):
        return _unauth()
    try:
        blog = BlogPost.objects.get(pk=pk)
    except BlogPost.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)

    if request.method == "POST":
        f = request.FILES.get("image")
        if not f:
            return JsonResponse({"detail": "image file is required (multipart/form-data)"}, status=400)
        caption = request.POST.get("caption") or None
        img = BlogImage.objects.create(blog_post=blog, image=f, caption=caption)
        return JsonResponse(
            {"id": img.id, "url": img.image.url, "caption": img.caption},
            status=201,
        )
    return HttpResponseNotAllowed(["POST"])


@csrf_exempt
def blog_image_detail(request, pk, image_id):
    if not _check_auth(request):
        return _unauth()
    try:
        img = BlogImage.objects.get(pk=image_id, blog_post_id=pk)
    except BlogImage.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)
    if request.method == "DELETE":
        img.delete()
        return HttpResponse(status=204)
    return HttpResponseNotAllowed(["DELETE"])


# ── Demo bookings (read + status update) ──────────────────────────────────────

def _serialize_booking(b):
    return {
        "id": str(b.id),
        "slot_id": b.slot_id,
        "slot_date": b.slot_date.isoformat() if b.slot_date else None,
        "start_time": b.start_time,
        "end_time": b.end_time,
        "customer_name": b.customer_name,
        "customer_email": b.customer_email,
        "customer_phone": b.customer_phone,
        "notes": b.notes,
        "status": b.status,
        "booking_token": b.booking_token,
        "source": b.source,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }


@csrf_exempt
def bookings_list(request):
    if not _check_auth(request):
        return _unauth()
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    qs = DemoBooking.objects.all().order_by("-created_at")[:500]
    return JsonResponse({"results": [_serialize_booking(b) for b in qs]})


@csrf_exempt
def bookings_detail(request, pk):
    if not _check_auth(request):
        return _unauth()
    try:
        booking = DemoBooking.objects.get(pk=pk)
    except DemoBooking.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)
    if request.method == "GET":
        return JsonResponse(_serialize_booking(booking))
    if request.method in ("PUT", "PATCH"):
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)
        if "status" in payload:
            booking.status = payload["status"]
        if "notes" in payload:
            booking.notes = payload["notes"]
        booking.updated_at = timezone.now()
        booking.save(update_fields=["status", "notes", "updated_at"])
        return JsonResponse(_serialize_booking(booking))
    return HttpResponseNotAllowed(["GET", "PUT", "PATCH"])


# ── Support tickets (read + status / reply) ───────────────────────────────────

def _serialize_ticket(t):
    return {
        "id": str(t.id),
        "ticket_id": t.ticket_id,
        "tracking_token": t.tracking_token,
        "name": t.name,
        "email": t.email,
        "category": t.category,
        "subject": t.subject,
        "message": t.message,
        "status": t.status,
        "priority": t.priority,
        "assigned_to": t.assigned_to,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _serialize_reply(r):
    return {
        "id": str(r.id),
        "ticket_id": r.ticket_id,
        "author": r.author,
        "author_type": r.author_type,
        "message": r.message,
        "is_internal": r.is_internal,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@csrf_exempt
def tickets_list(request):
    if not _check_auth(request):
        return _unauth()
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    qs = SupportTicket.objects.all().order_by("-created_at")[:500]
    return JsonResponse({"results": [_serialize_ticket(t) for t in qs]})


@csrf_exempt
def tickets_detail(request, pk):
    if not _check_auth(request):
        return _unauth()
    try:
        ticket = SupportTicket.objects.get(pk=pk)
    except SupportTicket.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)
    if request.method == "GET":
        replies = SupportTicketReply.objects.filter(ticket_id=ticket.ticket_id).order_by("created_at")
        return JsonResponse({
            **_serialize_ticket(ticket),
            "replies": [_serialize_reply(r) for r in replies],
        })
    if request.method in ("PUT", "PATCH"):
        try:
            payload = json.loads(request.body or "{}")
        except json.JSONDecodeError:
            return JsonResponse({"detail": "Invalid JSON"}, status=400)
        for f in ("status", "priority", "assigned_to"):
            if f in payload:
                setattr(ticket, f, payload[f])
        ticket.updated_at = timezone.now()
        ticket.save(update_fields=["status", "priority", "assigned_to", "updated_at"])
        return JsonResponse(_serialize_ticket(ticket))
    return HttpResponseNotAllowed(["GET", "PUT", "PATCH"])


@csrf_exempt
def tickets_replies(request, pk):
    if not _check_auth(request):
        return _unauth()
    try:
        ticket = SupportTicket.objects.get(pk=pk)
    except SupportTicket.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"detail": "Invalid JSON"}, status=400)
    message = (payload.get("message") or "").strip()
    if not message:
        return JsonResponse({"detail": "message is required"}, status=400)
    reply = SupportTicketReply.objects.create(
        id=uuid.uuid4(),
        ticket_id=ticket.ticket_id,
        author=payload.get("author") or "BOOPPA Support",
        author_type=payload.get("author_type") or "agent",
        message=message,
        is_internal=bool(payload.get("is_internal", False)),
    )
    return JsonResponse(_serialize_reply(reply), status=201)
