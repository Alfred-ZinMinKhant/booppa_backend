import pytest

from app.orchestrator.engine import run, run_many


@pytest.mark.asyncio
async def test_run_produces_report():
    report = await run("https://example.com")
    assert "scan" in report
    assert "notary_hash" in report


@pytest.mark.asyncio
async def test_run_many_concurrency():
    urls = ["https://example.com", "https://example.org"]
    results = await run_many(urls, concurrency=2)
    assert len(results) == 2
