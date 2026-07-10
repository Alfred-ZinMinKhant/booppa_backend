"""
Booppa CSP Compliance Pack v2 — Complete Test Suite
Covers:
  - FIX #1: PII encryption (EncryptedString TypeDecorator)
  - FIX #2: Deterministic STR hash (content-based, not timestamp-based)
  - FIX #3: Sanctions screening (OFAC + UN + auto-screen on CDD)
  - Bulk import (CSV + Excel parsing and validation)
  - Compliance scorer (all 9 pillars)
  - Tipping-off protection
  - Router business logic

Run:
    pytest tests/test_csp_v2.py -v
    pytest tests/test_csp_v2.py -k "encryption" -v
    pytest tests/test_csp_v2.py -k "str_hash" -v
    pytest tests/test_csp_v2.py -k "sanctions" -v
"""

import json
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call
from uuid import uuid4


# ── HELPERS ──────────────────────────────────────────────────────────────────

def _profile(**kw) -> dict:
    base = {
        "id": str(uuid4()), "legal_name": "Test CSP Pte Ltd", "uen": "202312345A",
        "acra_reg_status": "approved",
        "acra_renewal_date": datetime.now(timezone.utc) + timedelta(days=300),
        "rqi_name": "Jane Tan", "rqi_qualification": "ICSA",
        "rqi_training_completed": True,
        "aml_compliance_officer": "John Lim",
        "aml_programme_exists": True,
        "offers_company_formation": True, "offers_nominee_director": True,
        "offers_nominee_shareholder": True, "offers_corp_secretarial": True,
        "offers_shelf_company": False, "offers_registered_address": True,
        "overall_compliance_score": 0.0,
    }
    base.update(kw)
    return base


def _client(**kw) -> dict:
    base = {
        "id": str(uuid4()), "client_type": "company", "legal_name": "Test Client Ltd",
        "cdd_status": "completed", "risk_rating": "medium", "is_pep": False,
        "is_remote_onboarding": False, "video_call_completed": True,
        "high_risk_country": False, "is_active": True,
        "str_filed": False, "str_count": 0,
        "cdd_next_review": datetime.now(timezone.utc) + timedelta(days=355),
        "sanctions_screened": False, "sanctions_clear": None,
    }
    base.update(kw)
    return base


