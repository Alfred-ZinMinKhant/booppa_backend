import base64
import logging
import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)

# Provider attachment caps: Resend 25 MB, SES 40 MB. Stay conservatively below
# the smaller limit so a payload never silently bounces.
_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

# Type alias for attachments: list of (filename, raw_bytes) tuples.
Attachment = tuple[str, bytes]


def _filter_attachments(attachments: list[Attachment] | None) -> list[Attachment]:
    """Drop empty/oversize attachments; log when something is skipped."""
    if not attachments:
        return []
    kept: list[Attachment] = []
    total = 0
    for item in attachments:
        try:
            filename, data = item
        except (TypeError, ValueError):
            logger.warning("[Email] Ignoring malformed attachment entry: %r", item)
            continue
        if not data:
            logger.warning("[Email] Ignoring empty attachment %s", filename)
            continue
        total += len(data)
        if total > _MAX_ATTACHMENT_BYTES:
            logger.warning(
                "[Email] Skipping attachment %s — total payload exceeds %d bytes",
                filename, _MAX_ATTACHMENT_BYTES,
            )
            continue
        kept.append((filename, data))
    return kept


from app.ports.email_port import EmailPort

# Inline (CID) logo support. Branded emails reference ``cid:booppa-logo`` in the
# header; when that marker is present we attach the bundled email logo as an
# inline image so the client renders it without proxying a remote URL.
_INLINE_LOGO_CID = "booppa-logo"
_INLINE_LOGO_FILENAME = "booppa-logo.png"


def _load_inline_logo() -> bytes | None:
    """Return the bundled email logo bytes, or ``None`` if unavailable.

    Cached on the function object so the file is read from disk only once.
    """
    cached = getattr(_load_inline_logo, "_bytes", False)
    if cached is not False:
        return cached
    data: bytes | None = None
    try:
        import os

        here = os.path.dirname(os.path.dirname(__file__))  # app/
        candidates = [
            os.path.join(here, "..", "static", "email_logo.png"),
            "/app/static/email_logo.png",
            os.path.join(here, "..", "static", "logo.png"),
            "/app/static/logo.png",
        ]
        for path in candidates:
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    data = fh.read()
                break
    except Exception as e:  # never let a logo problem break email delivery
        logger.warning("[Email] Could not load inline logo: %s", e)
        data = None
    _load_inline_logo._bytes = data  # type: ignore[attr-defined]
    return data


