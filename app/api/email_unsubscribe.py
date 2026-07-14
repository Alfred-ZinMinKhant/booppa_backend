"""One-click List-Unsubscribe endpoint.

Recurring/marketing emails carry ``List-Unsubscribe`` + ``List-Unsubscribe-Post``
headers pointing here. The token is a stateless HMAC of the recipient address
(see ``app.services.email_suppression``), so no login is required.

- ``POST /api/email/unsubscribe`` — RFC 8058 one-click. Mail clients hit this
  automatically when the user clicks "unsubscribe"; suppresses marketing sends.
- ``GET  /api/email/unsubscribe?token=…`` — human-facing confirmation page;
  also suppresses so a plain link click works.

Scope is ``marketing`` only: transactional receipts (payment, kit delivery)
keep flowing even after an unsubscribe.
"""
import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.services.email_suppression import add_suppression, verify_unsubscribe_token

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unsubscribed</title></head>
<body style="font-family:system-ui,Arial,sans-serif;max-width:520px;margin:64px auto;padding:0 20px;color:#0f172a;">
<h2>{heading}</h2><p style="color:#334155;line-height:1.6;">{body}</p>
</body></html>"""


def _apply(token: str) -> bool:
    email = verify_unsubscribe_token(token)
    if not email:
        return False
    add_suppression(email, scope="marketing", source="unsubscribe", reason="one-click")
    return True


@router.post("/unsubscribe")
async def unsubscribe_oneclick(token: str = Query(default="")):
    # RFC 8058 one-click: mail clients POST here with a
    # `List-Unsubscribe=One-Click` form body, which we don't need to read.
    ok = _apply(token)
    logger.info("[Unsubscribe] one-click POST ok=%s", ok)
    return {"ok": ok}


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_landing(token: str = Query(default="")):
    if _apply(token):
        return HTMLResponse(
            _PAGE.format(
                heading="You're unsubscribed",
                body="You will no longer receive recurring or marketing emails from BOOPPA. "
                "Transactional messages (payment receipts, document delivery) will still be sent.",
            )
        )
    return HTMLResponse(
        _PAGE.format(
            heading="Link expired or invalid",
            body="We couldn't process this unsubscribe request. Please contact support if you continue to receive unwanted email.",
        ),
        status_code=400,
    )