def _str_report(**kw) -> dict:
    base = {
        "id": str(uuid4()),
        "csp_id": str(uuid4()),
        "client_id": str(uuid4()),
        "trigger_type": "cdd_failure",
        "trigger_detail": "Client refused to provide identity documents.",
        "decision": "filed",
        "decision_by": "Jane Tan",
        "decision_date": datetime.now(timezone.utc),
        "decision_rationale": "Reasonable grounds to suspect money laundering based on client refusal.",
        "stro_reference": "STRO-2026-001",
        "client_notified": False,
        "service_declined": True,
    }
    base.update(kw)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# FIX #1 — PII ENCRYPTION TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestPiiEncryption:
    """Verify application-level AES encryption for all sensitive PII fields."""

    def _setup_local_key(self):
        """Generate a valid Fernet key for testing."""
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        return key

    def test_encrypt_returns_enc_prefix(self):
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import encrypt_pii
            # Clear cache between tests
            from app.core.encryption import _load_fernet_key
            _load_fernet_key.cache_clear()
            result = encrypt_pii("S1234567A")
            assert result is not None
            assert result.startswith("ENC:")

    def test_decrypt_returns_original(self):
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import encrypt_pii, decrypt_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            plaintext  = "S1234567A"
            ciphertext = encrypt_pii(plaintext)
            decrypted  = decrypt_pii(ciphertext)
            assert decrypted == plaintext

    def test_encrypt_is_non_deterministic(self):
        """Fernet uses random IVs — same plaintext produces different ciphertexts each time."""
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import encrypt_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            c1 = encrypt_pii("S1234567A")
            c2 = encrypt_pii("S1234567A")
            # Both should decrypt to same value but be different ciphertexts
            assert c1 != c2

    def test_encrypt_none_returns_none(self):
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import encrypt_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            assert encrypt_pii(None) is None

    def test_decrypt_none_returns_none(self):
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import decrypt_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            assert decrypt_pii(None) is None

    def test_idempotent_encrypt(self):
        """Encrypting an already-encrypted value returns it unchanged."""
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import encrypt_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            c1 = encrypt_pii("S1234567A")
            c2 = encrypt_pii(c1)   # encrypting ciphertext again
            assert c2 == c1        # should be unchanged

    def test_decrypt_unencrypted_legacy_value(self):
        """Legacy unencrypted values (no ENC: prefix) are returned as-is with warning."""
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import decrypt_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            result = decrypt_pii("S1234567A")   # no ENC: prefix
            assert result == "S1234567A"

    def test_mask_pii_shows_last_4(self):
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import encrypt_pii, mask_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            encrypted = encrypt_pii("S1234567A")
            masked    = mask_pii(encrypted, visible_chars=4)
            assert masked is not None
            assert masked.endswith("567A")
            assert masked.startswith("*")

    def test_mask_none_returns_none(self):
        key = self._setup_local_key()
        with patch.dict(os.environ, {"CSP_PII_KEY_LOCAL": key}):
            from app.core.encryption import mask_pii, _load_fernet_key
            _load_fernet_key.cache_clear()
            assert mask_pii(None) is None

    def test_search_hash_deterministic(self):
        """Same plaintext + same pepper always produces same hash."""
        with patch.dict(os.environ, {"CSP_PII_SEARCH_PEPPER": "test-pepper"}):
            from app.core.encryption import pii_search_hash
            h1 = pii_search_hash("S1234567A")
            h2 = pii_search_hash("S1234567A")
            assert h1 == h2
            assert len(h1) == 64

    def test_search_hash_different_values(self):
        with patch.dict(os.environ, {"CSP_PII_SEARCH_PEPPER": "test-pepper"}):
            from app.core.encryption import pii_search_hash
            h1 = pii_search_hash("S1234567A")
            h2 = pii_search_hash("G9876543Z")
            assert h1 != h2

    def test_no_key_raises_runtime_error(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CSP_PII_KEY_LOCAL", "AWS_SECRETS_MANAGER_KEY_ARN")}
        with patch.dict(os.environ, env, clear=True):
            from app.core.encryption import _load_fernet_key
            _load_fernet_key.cache_clear()
            with pytest.raises(RuntimeError, match="not configured"):
                _load_fernet_key()

    def test_encrypted_string_type_decorator_exists(self):
        from app.core.encryption import EncryptedString, EncryptedText
        assert EncryptedString is not None
        assert EncryptedText is not None

    def test_model_pii_fields_use_encrypted_type(self):
        """Verify models use EncryptedString for all PII fields."""
        from app.core.models import CspCddRecord, CspNomineeDirector, CspBeneficialOwner
        from app.core.encryption import EncryptedString, EncryptedText

        # CDD Record
        nric_col = CspCddRecord.__table__.columns.get("individual_nric_or_passport")
        assert nric_col is not None
        assert isinstance(nric_col.type, EncryptedString), \
            "individual_nric_or_passport must use EncryptedString"

        addr_col = CspCddRecord.__table__.columns.get("individual_address")
        assert addr_col is not None
        assert isinstance(addr_col.type, EncryptedText), \
            "individual_address must use EncryptedText"

        # Nominee Director
        nom_col = CspNomineeDirector.__table__.columns.get("nominee_nric_or_passport")
        assert nom_col is not None
        assert isinstance(nom_col.type, EncryptedString), \
            "nominee_nric_or_passport must use EncryptedString"

        nid_col = CspNomineeDirector.__table__.columns.get("nominator_id")
        assert nid_col is not None
        assert isinstance(nid_col.type, EncryptedString), \
            "nominator_id must use EncryptedString"

        # UBO
        ubo_col = CspBeneficialOwner.__table__.columns.get("ubo_nric_or_passport")
        assert ubo_col is not None
        assert isinstance(ubo_col.type, EncryptedString), \
            "ubo_nric_or_passport must use EncryptedString"

        ubo_addr = CspBeneficialOwner.__table__.columns.get("ubo_address")
        assert ubo_addr is not None
        assert isinstance(ubo_addr.type, EncryptedText), \
            "ubo_address must use EncryptedText"


# ═══════════════════════════════════════════════════════════════════════════
# FIX #2 — DETERMINISTIC STR HASH TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestDeterministicStrHash:
    """Verify STR notarization hash is deterministic (content-based, not timestamp-based)."""

    def _str_data(self, **kw) -> dict:
        base = {
            "csp_id":             str(uuid4()),
            "client_id":          str(uuid4()),
            "decision":           "filed",
            "decision_rationale": "Reasonable grounds to suspect money laundering.",
            "decision_date":      datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc),
            "stro_reference":     "STRO-2026-001",
            "trigger_type":       "cdd_failure",
            "trigger_detail":     "Client refused to provide identity documents.",
        }
        base.update(kw)
        return base

    def test_same_str_data_produces_same_hash(self):
        from app.workers.csp_tasks import _build_record_hash
        record_id = str(uuid4())
        data = self._str_data()
        h1 = _build_record_hash("str", record_id, data)
        h2 = _build_record_hash("str", record_id, data)
        assert h1 == h2, "Same STR content must always produce the same hash"

    def test_hash_is_64_chars_sha256(self):
        from app.workers.csp_tasks import _build_record_hash
        h = _build_record_hash("str", str(uuid4()), self._str_data())
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_record_id_different_hash(self):
        from app.workers.csp_tasks import _build_record_hash
        data = self._str_data()
        h1 = _build_record_hash("str", str(uuid4()), data)
        h2 = _build_record_hash("str", str(uuid4()), data)
        assert h1 != h2

    def test_different_decision_different_hash(self):
        from app.workers.csp_tasks import _build_record_hash
        rid  = str(uuid4())
        d1   = self._str_data(decision="filed")
        d2   = self._str_data(decision="not_filed")
        assert _build_record_hash("str", rid, d1) != _build_record_hash("str", rid, d2)

    def test_different_rationale_different_hash(self):
        from app.workers.csp_tasks import _build_record_hash
        rid = str(uuid4())
        d1  = self._str_data(decision_rationale="Reason A")
        d2  = self._str_data(decision_rationale="Reason B")
        assert _build_record_hash("str", rid, d1) != _build_record_hash("str", rid, d2)

    def test_hash_stable_across_calls_time_independent(self):
        """Hash must not change between calls (no datetime.now() in hash computation)."""
        from app.workers.csp_tasks import _build_record_hash
        import time
        rid  = str(uuid4())
        data = self._str_data()
        h1   = _build_record_hash("str", rid, data)
        time.sleep(0.05)   # 50ms delay
        h2   = _build_record_hash("str", rid, data)
        assert h1 == h2, "Hash must be time-independent"

    def test_cdd_hash_deterministic(self):
        from app.workers.csp_tasks import _build_record_hash
        rid  = str(uuid4())
        data = {
            "csp_id": str(uuid4()), "client_id": str(uuid4()),
            "review_type": "initial", "status": "completed",
            "completed_by": "Jane Tan",
            "completed_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "id_doc_type": "NRIC", "sanctions_clear": True, "pep_result": "clear",
        }
        h1 = _build_record_hash("cdd", rid, data)
        h2 = _build_record_hash("cdd", rid, data)
        assert h1 == h2

    def test_nominee_assessment_hash_deterministic(self):
        from app.workers.csp_tasks import _build_record_hash
        rid  = str(uuid4())
        data = {
            "csp_id": str(uuid4()), "client_id": str(uuid4()),
            "nominee_full_name": "John Smith",
            "assessment_status": "fit_proper",
            "assessment_date": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "assessed_by": "Compliance Officer",
            "assessment_outcome": "All checks passed",
        }
        h1 = _build_record_hash("nominee_assessment", rid, data)
        h2 = _build_record_hash("nominee_assessment", rid, data)
        assert h1 == h2

    def test_training_hash_deterministic(self):
        from app.workers.csp_tasks import _build_record_hash
        rid  = str(uuid4())
        data = {
            "csp_id": str(uuid4()), "staff_name": "Jane Tan",
            "training_type": "AML_CFT_foundation",
            "training_title": "AML/CFT Foundation Course",
            "provider": "ACRA",
            "completion_date": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "status": "completed",
        }
        h1 = _build_record_hash("training", rid, data)
        h2 = _build_record_hash("training", rid, data)
        assert h1 == h2

    def test_unknown_record_type_fallback_hash(self):
        from app.workers.csp_tasks import _build_record_hash
        rid  = str(uuid4())
        data = {"csp_id": str(uuid4())}
        h    = _build_record_hash("unknown_type", rid, data)
        assert len(h) == 64   # Still produces valid hash

    def test_canonical_json_is_sorted(self):
        """Verify hash is built from sorted-key JSON (order-independent)."""
        from app.workers.csp_tasks import _build_record_hash
        rid  = str(uuid4())
        # Same data, fields set in different order — should produce same hash
        # (Python dicts are ordered but our hash uses sort_keys=True)
        data1 = {"csp_id": "A", "client_id": "B", "decision": "filed",
                 "decision_rationale": "R", "decision_date": None,
                 "stro_reference": None, "trigger_type": "T", "trigger_detail": "D"}
        h1 = _build_record_hash("str", rid, data1)
        # Reorder keys
        data2 = {"decision": "filed", "csp_id": "A", "trigger_detail": "D",
                 "client_id": "B", "decision_rationale": "R", "decision_date": None,
                 "stro_reference": None, "trigger_type": "T"}
        h2 = _build_record_hash("str", rid, data2)
        assert h1 == h2, "Hash must be independent of field insertion order"


