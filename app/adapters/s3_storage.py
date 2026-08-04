import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


from app.ports.storage_port import StoragePort

class S3StorageAdapter(StoragePort):
    """AWS S3 service for file storage"""

    def __init__(self):
        client_kwargs = {
            "region_name": settings.AWS_REGION,
            "config": Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 3}),
        }
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            client_kwargs.update(
                {
                    "aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
                    "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
                }
            )

        self.s3_client = boto3.client("s3", **client_kwargs)
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

    # Image types the CMS accepts. Django's `ImageField` validated nothing at the
    # HTTP layer beyond Pillow being able to open the file, and the endpoint that
    # fed it was admin-only, so in practice anything was uploadable. Keep this
    # narrow: these three are what `next/image` can serve and what the blog uses.
    ALLOWED_IMAGE_TYPES = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    MAX_IMAGE_BYTES = 5 * 1024 * 1024

    def upload_image(self, image_bytes: bytes, key: str, content_type: str) -> str:
        """Store a public CMS image and return the object key it was written to.

        Deliberately returns the *key*, not a URL. Blog images are public content
        embedded in cached pages and `next/image`, so they cannot be served from
        a presigned URL — `upload_pdf`'s 7-day expiry would turn every post older
        than a week into a broken image. Callers persist the key and the read
        path (`/api/public/cms-media/…`) mints a fresh presign per request, which
        keeps the bucket private while the public URL stays stable forever.

        Raises `ValueError` for a disallowed content type or an oversized file;
        both are caller (admin upload) errors and should surface as a 400, not a
        500.
        """
        if content_type not in self.ALLOWED_IMAGE_TYPES:
            raise ValueError(
                f"Unsupported image type {content_type!r}; allowed: "
                + ", ".join(sorted(self.ALLOWED_IMAGE_TYPES))
            )
        if not image_bytes:
            raise ValueError("Empty image upload")
        if len(image_bytes) > self.MAX_IMAGE_BYTES:
            raise ValueError(
                f"Image is {len(image_bytes)} bytes; limit is {self.MAX_IMAGE_BYTES}"
            )

        try:
            self.s3_client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=image_bytes,
                ContentType=content_type,
                # Long max-age is safe because keys are uuid-suffixed and never
                # rewritten in place — an edited image gets a new key.
                CacheControl="public, max-age=31536000, immutable",
            )
        except ClientError as e:
            logger.error(f"S3 image upload failed for {key}: {e}")
            raise

        logger.info(f"CMS image uploaded: {key}")
        return key

    def presign_key(self, key: str, expires_in: int = 604800) -> str | None:
        """Presign a known object key. Unlike `refresh_url`, takes a key rather
        than parsing one out of a stale URL. Returns None on failure so callers
        can 404 instead of redirecting somewhere useless."""
        try:
            return self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except ClientError as e:
            logger.warning(f"Presign failed for {key}: {e}")
            return None

    def key_from_url(self, url: str) -> str | None:
        """Extract the S3 object key from a stored (possibly-expired) presigned
        URL. Returns None for non-S3 URLs (e.g. backend redirect routes) so
        callers can leave those untouched."""
        if not url or not isinstance(url, str):
            return None
        try:
            from urllib.parse import urlparse, unquote

            parsed = urlparse(url)
            host = parsed.netloc.lower()
            if "amazonaws.com" not in host and self.bucket not in host:
                return None
            path = unquote(parsed.path).lstrip("/")
            # Path-style URLs (s3.<region>.amazonaws.com/<bucket>/<key>) carry the
            # bucket as the first path segment; virtual-hosted style does not.
            if path.startswith(f"{self.bucket}/"):
                path = path[len(self.bucket) + 1:]
            return path or None
        except Exception:
            return None

    def refresh_url(self, url: str, expires_in: int = 604800) -> str:
        """Re-presign a stored S3 URL so links stay valid after the original
        presign (and its rotating STS credentials) expire. Non-S3 URLs and any
        that fail to parse/sign are returned unchanged."""
        key = self.key_from_url(url)
        if not key:
            return url
        try:
            return self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        except ClientError as e:
            logger.warning(f"Re-presign failed for {key}: {e}")
            return url

    async def delete_file(self, key: str) -> bool:
        """Delete file from S3"""
        try:
            self.s3_client.delete_object(Bucket=self.bucket, Key=key)
            logger.info(f"File deleted from S3: {key}")
            return True
        except ClientError as e:
            logger.error(f"S3 delete failed: {e}")
            return False
