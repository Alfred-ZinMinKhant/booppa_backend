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
