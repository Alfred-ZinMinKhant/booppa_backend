import boto3
from botocore.exceptions import ClientError
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class EmailService:
    """AWS SES email service for notifications"""

    def __init__(self):
        client_kwargs = {
            "region_name": settings.AWS_SES_REGION,
        }
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            client_kwargs.update(
                {
                    "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
                    "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
                }
            )

        self.ses_client = boto3.client("ses", **client_kwargs)

    async def send_report_ready_email(self, to_email: str, report_url: str, user_name: str, report_id: str):
        """Send email notification when report is ready"""
        try:
            subject = f"BOOPPA Audit Report Ready - {report_id}"

            body_html = f"""
            <html>
            <head></head>
            <body>
                <h2>Your Audit Report is Ready</h2>
                <p>Hello {user_name},</p>
                <p>Your compliance audit report has been generated and is ready for download.</p>
                <p><strong>Report ID:</strong> {report_id}</p>
                <p><a href="{report_url}" style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Download Report</a></p>
                <p>The report includes blockchain-verified evidence for auditor review.</p>
                <p>Thank you for using BOOPPA.</p>
            </body>
            </html>
            """

            response = self.ses_client.send_email(
                Source=settings.SUPPORT_EMAIL,
                Destination={'ToAddresses': [to_email]},
                Message={
                    'Subject': {'Data': subject},
                    'Body': {'Html': {'Data': body_html}}
                }
            )

            logger.info(f"Report ready email sent to {to_email}: {response['MessageId']}")
            return True

        except ClientError as e:
            logger.error(f"Email sending failed: {e}")
            return False
