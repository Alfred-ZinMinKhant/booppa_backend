"""AWS SNS endpoint for SES bounce + complaint notifications.

Wire an SES configuration set (or the domain's default) to publish Bounce and
Complaint events to an SNS topic, then subscribe this endpoint to that topic:

    https://<backend-public-origin>/api/email/sns

On the first delivery SNS sends a ``SubscriptionConfirmation``; we fetch the
``SubscribeURL`` to confirm. Thereafter ``Notification`` messages carry a
bounce or complaint payload, and we add the offending recipients to the
suppression list (``scope="all"``) so we never email them again.

Only permanent bounces are suppressed hard; transient bounces are logged but
left deliverable (they may succeed on the next attempt).
"""
import json
import logging

import httpx
from fastapi import APIRouter, Request

from app.services.email_suppression import add_suppression

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/sns")
async def sns_notification(request: Request):
    raw = await request.body()
    try:
        envelope = json.loads(raw or b"{}")
    except Exception:
        logger.warning("[SNS] Unparseable body")
        return {"ok": False}

    msg_type = envelope.get("Type") or request.headers.get("x-amz-sns-message-type")

    # 1. Confirm the subscription on first contact.
    if msg_type == "SubscriptionConfirmation":
        sub_url = envelope.get("SubscribeURL")
        if sub_url:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    await client.get(sub_url)
                logger.info("[SNS] Subscription confirmed")
            except Exception as e:
                logger.error("[SNS] Failed to confirm subscription: %s", e)
        return {"ok": True}

    if msg_type != "Notification":
        return {"ok": True}

    # 2. Parse the SES event carried in Message.
    try:
        message = json.loads(envelope.get("Message") or "{}")
    except Exception:
        logger.warning("[SNS] Notification Message not JSON")
        return {"ok": True}

    notif_type = message.get("notificationType") or message.get("eventType")
    suppressed = 0

    if notif_type == "Bounce":
        bounce = message.get("bounce", {})
        if bounce.get("bounceType") == "Permanent":
            for r in bounce.get("bouncedRecipients", []):
                if add_suppression(
                    r.get("emailAddress", ""),
                    scope="all",
                    source="bounce",
                    reason=f"{bounce.get('bounceType')}/{bounce.get('bounceSubType')}",
                ):
                    suppressed += 1
        else:
            logger.info("[SNS] Transient bounce ignored (%s)", bounce.get("bounceSubType"))

    elif notif_type == "Complaint":
        complaint = message.get("complaint", {})
        for r in complaint.get("complainedRecipients", []):
            if add_suppression(
                r.get("emailAddress", ""),
                scope="all",
                source="complaint",
                reason=complaint.get("complaintFeedbackType") or "complaint",
            ):
                suppressed += 1

    logger.info("[SNS] %s processed, %d suppressed", notif_type, suppressed)
    return {"ok": True, "suppressed": suppressed}