class ResendEmailAdapter(EmailPort):
    """Email service — uses Resend if RESEND_API_KEY is set, falls back to AWS SES."""

    async def send_html_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachments: list[Attachment] | None = None,
        category: str = "transactional",
        list_unsubscribe: bool | None = None,
    ) -> bool:
        """Send an HTML email.

        ``category`` is "transactional" (default) or "marketing"; recurring
        digests / marketing should pass "marketing" so a one-click unsubscribe
        actually stops them. ``list_unsubscribe`` adds one-click unsubscribe
        headers — it defaults to True for marketing sends. A suppressed
        recipient is skipped and reported as success (there is nothing to
        retry and no fulfillment failure to alert on).
        """
        if getattr(settings, "SKIP_EMAIL", False):
            logger.info(f"[Email] Skipped (SKIP_EMAIL=True): to={to_email} subject={subject}")
            return True

        # Suppression gate — bounces/complaints (scope=all) block everything;
        # unsubscribes (scope=marketing) block only marketing sends.
        try:
            from app.services.email_suppression import is_suppressed
            if is_suppressed(to_email, category):
                logger.info(
                    "[Email] Suppressed (%s): to=%s subject=%s", category, to_email, subject
                )
                return True
        except Exception as e:  # never let the gate break delivery
            logger.warning("[Email] Suppression gate error (sending anyway): %s", e)

        headers: dict[str, str] | None = None
        want_unsub = list_unsubscribe if list_unsubscribe is not None else (category == "marketing")
        if want_unsub:
            try:
                from app.services.email_suppression import list_unsubscribe_headers
                headers = list_unsubscribe_headers(to_email)
            except Exception as e:
                logger.warning("[Email] Could not build unsubscribe headers: %s", e)

        attachments = _filter_attachments(attachments)
        if getattr(settings, "RESEND_API_KEY", None):
            return await self._send_resend(to_email, subject, body_html, attachments, headers)
        return await self._send_ses(to_email, subject, body_html, attachments, headers)

    # ── Resend ────────────────────────────────────────────────────────────────

    async def _send_resend(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachments: list[Attachment] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bool:
        try:
            payload = {
                "from": f"BOOPPA <{settings.SUPPORT_EMAIL}>",
                "to": [to_email],
                "subject": subject,
                "html": body_html,
            }
            if headers:
                payload["headers"] = headers
            payload_attachments = [
                {"filename": fn, "content": base64.b64encode(data).decode("ascii")}
                for fn, data in (attachments or [])
            ]
            logo = _load_inline_logo() if f"cid:{_INLINE_LOGO_CID}" in body_html else None
            if logo:
                payload_attachments.append({
                    "filename": _INLINE_LOGO_FILENAME,
                    "content": base64.b64encode(logo).decode("ascii"),
                    "content_type": "image/png",
                    "content_id": _INLINE_LOGO_CID,
                })
            if payload_attachments:
                payload["attachments"] = payload_attachments
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code in (200, 201):
                logger.info(f"[Resend] Email sent to {to_email}: {resp.json().get('id')}")
                return True
            logger.error(f"[Resend] Failed ({resp.status_code}): {resp.text}")
            return False
        except Exception as e:
            logger.error(f"[Resend] Exception sending to {to_email}: {e}")
            return False

    # ── AWS SES ───────────────────────────────────────────────────────────────

    async def _send_ses(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachments: list[Attachment] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bool:
        try:
            import boto3

            client_kwargs = {"region_name": settings.AWS_SES_REGION}
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                client_kwargs.update(
                    {
                        "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
                        "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
                    }
                )
            ses = boto3.client("ses", **client_kwargs)

            inline_logo = (
                _load_inline_logo() if f"cid:{_INLINE_LOGO_CID}" in body_html else None
            )
            # Custom headers (List-Unsubscribe) require the raw MIME path.
            if attachments or inline_logo or headers:
                raw = self._build_raw_mime(
                    to_email, subject, body_html, attachments or [], inline_logo, headers
                )
                response = ses.send_raw_email(
                    Source=settings.SUPPORT_EMAIL,
                    Destinations=[to_email],
                    RawMessage={"Data": raw},
                )
            else:
                response = ses.send_email(
                    Source=settings.SUPPORT_EMAIL,
                    Destination={"ToAddresses": [to_email]},
                    Message={
                        "Subject": {"Data": subject},
                        "Body": {"Html": {"Data": body_html}},
                    },
                )
            logger.info(f"[SES] Email sent to {to_email}: {response['MessageId']}")
            return True
        except Exception as e:
            logger.error(f"[SES] Failed sending to {to_email}: {e}")
            return False

    @staticmethod
    def _build_raw_mime(
        to_email: str,
        subject: str,
        body_html: str,
        attachments: list[Attachment],
        inline_logo: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Build a multipart MIME message with attachments (and optional inline logo) for SES.

        When ``inline_logo`` is given, the HTML and image are wrapped in a
        ``multipart/related`` part so the ``cid:booppa-logo`` reference resolves.
        ``headers`` adds extra top-level headers (e.g. List-Unsubscribe).
        """
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.application import MIMEApplication
        from email.mime.image import MIMEImage

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = f"BOOPPA <{settings.SUPPORT_EMAIL}>"
        msg["To"] = to_email
        for hk, hv in (headers or {}).items():
            msg[hk] = hv

        if inline_logo:
            related = MIMEMultipart("related")
            related.attach(MIMEText(body_html, "html"))
            img = MIMEImage(inline_logo, _subtype="png")
            img.add_header("Content-ID", f"<{_INLINE_LOGO_CID}>")
            img.add_header("Content-Disposition", "inline", filename=_INLINE_LOGO_FILENAME)
            related.attach(img)
            msg.attach(related)
        else:
            msg.attach(MIMEText(body_html, "html"))

        for filename, data in attachments:
            subtype = "pdf" if filename.lower().endswith(".pdf") else "octet-stream"
            part = MIMEApplication(data, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
        return msg.as_bytes()

    # ── Convenience wrapper ───────────────────────────────────────────────────

    async def send_report_ready_email(
        self, to_email: str, report_url: str | None, user_name: str, report_id: str
    ) -> bool:
        # Imported at call time to avoid an import cycle (email_layout may import
        # adapters indirectly).
        from app.services.email_layout import branded_email_html, email_button
        download_section = (
            email_button(report_url, "Download Report")
            if report_url
            else '<p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;">Your report is ready on the BOOPPA website. Please return to your report page to view it.</p>'
        )
        body_html = branded_email_html(
            f"""
            <h2 style="margin:0 0 16px;font-size:20px;color:#0f172a;">Your Audit Report is Ready</h2>
            <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Hello {user_name},</p>
            <p style="margin:0 0 12px;color:#334155;font-size:15px;line-height:1.6;">Your compliance audit report has been generated and is ready for download.</p>
            <p style="margin:0 0 16px;color:#334155;font-size:15px;line-height:1.6;"><strong>Report ID:</strong> {report_id}</p>
            {download_section}
            <p style="margin:16px 0 0;color:#334155;font-size:15px;line-height:1.6;">Thank you for using BOOPPA.</p>
            """,
            title="Your Audit Report is Ready",
            preheader=f"Report {report_id} is ready for download.",
        )
        return await self.send_html_email(
            to_email, f"BOOPPA Audit Report Ready - {report_id}", body_html
        )

    async def send_monitor_report_email(
        self,
        to_email: str,
        company_name: str,
        month_label: str,
        body_html: str,
        pdf_s3_key: str | None = None,
        report_url: str | None = None,
    ) -> bool:
        """
        Send the PDPA Monitor monthly report email with the PDF attached.

        PDF is fetched from S3 using pdf_s3_key (preferred) and attached
        directly to the email. Falls back to link-only if S3 fetch fails.
        """
        from datetime import datetime

        attachments: list[Attachment] = []
        if pdf_s3_key:
            try:
                from app.services.storage import S3Service

                s3 = S3Service()
                pdf_bytes = s3.s3_client.get_object(
                    Bucket=s3.bucket, Key=pdf_s3_key
                )["Body"].read()
                filename = f"PDPA_Monitor_Report_{datetime.now().strftime('%Y%m%d')}.pdf"
                attachments = [(filename, pdf_bytes)]
                logger.info(
                    "[MonitorReport] Attaching PDF (%d bytes) to email for %s",
                    len(pdf_bytes), to_email,
                )
            except Exception as e:
                # Non-fatal: degrade gracefully to link-only email
                logger.warning(
                    "[MonitorReport] Could not fetch PDF from S3 key=%s: %s. "
                    "Sending link-only email.", pdf_s3_key, e,
                )
                attachments = []

        return await self.send_html_email(
            to_email=to_email,
            subject=f"Your PDPA Monitor Report — {month_label}",
            body_html=body_html,
            attachments=attachments or None,
        )

    async def send_with_pdf_attachment(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        pdf_bytes: bytes,
        filename: str,
    ) -> bool:
        """
        Generic helper: send an email with a PDF attached directly from bytes.
        Avoids an S3 round-trip when the caller already has the PDF in memory.
        """
        return await self.send_html_email(
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            attachments=[(filename, pdf_bytes)],
        )
