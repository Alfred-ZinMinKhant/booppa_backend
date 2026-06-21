"""Shared branded HTML email shell.

Wraps email body content in a consistent BOOPPA-branded frame: a dark header
band with the hosted logo, a white content card, and a muted footer. Reusable
across transactional + digest emails so they stop being bare unstyled HTML.

The logo is referenced by a public URL (emails can't read local files); it is
served from the Next.js `public/` folder at `<VERIFY_BASE_URL>/logo.png`.
"""
from __future__ import annotations

from app.core.config import settings

_BASE = (getattr(settings, "VERIFY_BASE_URL", "https://www.booppa.io") or "https://www.booppa.io").rstrip("/")
_LOGO_URL = f"{_BASE}/logo.png"


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
<html><body style="margin:0;padding:0;background:#eef1f5;">
  {pre}
  <div style="max-width:600px;margin:0 auto;font-family:Arial,Helvetica,sans-serif;color:#0f172a;">
    <div style="background:#0A0F1E;padding:18px 28px;border-radius:12px 12px 0 0;border-bottom:2px solid #00C9A7;">
      <img src="{_LOGO_URL}" alt="BOOPPA INTELLIGENCE" height="34"
           style="height:34px;display:block;border:0;outline:none;text-decoration:none;" />
      {header_line}
    </div>
    <div style="background:#ffffff;padding:30px 28px;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;">
      {body_html}
    </div>
    <div style="text-align:center;color:#94a3b8;font-size:11px;padding:14px 28px 28px;">
      BOOPPA Intelligence · Singapore · <a href="{_BASE}" style="color:#94a3b8;">booppa.io</a>
    </div>
  </div>
</body></html>"""
