"""Self-test for scripts/check_api_contract.py.

A contract checker that silently stops matching is worse than no checker: it
reports OK forever and the build stays green through exactly the bugs it exists
to catch. Every assertion here corresponds to a real mis-match this script
produced while being written — each one made it either blind (passing a genuine
404) or noisy (failing on a path that works).
"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_api_contract",
    Path(__file__).resolve().parent.parent / "scripts" / "check_api_contract.py",
)
cc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cc)


# ── Path normalisation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("/reports/${id}", "/reports/*"),
    ("/reports/{report_id}", "/reports/*"),
    ("/compare/", "/compare"),
    ("/a//b", "/a/b"),
])
def test_normalise(raw, expected):
    assert cc.normalise(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("/api/v1/auth/me", "/auth/me"),
    ("/api/auth/me", "/auth/me"),
    # An interpolation glued straight onto the mount, with no slash. Missing
    # this left `/v1*` as the candidate and reported every such call as a 404.
    ("/api/v1*", "*"),
    ("/no-prefix", "/no-prefix"),
])
def test_strip_api_prefix(raw, expected):
    assert cc.strip_api_prefix(raw) == expected


# ── Route matching ───────────────────────────────────────────────────────────

def test_exact_and_parameterised_match():
    known = {"/auth/me", "/reports/*"}
    assert cc.matches("/auth/me", known)
    assert cc.matches("/reports/*", known)


def test_catch_all_matches_any_depth():
    """app/api/enterprise/[...path]/route.ts proxies every enterprise call.
    Treating it as a single segment made ~15 working calls read as 404s."""
    known = {"/api/enterprise/**"}
    assert cc.matches("/api/enterprise/organisations/*/watchlist/*/comments", known)
    assert cc.matches("/api/enterprise/seats", known)
    assert cc.matches("/api/enterprise", known)


def test_wildcard_inside_a_segment_matches_a_literal():
    """`${name}.pdf` normalises to `*.pdf`; the backend declares the four
    artefact routes literally."""
    assert cc.matches("/vendor-artifacts/*.pdf",
                      {"/vendor-artifacts/badge-certificate.pdf"})


def test_a_genuinely_absent_path_does_not_match():
    known = {"/rankings/leaderboard/all", "/rankings/leaderboard/*"}
    # The real bug this check found: no bare /rankings/leaderboard exists.
    assert not cc.matches("/rankings/leaderboard", known)
    assert not cc.matches("/funnel/revenue-summary", {"/funnel/revenue"})


def test_segment_count_is_respected():
    assert not cc.matches("/a/b/c", {"/a/b"})
    assert not cc.matches("/a/b", {"/a/b/c"})


# ── Target resolution ────────────────────────────────────────────────────────

def test_only_rewritten_prefixes_reach_the_backend():
    """A bare relative /api/… path with no Next handler 404s unless a
    next.config.js rewrite proxies it. `/api/rfp-intake/…` did not — that was a
    live post-payment failure."""
    assert cc.rewritten_to_backend("/api/v1/anything")
    assert cc.rewritten_to_backend("/api/public/cms-media/x")
    assert not cc.rewritten_to_backend("/api/rfp-intake/*/extract")


def test_unresolvable_paths_are_skipped_not_passed():
    assert cc.is_unresolvable("/vendor/**")
    assert cc.is_unresolvable("*")
    assert cc.is_unresolvable("/enterprise/${path.join(")
    assert not cc.is_unresolvable("/auth/me")


def test_query_suffix_is_stripped_but_path_params_are_not():
    """`${qs}` is a query string; `${params.ticketId}` is a path segment.
    Conflating them silently truncated /tickets/track/{id} to /tickets/track."""
    assert cc.QUERY_SUFFIX.sub("", "/awards${qs}") == "/awards"
    assert cc.QUERY_SUFFIX.sub("", "/track/${params.ticketId}") == "/track/${params.ticketId}"


# ── The checker is actually wired to something ───────────────────────────────

def test_backend_routes_are_discovered():
    """Guards the failure mode where the import breaks, zero routes load, and
    everything is reported as a 404 — or worse, the script is made lenient to
    compensate."""
    routes = cc.backend_routes()
    assert len(routes) > 100
    assert "/auth/me" in routes


def test_allowlist_stays_empty():
    """It was seeded empty because every orphan proved fixable. An entry here
    is a blind spot and needs a comment justifying it — this test is the prompt
    to write one."""
    assert cc.ALLOWLIST == set()
