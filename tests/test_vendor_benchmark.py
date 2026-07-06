"""Phase 5B: Vendor Proof Trust Score sector benchmark.

Two layers: the pure percentile maths (`benchmark_stats`) and the certificate
rendering that surfaces (or honestly omits) the benchmark line.
"""
import io

import pypdf

from app.services.vendor_benchmark import benchmark_stats, MIN_TOTAL_PEERS
from app.services.vendor_proof_generator import generate_vendor_proof_certificate


def _text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _gen(**over) -> bytes:
    kwargs = dict(
        company_name="Acme Pte Ltd",
        uen="201812345A",
        acra_data={"matched": True, "entity_type": "Private Limited"},
        score=70,
        expires_on="06 July 2027",
    )
    kwargs.update(over)
    return generate_vendor_proof_certificate(**kwargs)


# ── pure stats ────────────────────────────────────────────────────────────────

def test_benchmark_percentile_and_average():
    # my_score 70 vs peers [40,50,60,80,90]: at-or-above 3 of 5 = 60%; avg 64.
    stats = benchmark_stats(70, [40, 50, 60, 80, 90], "IT Services", basis="sector")
    assert stats["percentile"] == 60
    assert stats["sector_avg"] == 64
    assert stats["peer_count"] == 5
    assert stats["basis"] == "sector"


def test_benchmark_returns_none_when_too_few_peers():
    thin = [80] * (MIN_TOTAL_PEERS - 1)
    assert benchmark_stats(70, thin, "IT Services", basis="sector") is None


def test_benchmark_ignores_none_scores():
    stats = benchmark_stats(70, [40, None, 60, None, 80, 90], "IT", basis="sector")
    assert stats["peer_count"] == 4  # Nones dropped


# ── certificate rendering ────────────────────────────────────────────────────

def test_cert_renders_sector_benchmark_line():
    bench = {"sector": "IT Services", "basis": "sector",
             "peer_count": 5, "sector_avg": 64, "percentile": 60}
    txt = _text(_gen(sector_benchmark=bench))
    assert "Sector benchmark" in txt
    assert "60%" in txt
    assert "IT Services" in txt


def test_cert_fallback_basis_states_all_vendors():
    bench = {"sector": "Rare Trade", "basis": "all_vendors",
             "peer_count": 40, "sector_avg": 55, "percentile": 72}
    txt = _text(_gen(sector_benchmark=bench))
    assert "all Booppa-scanned vendors" in txt


def test_cert_omits_benchmark_when_absent():
    txt = _text(_gen(sector_benchmark=None))
    assert "Sector benchmark" not in txt
