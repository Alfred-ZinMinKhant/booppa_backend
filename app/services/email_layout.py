"""Shared branded HTML email shell + reusable UI components.

Wraps email body content in a consistent BOOPPA-branded frame: a dark header
band with the hosted logo, a white content card, and a muted footer. Reusable
across transactional + digest emails so they stop being bare unstyled HTML.

The logo is referenced by a public URL (emails can't read local files); it is
served from the Next.js `public/` folder at `<VERIFY_BASE_URL>/logo.png`.

Component helpers (``email_button``, ``email_download_card``, ``email_info_box``,
``email_kv``) render email-client-safe markup — inline styles, table-based
layout, no external CSS — so every template shares one button/box/spacing system.
Compose inner content from these, then pass it to :func:`branded_email_html`.
"""
from __future__ import annotations

from typing import Iterable, Sequence, Tuple

from app.core.config import settings

_BASE = (getattr(settings, "VERIFY_BASE_URL", "https://www.booppa.io") or "https://www.booppa.io").rstrip("/")
# The header logo is embedded as an inline (CID) attachment rather than a remote
# URL: Gmail/Outlook proxy remote images through their own fetchers, which get
# bot-challenged by Cloudflare in front of booppa.io and cached as broken. A
# ``cid:`` reference is delivered inside the message and never leaves the client.
# ``EmailService.send_html_email`` detects this marker and attaches the bundled
# ``static/email_logo.png`` automatically (see app/adapters/resend_email.py).
EMAIL_LOGO_CID = "booppa-logo"
_LOGO_URL = f"cid:{EMAIL_LOGO_CID}"

# Brand palette (kept here so every email pulls from one source).
_TEAL = "#10b981"
_NAVY = "#0f172a"
_HEADER_BG = "#0A0F1E"
_INK = "#0f172a"
_MUTED = "#64748b"
_BORDER = "#e2e8f0"


def branded_email_html(body_html: str, *, title: str = "", preheader: str = "") -> str:
    """Return a full HTML document wrapping ``body_html`` in the brand frame.

    ``body_html`` is the inner content (already-escaped where needed). ``title``
    renders as the teal header line; ``preheader`` is the hidden inbox preview.
    """
    pre = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>'
        if preheader else ""
    )
    header_line = (
        f'<div style="color:#9fb0c3;font-size:13px;font-weight:600;margin-top:6px;">{title}</div>'
        if title else ""
    )
    return f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
</head>
<body style="margin:0;padding:0;background:#eef1f5;">
  {pre}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;">
    <tr><td align="center" style="padding:24px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;color:{_INK};">
        <tr><td style="background:{_HEADER_BG};padding:18px 28px;border-radius:12px 12px 0 0;border-bottom:2px solid #00C9A7;">
          <img src="{_LOGO_URL}" alt="BOOPPA INTELLIGENCE" height="34"
               style="height:34px;display:block;border:0;outline:none;text-decoration:none;" />
          {header_line}
        </td></tr>
        <tr><td style="background:#ffffff;padding:30px 28px;border:1px solid {_BORDER};border-top:none;border-radius:0 0 12px 12px;">
          {body_html}
        </td></tr>
        <tr><td style="text-align:center;color:#94a3b8;font-size:11px;padding:14px 28px 28px;">
          BOOPPA Intelligence · Singapore · <a href="{_BASE}" style="color:#94a3b8;">booppa.io</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def email_button(url: str, label: str, *, primary: bool = True) -> str:
    """A full-width, block-level button. Teal for primary, navy for secondary.

    Full-width (``display:block``) so stacked buttons align to a uniform edge
    instead of each shrinking to fit its own label.
    """
    bg = _TEAL if primary else _NAVY
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 16px;"><tr><td>'
        f'<a href="{url}" style="display:block;background:{bg};color:#ffffff;'
        f'padding:15px 20px;text-align:center;text-decoration:none;border-radius:8px;'
        f'font-weight:bold;font-size:15px;">{label}</a>'
        f'</td></tr></table>'
    )


def email_download_card(url: str, label: str, description: str = "", *, primary: bool = False) -> str:
    """A download button with a muted caption below it, evenly spaced."""
    caption = (
        f'<div style="color:{_MUTED};font-size:13px;line-height:1.5;margin:-8px 4px 22px;">'
        f'{description}</div>'
        if description else ""
    )
    # email_button already carries 16px bottom margin; caption adds the rest of
    # the rhythm (and pulls up over the button's margin when present).
    return email_button(url, label, primary=primary) + caption


_INFO_TONES = {
    "neutral": ("#f8fafc", "#94a3b8", "#334155"),
    "success": ("#f0fdf4", _TEAL, "#166534"),
    "warn": ("#fffbeb", "#f59e0b", "#92400e"),
}


def email_info_box(inner_html: str, *, tone: str = "neutral") -> str:
    """A callout box with a colored left border. tone: neutral | success | warn."""
    bg, border, ink = _INFO_TONES.get(tone, _INFO_TONES["neutral"])
    return (
        f'<div style="background:{bg};border-left:3px solid {border};'
        f'border-radius:4px;padding:12px 16px;margin:0 0 20px;'
        f'color:{ink};font-size:13px;line-height:1.6;">{inner_html}</div>'
    )


def email_kv(rows: Sequence[Tuple[str, str]] | Iterable[Tuple[str, str]], *, tone: str = "success") -> str:
    """A compact label/value fact block (Score / Report ID / Generated …).

    Rendered inside an :func:`email_info_box` so it inherits the callout styling.
    """
    lines = "<br>".join(
        f'<strong>{label}:</strong> {value}' for label, value in rows
    )
    return email_info_box(lines, tone=tone)
