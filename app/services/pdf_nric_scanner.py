"""
Linked-PDF NRIC Scanner
=======================
Discovers PDFs linked from a page (procurement packs, application forms,
brochures), fetches a bounded sample, extracts text, and harvests NRIC
candidates for the classifier.

Bounded by design:
  - MAX_PDFS pdfs per site
  - MAX_PDF_BYTES per file
  - TOTAL_BUDGET_SECONDS across the whole scan
A PDF that violates any budget is skipped, never partially parsed.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

MAX_PDFS = 5
MAX_PDF_BYTES = 5 * 1024 * 1024  # 5 MB per file
TOTAL_BUDGET_SECONDS = 15.0
PER_REQUEST_TIMEOUT = 8.0

_PDF_HREF_RE = re.compile(
    r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    re.IGNORECASE,
)


def discover_pdf_links(html: str, base_url: str) -> list[str]:
    """Extract up to MAX_PDFS unique absolute PDF URLs from an HTML page."""
    if not html or not base_url:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _PDF_HREF_RE.finditer(html):
        href = m.group(1).strip()
        abs_url = href if href.lower().startswith(("http://", "https://")) else urljoin(base_url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        out.append(abs_url)
        if len(out) >= MAX_PDFS:
            break
    return out


async def fetch_pdf(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    """Fetch a PDF with size and timeout caps. Returns bytes or None."""
    try:
        # HEAD first to check size when the server reports it
        try:
            head = await client.head(url, timeout=PER_REQUEST_TIMEOUT, follow_redirects=True)
            cl = head.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > MAX_PDF_BYTES:
                logger.info("Skipping PDF %s: content-length %s > cap", url, cl)
                return None
        except Exception:
            pass  # HEAD often unsupported; fall through to GET

        resp = await client.get(url, timeout=PER_REQUEST_TIMEOUT, follow_redirects=True)
        if resp.status_code >= 400:
            return None
        if len(resp.content) > MAX_PDF_BYTES:
            logger.info("Skipping PDF %s: body %d bytes > cap", url, len(resp.content))
            return None
        if not resp.content.startswith(b"%PDF"):
            return None
        return resp.content
    except Exception as e:
        logger.info("PDF fetch failed for %s: %s", url, e)
        return None


def extract_text(pdf_bytes: bytes, max_pages: int = 30) -> str:
    """Extract text from a PDF using pypdf. Returns '' on failure."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf not installed; cannot extract PDF text")
        return ""

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        chunks: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= max_pages:
                break
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(chunks)
    except Exception as e:
        logger.info("PDF parse failed: %s", e)
        return ""


async def scan_linked_pdfs(html: str, base_url: str) -> list[dict]:
    """Top-level entrypoint.

    Returns a list of {url, text} for each PDF that was successfully fetched
    and parsed, ready for harvest_candidates() in nric_classifier.
    """
    pdf_urls = discover_pdf_links(html, base_url)
    if not pdf_urls:
        return []

    deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
    results: list[dict] = []

    async with httpx.AsyncClient(
        headers={"User-Agent": "BooppaComplianceBot/1.0"},
    ) as client:
        for url in pdf_urls:
            if time.monotonic() >= deadline:
                logger.info("PDF budget exhausted; %d PDFs not scanned", len(pdf_urls) - len(results))
                break

            remaining = max(0.5, deadline - time.monotonic())
            try:
                pdf_bytes = await asyncio.wait_for(fetch_pdf(client, url), timeout=remaining)
            except asyncio.TimeoutError:
                logger.info("PDF fetch timed out per budget for %s", url)
                continue
            if not pdf_bytes:
                continue

            text = extract_text(pdf_bytes)
            if text:
                results.append({"url": url, "text": text})

    return results
