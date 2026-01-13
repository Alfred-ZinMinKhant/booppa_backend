#!/usr/bin/env python3
"""Quick utility to validate AWS credentials used by the app.

Checks:
- STS get_caller_identity (validates credentials)
- S3 HeadBucket for `S3_BUCKET` (if set)
- SES get_send_quota (if SES configured)

Usage: run with environment variables set (or place them in a .env file).
    python3 scripts/test_aws_creds.py
"""
import os
import sys
import json

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

import boto3
from botocore.exceptions import ClientError, NoCredentialsError


def info(msg):
    print(msg)


def fail(msg):
    print("ERROR:", msg)
    sys.exit(2)


def main():
    aws_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    bucket = os.environ.get("S3_BUCKET")

    if not aws_key or not aws_secret:
        fail(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY must be set in the environment or .env"
        )

    info(f"Using AWS region: {region or '(not set)'}")

    # STS identity
    try:
        sts = boto3.client("sts", region_name=region)
        ident = sts.get_caller_identity()
        info("STS identity: " + json.dumps(ident))
    except NoCredentialsError:
        fail("No AWS credentials found")
    except ClientError as e:
        fail(f"STS call failed: {e}")

    # S3 bucket check
    if bucket:
        try:
            s3 = boto3.client("s3", region_name=region)
            s3.head_bucket(Bucket=bucket)
            info(f"S3 bucket reachable: {bucket}")
        except ClientError as e:
            info(f"S3 head_bucket error: {e}")
    else:
        info("S3_BUCKET not set; skipping bucket check")

    # SES quick check
    try:
        ses = boto3.client("ses", region_name=region)
        quota = ses.get_send_quota()
        info(f"SES send quota: {quota}")
    except ClientError as e:
        info(f"SES check failed (may be okay if you don't use SES): {e}")

    info("All checks completed")


if __name__ == "__main__":
    main()
