import boto3
from botocore.exceptions import ClientError
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


class S3Service:
    """AWS S3 service for file storage"""

    def __init__(self):
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION,
        )
        self.bucket = settings.S3_BUCKET

    async def upload_pdf(self, pdf_bytes: bytes, report_id: str) -> str:
        """Upload PDF to S3 and return URL"""
        try:
            key = f"reports/{report_id}.pdf"

            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=pdf_bytes,
                ContentType="application/pdf",
                Metadata={"report-id": report_id, "uploaded-by": "booppa-v10"},
            )

            # Generate presigned URL (valid for 7 days)
            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=604800,  # 7 days
            )

            logger.info(f"PDF uploaded successfully: {key}")
            return url

        except ClientError as e:
            logger.error(f"S3 upload failed: {e}")
            raise

    async def delete_file(self, key: str) -> bool:
        """Delete file from S3"""
        try:
            self.s3_client.delete_object(Bucket=self.bucket, Key=key)
            logger.info(f"File deleted from S3: {key}")
            return True
        except ClientError as e:
            logger.error(f"S3 delete failed: {e}")
            return False
