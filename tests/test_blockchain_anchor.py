"""
Tests for blockchain anchoring fixes:
1. BlockchainService._hash_to_bytes32 validation
2. RFPExpressBuilder._anchor_to_blockchain uses SHA-256 of report_id, not the UUID directly
"""
import asyncio
import hashlib
import sys
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_web3():
    if "web3" in sys.modules:
        return
    mod = types.ModuleType("web3")

    class FakeWeb3:
        HTTPProvider = MagicMock()

        def __init__(self, provider=None):
            self.eth = MagicMock()

        @staticmethod
        def to_checksum_address(addr):
            return addr

        @staticmethod
        def to_bytes(hexstr=None):
            raw = hexstr[2:] if hexstr and hexstr.startswith("0x") else (hexstr or "")
            return bytes.fromhex(raw)

    mod.Web3 = FakeWeb3
    sys.modules["web3"] = mod


_mock_web3()


def _bare_svc():
    from app.services.blockchain import BlockchainService
    svc = BlockchainService.__new__(BlockchainService)
    svc.w3 = sys.modules["web3"].Web3()
    svc.contract = MagicMock()
    return svc


class TestHashToBytes32:
    def test_valid_sha256_hex_accepted(self):
        svc = _bare_svc()
        result = svc._hash_to_bytes32(hashlib.sha256(b"test").hexdigest())
        assert len(result) == 32

    def test_uuid_string_rejected(self):
        svc = _bare_svc()
        with pytest.raises(ValueError, match="64-char SHA-256 hex string"):
            svc._hash_to_bytes32(str(uuid.uuid4()))

    def test_empty_string_rejected(self):
        svc = _bare_svc()
        with pytest.raises(ValueError, match="64-char SHA-256 hex string"):
            svc._hash_to_bytes32("")

    def test_short_hex_rejected(self):
        svc = _bare_svc()
        with pytest.raises(ValueError, match="64-char SHA-256 hex string"):
            svc._hash_to_bytes32("deadbeef")

    def test_0x_prefix_accepted(self):
        svc = _bare_svc()
        result = svc._hash_to_bytes32("0x" + hashlib.sha256(b"test").hexdigest())
        assert len(result) == 32


class TestRFPAnchorUsesHashedReportId:
    def test_anchor_passes_sha256_not_uuid(self):
        report_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "rfp:cs_test_abc123"))
        expected = hashlib.sha256(report_id.encode()).hexdigest()
        captured = {}

        async def fake_anchor(evidence_hash, metadata=""):
            captured["evidence_hash"] = evidence_hash
            return "0x" + "c" * 64

        with patch("app.services.blockchain.BlockchainService") as MockBC:
            inst = MagicMock()
            inst.anchor_evidence = AsyncMock(side_effect=fake_anchor)
            MockBC.return_value = inst

            from app.services.rfp_express_builder import RFPExpressBuilder
            builder = RFPExpressBuilder.__new__(RFPExpressBuilder)
            builder.report_id = report_id
            builder.vendor_id = "vendor-123"
            builder.warnings = []

            tx = asyncio.run(builder._anchor_to_blockchain())

        assert tx == "0x" + "c" * 64
        assert captured["evidence_hash"] == expected
        assert captured["evidence_hash"] != report_id, "Raw UUID must never be passed"

    def test_anchor_failure_is_non_blocking(self):
        with patch("app.services.blockchain.BlockchainService") as MockBC:
            inst = MagicMock()
            inst.anchor_evidence = AsyncMock(side_effect=RuntimeError("connection refused"))
            MockBC.return_value = inst

            from app.services.rfp_express_builder import RFPExpressBuilder
            builder = RFPExpressBuilder.__new__(RFPExpressBuilder)
            builder.report_id = str(uuid.uuid4())
            builder.vendor_id = "vendor-456"
            builder.warnings = []

            tx = asyncio.run(builder._anchor_to_blockchain())

        assert tx is None
        assert any("Blockchain anchor skipped" in w for w in builder.warnings)


class TestContentBoundEvidenceHash:
    """The evidence hash PRINTED in the PDF must equal the hash ANCHORED on
    chain, and must be a real content-bound SHA-256 — not the raw UUID. This is
    what makes the document independently verifiable (forensic audit finding:
    'evidence hash is a UUID, not SHA-256')."""

    def _builder(self):
        from app.services.rfp_express_builder import RFPExpressBuilder
        b = RFPExpressBuilder.__new__(RFPExpressBuilder)
        b.report_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "rfp:cs_test_xyz"))
        b.vendor_id = "vendor-789"
        b.warnings = []
        return b

    def test_hash_is_64_hex_and_deterministic(self):
        b = self._builder()
        qa = {"encryption_standards": "AES-256", "data_residency": "Singapore"}
        h1 = b._compute_evidence_hash("Acme Pte Ltd", qa)
        h2 = b._compute_evidence_hash("Acme Pte Ltd", dict(reversed(list(qa.items()))))
        assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)
        assert h1 == h2, "key order must not change the hash"
        assert h1 != b.report_id, "must not be the raw UUID"

    def test_hash_changes_with_content(self):
        b = self._builder()
        base = b._compute_evidence_hash("Acme", {"q": "AES-256"})
        assert base != b._compute_evidence_hash("Acme", {"q": "AES-128"})
        assert base != b._compute_evidence_hash("Other Co", {"q": "AES-256"})

    def test_anchored_hash_equals_displayed_evidence_hash(self):
        captured = {}

        async def fake_anchor(evidence_hash, metadata=""):
            captured["evidence_hash"] = evidence_hash
            return "0x" + "d" * 64

        with patch("app.services.blockchain.BlockchainService") as MockBC:
            inst = MagicMock()
            inst.anchor_evidence = AsyncMock(side_effect=fake_anchor)
            MockBC.return_value = inst

            b = self._builder()
            # This is the value generate_express_package computes before anchoring
            # and the value _build_pdf renders as the EVIDENCE HASH.
            b.evidence_hash = b._compute_evidence_hash("Acme", {"q": "AES-256"})
            asyncio.run(b._anchor_to_blockchain())

        assert captured["evidence_hash"] == b.evidence_hash, \
            "anchored hash must equal the displayed evidence hash"
