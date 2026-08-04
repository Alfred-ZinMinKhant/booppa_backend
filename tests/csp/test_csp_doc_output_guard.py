"""The generated document is checked, not just the prompt that asked for it.

Schema v2 corrected the tipping-off citation from CDSA s.48A to s.48, but that
correction lives in a prompt string. Nothing in the system persona forbids the
model from "correcting" it back to the more commonly-miscited s.48A, and
`schema_version: 2` was stamped unconditionally on whatever came back — so a
delivered PDF could carry the wrong statute under a version claiming the right
one.

The truncation half was found by a live run: at the old 4096-token ceiling both
the STR Policy and the AML/CFT Programme came back cut mid-sentence with
`finish_reason == "length"`, and the Programme never reached its tipping-off
section at all. A half-written AML programme reads as authoritative for its first
2,700 words; the compliance officer has no way to see that the rest is missing.
Raising the ceiling to 8192 fixed the STR Policy but not the Programme, which is
now generated in four parts — see the second half of this file.
"""
from __future__ import annotations

import pytest

from app.services import csp_doc_generator as gen


@pytest.fixture
def _one_doc(monkeypatch):
    """Reduce the catalog to a single profile-only document."""
    monkeypatch.setattr(
        gen, "CSP_DOCUMENT_CATALOG",
        [("str_policy", "STR Policy and Decision Framework", gen.gen_str_policy, False)],
    )


def _stub(monkeypatch, content: str, finish: str = "stop"):
    monkeypatch.setattr(gen, "_call", lambda system, user: (content, 100, 200, finish))


def _stub_parts(monkeypatch, *parts):
    """Answer successive `_call`s from a queue — `gen_aml_programme` makes four."""
    queue = list(parts)
    monkeypatch.setattr(gen, "_call", lambda system, user: queue.pop(0))


GOOD = "Tipping-off is a criminal offence under CDSA s.48. File via STRO SONAR."


def test_good_document_is_kept_and_stamped(_one_doc, monkeypatch):
    _stub(monkeypatch, GOOD)
    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] == GOOD
    assert doc["schema_version"] == gen.CSP_DOC_SCHEMA_VERSION


@pytest.mark.parametrize("bad", [
    "Tipping-off is an offence under CDSA s.48A.",
    "Tipping-off is an offence under CDSA s. 48A.",
    "an offence under section 48A of the Corruption, Drug Trafficking and Other "
    "Serious Crimes Act",
])
def test_s48a_citation_is_rejected_not_stamped(_one_doc, monkeypatch, bad):
    _stub(monkeypatch, bad)
    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] is None
    assert "s.48" in doc["error"]
    # The failure must not masquerade as a v2 deliverable.
    assert "schema_version" not in doc


def test_s48_alone_does_not_trip_the_guard(_one_doc, monkeypatch):
    """`\\bs\\.?\\s*48A\\b` must not fire on a correct s.48 followed by a word."""
    _stub(monkeypatch, "Under CDSA s.48 Any disclosure is prohibited. See s.48 above.")
    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] is not None


def test_truncated_document_is_rejected(_one_doc, monkeypatch):
    _stub(monkeypatch, GOOD + " The procedure for", finish="length")
    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] is None
    assert "truncated" in doc["error"]


# ── The AML/CFT Programme is generated in four parts ─────────────────────────
#
# Nine sections did not fit in one completion, or in two. A single call at the
# 8192-token ceiling was cut inside Section 7; a 1-5 / 6-9 split was cut inside
# Section 4, which silently dropped Section 5 — the tipping-off section this
# document's whole s.48 correction lives in. Splitting it further is only safe if
# every failure mode of a single call survives the concatenation, which is what
# these pin.


@pytest.fixture
def _aml_only(monkeypatch):
    monkeypatch.setattr(
        gen, "CSP_DOCUMENT_CATALOG",
        [("aml_programme", "AML/CFT/PF Programme", gen.gen_aml_programme, True)],
    )


def _four(*, texts=None, finishes=None):
    texts = texts or ["part one", "part two", "part three", "part four"]
    finishes = finishes or ["stop"] * 4
    return [(t, 100, 200, f) for t, f in zip(texts, finishes)]


def test_aml_programme_concatenates_every_part(_aml_only, monkeypatch):
    _stub_parts(monkeypatch, *_four(texts=[
        "SECTION 1 — EXECUTIVE COMMITMENT",
        "SECTION 3 — CUSTOMER DUE DILIGENCE",
        "SECTION 5 ... Tipping-off is an offence under CDSA s.48.",
        "SECTION 9 — PROGRAMME REVIEW",
    ]))
    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    for marker in ("SECTION 1", "SECTION 3", "SECTION 5", "SECTION 9"):
        assert marker in doc["content"]
    assert doc["schema_version"] == gen.CSP_DOC_SCHEMA_VERSION


def test_aml_programme_reports_summed_usage(_aml_only, monkeypatch):
    """Cost/token accounting must cover all four calls, not just the first."""
    _stub_parts(monkeypatch, *_four())
    content, in_tok, out_tok, finish = gen.gen_aml_programme({"legal_name": "X"}, [])

    assert (in_tok, out_tok, finish) == (400, 800, "stop")
    assert content == "part one\n\npart two\n\npart three\n\npart four"


@pytest.mark.parametrize("cut", [0, 1, 2, 3])
def test_a_truncated_part_rejects_the_whole_programme(_aml_only, monkeypatch, cut):
    """A complete-looking programme missing one middle section is the worst case:
    nothing in the delivered text says a section is absent."""
    finishes = ["stop"] * 4
    finishes[cut] = "length"
    _stub_parts(monkeypatch, *_four(finishes=finishes))

    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] is None
    assert "truncated" in doc["error"]


@pytest.mark.parametrize("blank", [0, 1, 2, 3])
def test_an_empty_part_rejects_the_whole_programme(_aml_only, monkeypatch, blank):
    texts = ["part one", "part two", "part three", "part four"]
    texts[blank] = ""
    _stub_parts(monkeypatch, *_four(texts=texts))

    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] is None
    assert "empty" in doc["error"]


@pytest.mark.parametrize("where", [0, 1, 2, 3])
def test_s48a_in_any_part_is_still_caught(_aml_only, monkeypatch, where):
    """The guard reads the concatenation, so no part can smuggle it in."""
    texts = ["part one", "part two", "part three", "part four"]
    texts[where] = "see CDSA s.48A for tipping-off"
    _stub_parts(monkeypatch, *_four(texts=texts))

    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] is None
    assert "s.48" in doc["error"]


def test_empty_response_is_rejected(_one_doc, monkeypatch):
    """A missing DEEPSEEK_API_KEY / empty completion used to persist as a doc."""
    _stub(monkeypatch, "   ")
    (doc,) = gen.generate_all_csp_documents({"legal_name": "X"}, [])

    assert doc["content"] is None
    assert "empty" in doc["error"]