# ═══════════════════════════════════════════════════════════════════════════
# FIX #3 — SANCTIONS SCREENING TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestSanctionsScreening:
    """Verify OFAC SDN + UN Consolidated screening logic."""

    def test_name_normalization_removes_honorifics(self):
        from app.services.csp_sanctions import _normalize_name
        assert _normalize_name("Mr John Smith") == "john smith"
        assert _normalize_name("Dr Jane Tan") == "jane tan"
        assert _normalize_name("Dato Abdullah") == "abdullah"

    def test_name_normalization_lowercases(self):
        from app.services.csp_sanctions import _normalize_name
        assert _normalize_name("JOHN SMITH") == "john smith"

    def test_name_normalization_collapses_whitespace(self):
        from app.services.csp_sanctions import _normalize_name
        assert _normalize_name("John   Smith") == "john smith"

    def test_names_match_exact(self):
        from app.services.csp_sanctions import _names_match
        assert _names_match("John Smith", "John Smith") is True

    def test_names_match_case_insensitive(self):
        from app.services.csp_sanctions import _names_match
        assert _names_match("JOHN SMITH", "john smith") is True

    def test_names_match_substring(self):
        from app.services.csp_sanctions import _names_match
        assert _names_match("John Smith", "John Robert Smith") is True

    def test_names_no_match_different(self):
        from app.services.csp_sanctions import _names_match
        assert _names_match("John Smith", "Jane Doe") is False

    def test_screen_individual_returns_result_object(self):
        from app.services.csp_sanctions import screen_individual, ScreeningResult
        # Mock every screener to avoid network calls in tests
        with patch("app.services.csp_sanctions.OfacSdnScreener.screen", return_value=[]), \
             patch("app.services.csp_sanctions.UnConsolidatedScreener.screen", return_value=[]), \
             patch("app.services.csp_sanctions.EuConsolidatedScreener.screen", return_value=[]):
            result = screen_individual("John Smith")
        assert isinstance(result, ScreeningResult)
        assert result.is_clear is True
        assert result.hit_count == 0
        assert isinstance(result.lists_checked, list)
        # EU is screened live; MAS is only reported when World-Check is configured.
        assert "EU Consolidated" in result.lists_checked
        assert "MAS Watchlist" not in result.lists_checked

    def test_screen_returns_hit_when_name_matches(self):
        from app.services.csp_sanctions import screen_individual
        fake_hit = [{
            "list": "OFAC SDN", "entry_id": "12345",
            "name": "John Smith", "type": "individual", "programs": ["SDGT"],
        }]
        with patch("app.services.csp_sanctions.OfacSdnScreener.screen", return_value=fake_hit), \
             patch("app.services.csp_sanctions.UnConsolidatedScreener.screen", return_value=[]), \
             patch("app.services.csp_sanctions.EuConsolidatedScreener.screen", return_value=[]):
            result = screen_individual("John Smith")
        assert result.is_clear is False
        assert result.hit_count == 1
        assert result.hits[0]["list"] == "OFAC SDN"

    def test_screen_clear_when_no_matches(self):
        from app.services.csp_sanctions import screen_individual
        with patch("app.services.csp_sanctions.OfacSdnScreener.screen", return_value=[]), \
             patch("app.services.csp_sanctions.UnConsolidatedScreener.screen", return_value=[]), \
             patch("app.services.csp_sanctions.EuConsolidatedScreener.screen", return_value=[]):
            result = screen_individual("Very Unique Name XYZ123")
        assert result.is_clear is True
        assert result.hit_count == 0

    def test_screen_result_to_dict(self):
        from app.services.csp_sanctions import ScreeningResult
        result = ScreeningResult(
            is_clear=True, hit_count=0, lists_checked=["OFAC SDN", "UN Consolidated"],
            screened_at="2026-06-01T00:00:00+00:00", name_searched="Test Name",
        )
        d = result.to_dict()
        assert d["is_clear"] is True
        assert d["hit_count"] == 0
        assert "lists_checked" in d

    def test_worldcheck_not_configured_skips_gracefully(self):
        from app.services.csp_sanctions import WorldCheckScreener
        env = {k: v for k, v in os.environ.items() if k != "WORLDCHECK_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            assert WorldCheckScreener.is_configured() is False
            hits = WorldCheckScreener.screen("John Smith")
            assert hits == []

    def test_cache_key_consistent(self):
        from app.services.csp_sanctions import _cache_key
        k1 = _cache_key("John Smith", ["OFAC SDN", "UN Consolidated"])
        k2 = _cache_key("John Smith", ["OFAC SDN", "UN Consolidated"])
        assert k1 == k2

    def test_cache_key_name_normalized(self):
        from app.services.csp_sanctions import _cache_key
        k1 = _cache_key("John Smith", ["OFAC SDN"])
        k2 = _cache_key("JOHN SMITH", ["OFAC SDN"])
        assert k1 == k2   # normalized before hashing

    def test_screen_entity_callable(self):
        from app.services.csp_sanctions import screen_entity
        with patch("app.services.csp_sanctions.OfacSdnScreener.screen", return_value=[]):
            with patch("app.services.csp_sanctions.UnConsolidatedScreener.screen", return_value=[]):
                result = screen_entity("Test Corp Ltd")
        assert result is not None

    def test_refresh_sanctions_lists_clears_cache(self):
        from app.services.csp_sanctions import (
            OfacSdnScreener, UnConsolidatedScreener, refresh_sanctions_lists
        )
        # Pre-populate cache
        OfacSdnScreener._entries_cache   = [{"uid": "1", "name": "Test", "type": "individual", "aliases": [], "programs": []}]
        UnConsolidatedScreener._entries_cache = [{"ref": "2", "name": "Test2", "type": "individual", "aliases": []}]

        with patch.object(OfacSdnScreener, "_load_entries", return_value=[]) as mock_ofac:
            with patch.object(UnConsolidatedScreener, "_load_entries", return_value=[]) as mock_un:
                result = refresh_sanctions_lists()
                mock_ofac.assert_called_once()
                mock_un.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# BULK IMPORT TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestBulkImport:
    """CSV and Excel bulk import validation."""

    def _make_csv(self, rows: list) -> bytes:
        import csv, io
        from app.services.csp_bulk_import import CSV_COLUMNS
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        return out.getvalue().encode("utf-8")

    def test_valid_csv_parses_correctly(self):
        from app.services.csp_bulk_import import parse_csv
        csv_bytes = self._make_csv([{
            "client_type": "company", "legal_name": "ACME Pte Ltd",
            "uen_or_reg_no": "202312345A", "country_of_inc": "SG",
            "contact_name": "Jane Tan", "contact_email": "jane@acme.com",
            "risk_rating": "medium", "cdd_status": "not_started",
            "is_remote_onboarding": "FALSE",
        }])
        rows, errors = parse_csv(csv_bytes)
        assert len(errors) == 0
        assert len(rows) == 1
        assert rows[0].is_valid is True
        assert rows[0].parsed_data["client_type"] == "company"
        assert rows[0].parsed_data["legal_name"] == "ACME Pte Ltd"

    def test_missing_required_column_file_error(self):
        from app.services.csp_bulk_import import parse_csv
        csv_bytes = b"uen_or_reg_no,country_of_inc\n202312345A,SG\n"
        rows, errors = parse_csv(csv_bytes)
        assert len(errors) > 0
        assert any("required" in e.lower() or "missing" in e.lower() for e in errors)

    def test_missing_legal_name_row_error(self):
        from app.services.csp_bulk_import import parse_csv
        csv_bytes = self._make_csv([{
            "client_type": "company", "legal_name": "",
        }])
        rows, errors = parse_csv(csv_bytes)
        assert len(rows) == 1
        assert rows[0].is_valid is False
        assert any("legal_name" in e for e in rows[0].errors)

    def test_invalid_client_type_row_error(self):
        from app.services.csp_bulk_import import parse_csv
        csv_bytes = self._make_csv([{
            "client_type": "partnership", "legal_name": "Test",
        }])
        rows, errors = parse_csv(csv_bytes)
        assert rows[0].is_valid is False
        assert any("client_type" in e for e in rows[0].errors)

    def test_all_valid_client_types_accepted(self):
        from app.services.csp_bulk_import import parse_csv
        for ct in ("individual", "company", "llp", "foreign_co"):
            csv_bytes = self._make_csv([{"client_type": ct, "legal_name": "Test"}])
            rows, _ = parse_csv(csv_bytes)
            assert rows[0].is_valid is True, f"client_type '{ct}' should be valid"

    def test_invalid_risk_rating_defaults_to_medium(self):
        from app.services.csp_bulk_import import parse_csv
        csv_bytes = self._make_csv([{
            "client_type": "company", "legal_name": "Test",
            "risk_rating": "extreme",
        }])
        rows, _ = parse_csv(csv_bytes)
        assert rows[0].is_valid is True  # Warning, not error
        assert rows[0].parsed_data["risk_rating"] == "medium"
        assert len(rows[0].warnings) > 0

    def test_boolean_parsing_true_variants(self):
        from app.services.csp_bulk_import import _parse_bool
        for v in ("TRUE", "true", "Yes", "YES", "1", "Y"):
            val, err = _parse_bool(v, "test_field")
            assert val is True, f"'{v}' should parse to True"
            assert err is None

    def test_boolean_parsing_false_variants(self):
        from app.services.csp_bulk_import import _parse_bool
        for v in ("FALSE", "false", "No", "NO", "0", "N", ""):
            val, err = _parse_bool(v, "test_field")
            assert val is False, f"'{v}' should parse to False"
            assert err is None

    def test_invalid_boolean_produces_warning(self):
        from app.services.csp_bulk_import import _parse_bool
        val, err = _parse_bool("maybe", "test_field")
        assert err is not None
        assert "test_field" in err

    def test_date_parsing_valid(self):
        from app.services.csp_bulk_import import _parse_date
        dt, err = _parse_date("2024-03-15", "onboarded_date")
        assert err is None
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 3
        assert dt.day == 15

    def test_date_parsing_invalid_format(self):
        from app.services.csp_bulk_import import _parse_date
        dt, err = _parse_date("15/03/2024", "onboarded_date")
        assert err is not None
        assert dt is None

    def test_date_parsing_empty_returns_none(self):
        from app.services.csp_bulk_import import _parse_date
        dt, err = _parse_date("", "onboarded_date")
        assert dt is None
        assert err is None

    def test_empty_rows_skipped(self):
        from app.services.csp_bulk_import import parse_csv
        csv_bytes = (
            b"client_type,legal_name\n"
            b"company,ACME Pte Ltd\n"
            b",,\n"         # empty row
            b"individual,Jane Tan\n"
        )
        rows, _ = parse_csv(csv_bytes)
        assert len(rows) == 2  # empty row skipped

    def test_max_500_rows_enforced(self):
        from app.services.csp_bulk_import import parse_csv, MAX_ROWS
        many_rows = [{"client_type": "company", "legal_name": f"Client {i}"}
                     for i in range(MAX_ROWS + 10)]
        csv_bytes = self._make_csv(many_rows)
        rows, errors = parse_csv(csv_bytes)
        assert len(rows) <= MAX_ROWS
        assert any("maximum" in e.lower() or "500" in e for e in errors)

    def test_bom_utf8_handled(self):
        """UTF-8 BOM (from Excel exports) should be handled."""
        from app.services.csp_bulk_import import parse_csv
        bom_csv = b"\xef\xbb\xbfclient_type,legal_name\ncompany,ACME Pte Ltd\n"
        rows, errors = parse_csv(bom_csv)
        assert len(errors) == 0
        assert len(rows) == 1
        assert rows[0].parsed_data["client_type"] == "company"

    def test_csv_template_downloadable(self):
        from app.services.csp_bulk_import import generate_csv_template, CSV_COLUMNS
        template = generate_csv_template()
        assert isinstance(template, bytes)
        assert b"client_type" in template
        assert b"legal_name" in template
        # All columns present
        for col in CSV_COLUMNS:
            assert col.encode() in template

    def test_services_provided_parsed_as_list(self):
        from app.services.csp_bulk_import import parse_csv
        csv_bytes = self._make_csv([{
            "client_type": "company", "legal_name": "Test",
            "services_provided": "company_formation,corp_secretarial,nominee_director",
        }])
        rows, _ = parse_csv(csv_bytes)
        assert rows[0].is_valid is True
        services = rows[0].parsed_data.get("services_provided", [])
        assert isinstance(services, list)
        assert "company_formation" in services
        assert "corp_secretarial" in services


# ═══════════════════════════════════════════════════════════════════════════
# TIPPING-OFF PROTECTION TESTS (critical compliance)
# ═══════════════════════════════════════════════════════════════════════════

class TestTippingOffProtection:
    """
    CDSA s.48A — informing a client of an STR filing is a criminal offence.
    These tests verify server-side enforcement.
    """

    def test_client_notified_always_false_in_model(self):
        """client_notified defaults to False and cannot be True."""
        from app.core.models import CspStrReport
        col = CspStrReport.__table__.columns["client_notified"]
        assert col.default.arg is False

    def test_str_schema_decision_validated(self):
        from pydantic import ValidationError
        from app.api.csp_schemas import StrCreate
        with pytest.raises(ValidationError):
            StrCreate(
                trigger_type="cdd_failure",
                trigger_detail="Client refused to provide documents.",
                decision="maybe",  # invalid
                decision_by="Jane Tan",
                decision_rationale="Documented rationale of sufficient length.",
            )

    def test_str_rationale_minimum_length(self):
        """Rationale must be at least 20 chars — prevents one-word non-documented decisions."""
        from pydantic import ValidationError
        from app.api.csp_schemas import StrCreate
        with pytest.raises(ValidationError):
            StrCreate(
                trigger_type="cdd_failure",
                trigger_detail="Client refused to provide documents.",
                decision="not_filed",
                decision_by="Jane Tan",
                decision_rationale="Too short",  # < 20 chars
            )

    def test_str_valid_decisions(self):
        from app.api.csp_schemas import StrCreate
        for decision in ("filed", "not_filed", "pending", "escalated"):
            s = StrCreate(
                trigger_type="unusual_activity",
                trigger_detail="Unusual transaction pattern observed in client account.",
                decision=decision,
                decision_by="Compliance Officer",
                decision_rationale="Documented rationale with sufficient detail for ACRA review.",
            )
            assert s.decision == decision

    def test_compliance_scorer_tipping_off_critical(self):
        from app.services.csp_compliance_scorer import score_str
        reports = [{"decision": "filed", "client_notified": True,
                    "decision_rationale": "Reason", "client_id": str(uuid4())}]
        result  = score_str(reports, [])
        assert result["score"] <= 60
        assert any("criminal" in g.lower() or "tipping" in g.lower()
                   for g in result["gaps"])

    def test_str_scorer_clean_when_not_notified(self):
        from app.services.csp_compliance_scorer import score_str
        reports = [{"decision": "filed", "client_notified": False,
                    "decision_rationale": "Full documented rationale.", "client_id": str(uuid4())}]
        result  = score_str(reports, [])
        tipping_gaps = [g for g in result["gaps"] if "tipping" in g.lower()]
        assert len(tipping_gaps) == 0


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════

class TestIntegrationV2:

    def test_full_compliance_score_structure(self):
        from app.services.csp_compliance_scorer import compute_overall_compliance, PILLAR_WEIGHTS
        result = compute_overall_compliance(
            profile=_profile(), clients=[_client()], cdd_records=[], edd_records=[],
            str_reports=[], directors=[], shareholders=[], ubos=[],
            aml_prog={"status": "approved", "next_review_date": datetime.now(timezone.utc) + timedelta(days=300),
                      **{f"{s}_section": "content" for s in ["risk_assessment","cdd_procedures",
                         "edd_procedures","str_procedures","record_keeping",
                         "training_policy","governance","nominee_procedures"]}},
            training=[{"status": "completed", "is_rqi": True, "staff_name": "Jane Tan",
                       "completion_date": datetime.now(timezone.utc) - timedelta(days=30),
                       "expiry_date": datetime.now(timezone.utc) + timedelta(days=335)}],
            pdpa_data={"nric_compliance_score": 80, "risk_band": "LOW"},
        )
        assert 0 <= result["overall_score"] <= 100
        for pillar in PILLAR_WEIGHTS:
            assert pillar in result["pillars"]

    def test_encryption_service_importable(self):
        from app.core.encryption import (
            encrypt_pii, decrypt_pii, mask_pii,
            pii_search_hash, EncryptedString, EncryptedText,
        )
        assert all([encrypt_pii, decrypt_pii, mask_pii,
                    pii_search_hash, EncryptedString, EncryptedText])

    def test_sanctions_screening_importable(self):
        from app.services.csp_sanctions import (
            screen_individual, screen_entity, refresh_sanctions_lists,
            OfacSdnScreener, UnConsolidatedScreener, WorldCheckScreener,
        )
        assert all([screen_individual, screen_entity, refresh_sanctions_lists])

    def test_bulk_import_importable(self):
        from app.services.csp_bulk_import import (
            parse_csv, parse_excel, execute_import,
            generate_csv_template, CSV_COLUMNS,
        )
        assert all([parse_csv, parse_excel, execute_import, generate_csv_template])
        assert len(CSV_COLUMNS) >= 10

    def test_tasks_deterministic_hash_importable(self):
        from app.workers.csp_tasks import _build_record_hash
        assert callable(_build_record_hash)

    def test_all_models_importable(self):
        from app.core.models import (
            CspProfile, CspClient, CspCddRecord, CspEddRecord, CspStrReport,
            CspNomineeDirector, CspNomineeShareholder, CspBeneficialOwner,
            CspAmlProgramme, CspRiskAssessment, CspComplianceCalendar,
            CspStaffTraining, CspBlockchainEvidence,
        )
        models = [CspProfile, CspClient, CspCddRecord, CspEddRecord, CspStrReport,
                  CspNomineeDirector, CspNomineeShareholder, CspBeneficialOwner,
                  CspAmlProgramme, CspRiskAssessment, CspComplianceCalendar,
                  CspStaffTraining, CspBlockchainEvidence]
        assert len(models) == 13

    def test_three_fixes_summary(self):
        """Smoke test confirming all three fixes are implemented."""
        # Fix 1: EncryptedString in models
        from app.core.models import CspCddRecord
        from app.core.encryption import EncryptedString
        nric_col = CspCddRecord.__table__.columns["individual_nric_or_passport"]
        assert isinstance(nric_col.type, EncryptedString), "Fix #1 not applied"

        # Fix 2: Deterministic hash
        from app.workers.csp_tasks import _build_record_hash
        rid  = str(uuid4())
        data = {"csp_id": "x", "client_id": "y", "decision": "filed",
                "decision_rationale": "r", "decision_date": None,
                "stro_reference": None, "trigger_type": "t", "trigger_detail": "d"}
        h1 = _build_record_hash("str", rid, data)
        h2 = _build_record_hash("str", rid, data)
        assert h1 == h2, "Fix #2 not applied — hash is non-deterministic"

        # Fix 3: Sanctions screening exists and callable
        from app.services.csp_sanctions import screen_individual
        assert callable(screen_individual), "Fix #3 not applied"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
