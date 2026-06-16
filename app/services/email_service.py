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


class EmailService:
    """Email service — uses Resend if RESEND_API_KEY is set, falls back to AWS SES."""

    async def send_html_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachments: list[Attachment] | None = None,
    ) -> bool:
        if getattr(settings, "SKIP_EMAIL", False):
            logger.info(f"[Email] Skipped (SKIP_EMAIL=True): to={to_email} subject={subject}")
            return True
        attachments = _filter_attachments(attachments)
        if getattr(settings, "RESEND_API_KEY", None):
            return await self._send_resend(to_email, subject, body_html, attachments)
        return await self._send_ses(to_email, subject, body_html, attachments)

    # ── Resend ────────────────────────────────────────────────────────────────

    async def _send_resend(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        attachments: list[Attachment] | None = None,
    ) -> bool:
        try:
            payload = {
                "from": f"BOOPPA <{settings.SUPPORT_EMAIL}>",
                "to": [to_email],
                "subject": subject,
                "html": body_html,
            }
            if attachments:
                payload["attachments"] = [
                    {"filename": fn, "content": base64.b64encode(data).decode("ascii")}
                    for fn, data in attachments
                ]
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

            if attachments:
                raw = self._build_raw_mime(to_email, subject, body_html, attachments)
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
    ) -> bytes:
        """Build a multipart MIME message with PDF (or other) attachments for SES."""
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.application import MIMEApplication

        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = f"BOOPPA <{settings.SUPPORT_EMAIL}>"
        msg["To"] = to_email
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
        download_section = (
            f'<p><a href="{report_url}" style="background-color:#4CAF50;color:#fff;'
            f'padding:10px 20px;text-decoration:none;border-radius:5px;">Download Report</a></p>'
            if report_url
            else "<p>Your report is ready on the BOOPPA website. Please return to your report page to view it.</p>"
        )
        body_html = f"""
        <html><body>
            <h2>Your Audit Report is Ready</h2>
            <p>Hello {user_name},</p>
            <p>Your compliance audit report has been generated and is ready for download.</p>
            <p><strong>Report ID:</strong> {report_id}</p>
            {download_section}
            <p>Thank you for using BOOPPA.</p>
        </body></html>
        """
        return await self.send_html_email(
            to_email, f"BOOPPA Audit Report Ready - {report_id}", body_html
        )
