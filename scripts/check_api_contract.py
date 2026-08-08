#!/usr/bin/env python3
"""Fail the build when the frontend calls an API path that does not exist.

The 2026-08-07 audit found six separate live 404s of this shape — enterprise
organisations, seats, SSO (x2), sso-discover, bulk-request — all of which type-
checked, reviewed cleanly, and failed only in production. They are invisible to
every other check we run because a URL is just a string on one side and a
decorator on the other, and nothing joins them.

This joins them. Three call shapes, three different targets, and getting the
target wrong is itself one of the bugs:

  fetchWithAuth('/api/v1/x')      → BACKEND. lib/auth.ts prepends config.apiUrl,
                                     so this never reaches a Next route handler
                                     even though it looks like a relative path.
                                     This shape produced five of the six 404s.
  `${config.apiUrl}/api/v1/x`     → BACKEND, explicitly.
  apiPath('/x')                   → BACKEND, with /api/v1 supplied by the helper.
  fetch('/api/x')                 → NEXT route handler. A bare relative path is
                                     resolved against the Next origin by the
                                     browser; it cannot reach the backend.

Backend routes are enumerated by importing `app.main:app` rather than grepping
for decorators — the audit's own regex pass missed `sso_router` and
`content_router`, which are attached programmatically.

Usage:
    python scripts/check_api_contract.py [--frontend ../booppa-nextjs]

Exit codes: 0 = clean (or frontend absent), 1 = unmatched paths found.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Known-unmatched paths, exempted so the build can stay green. It is EMPTY, and
# that is the point: the plan called for seeding it from the current orphan list,
# but every orphan turned out to be either already fixed or a real bug worth
# fixing now, so there was nothing to grandfather. An empty allowlist means this
# check has no blind spots — keep it that way. Adding an entry needs a comment
# saying why the call cannot simply be corrected.
ALLOWLIST: set[str] = set()

SKIP_DIRS = {"node_modules", ".next", ".git", "dist", "build", "coverage", "e2e"}

# Call shapes. Each yields the path as group 1.
BACKEND_PATTERNS = [
    # fetchWithAuth('/api/v1/…')  — prepends config.apiUrl inside lib/auth.ts
    re.compile(r"""fetchWithAuth\(\s*[`'"](/[^`'"?\s]*)"""),
    # `${<anything>}/api/…` — any interpolated base followed by the /api mount.
    #
    # This used to enumerate the identifier by name
    # (`config.apiUrl|apiUrl|API_URL|API_BASE|apiBase`), which meant a component
    # declaring its own `const API = process.env.NEXT_PUBLIC_API_BASE` was
    # invisible to this check. 14 call sites were unscanned that way, including
    # the government dashboard's POST to `/api/v1/reports/gov-shortlist` — an
    # endpoint that does not exist and shipped anyway (AUDIT_2026-08-08.md P1-4).
    #
    # The discriminator is the `/api/` mount, not what someone named the
    # variable, so match on that instead and stop guessing at identifiers.
    re.compile(r"""\$\{[^}]*\}(/api/[^`'"?\s]*)"""),
    # apiPath('/…') — the helper supplies /api/v1
    re.compile(r"""apiPath\(\s*[`'"](/[^`'"?\s]*)"""),
]
NEXT_PATTERN = re.compile(r"""(?<!\w)fetch\(\s*[`'"](/api/[^`'"?\s]*)""")

# `// …`, `/* …`, or a ` * …` continuation line inside a block comment.
COMMENT_LINE = re.compile(r"\s*(//|/\*|\*)")

# A `${…}` interpolation inside a path is a path parameter; normalise it to the
# same wildcard the backend's `{id}` becomes so the two can be compared.
INTERP = re.compile(r"\$\{[^}]*\}")
BRACE_PARAM = re.compile(r"\{[^}]*\}")


# Most call sites don't inline the path — they interpolate `endpoints.auth.me`
# from lib/config.ts. Without resolving those, the check would skip ~40 of the
# real call sites as "too dynamic" and quietly cover almost nothing.
ENDPOINT_REF = re.compile(r"\$\{\s*endpoints((?:\.\w+)+)\s*(?:\([^)]*\))?\s*\}")
_ENTRY = re.compile(
    r"""(\w+)\s*:\s*(?:\([^)]*\)\s*=>\s*)?[`'"](/[^`'"]*)[`'"]"""
)


def load_endpoints(frontend: Path) -> dict[str, str]:
    """Flatten lib/config.ts's `endpoints` object to {".auth.me": "/auth/me"}."""
    src = frontend / "lib" / "config.ts"
    if not src.is_file():
        return {}
    text = src.read_text(encoding="utf-8")
    start = text.find("export const endpoints")
    if start < 0:
        return {}
    out: dict[str, str] = {}
    stack: list[str] = []
    depth = 0
    for line in text[start:].splitlines():
        m = _ENTRY.search(line)
        if m:
            out["." + ".".join(stack + [m.group(1)])] = m.group(2)
        elif re.match(r"\s*(\w+)\s*:\s*\{", line):
            stack.append(re.match(r"\s*(\w+)\s*:", line).group(1))
        opened = line.count("{")
        closed = line.count("}")
        depth += opened - closed
        if closed > opened and stack:
            stack.pop()
        if depth <= 0 and out:
            break
    return out


# Trailing query-string interpolation: `…/awards${qs}`. Not part of the path,
# but glued to the last segment, so it must come off before comparison or every
# proxy route reads as a 404.
QUERY_SUFFIX = re.compile(
    r"(?:\$\{\s*(?:qs|search|query|queryString|searchParams)\b[^}]*\}\s*)+$"
)


def is_unresolvable(candidate: str) -> bool:
    """True when nothing concrete survives: a bare or leading wildcard."""
    if "${" in candidate:
        # An interpolation the extractor could not close — `${path.join('/')}`
        # contains a quote, which terminates the surrounding template literal
        # as far as a regex is concerned. These are catch-all proxies.
        return True
    parts = [p for p in candidate.strip("/").split("/") if p]
    return not parts or parts[0].strip("*") == "" or "**" in candidate


def normalise(path: str) -> str:
    """Reduce a path to its comparable shape: params wildcarded, no trailing /."""
    p = INTERP.sub("*", path)
    p = BRACE_PARAM.sub("*", p)
    p = re.sub(r"/+", "/", p)
    if len(p) > 1:
        p = p.rstrip("/")
    return p


def strip_api_prefix(path: str) -> str:
    """Drop the mount prefix. app/main.py mounts the same router at both."""
    for prefix in ("/api/v1", "/api"):
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
        if path.startswith(prefix):
            # `/api/v1${p}` — an interpolation glued straight onto the mount.
            # No real route can collide, since every route starts with "/".
            return path[len(prefix):]
    return path


def matches(candidate: str, known: set[str]) -> bool:
    """Wildcard-aware membership.

    `*` matches exactly one segment on either side; `**` (from a Next
    `[...slug]` / `[[...slug]]` catch-all) matches any number, including zero.
    """
    if candidate in known:
        return True
    cand_parts = [p for p in candidate.strip("/").split("/") if p]
    for route in known:
        route_parts = [p for p in route.strip("/").split("/") if p]
        if _seg_match(cand_parts, route_parts):
            return True
    return False


def _seg_match(cand: list[str], route: list[str]) -> bool:
    if not route:
        return not cand
    head, rest = route[0], route[1:]
    if head == "**":
        # Catch-all: try consuming 0..n leading candidate segments.
        return any(_seg_match(cand[i:], rest) for i in range(len(cand) + 1))
    if not cand:
        return False
    # A segment containing "*" anywhere is a wildcard for that segment:
    # `${name}.pdf` normalises to `*.pdf`, which must still match the literal
    # `badge-certificate.pdf` route the backend declares.
    if head != cand[0] and "*" not in head and "*" not in cand[0]:
        return False
    return _seg_match(cand[1:], rest)


# Mirrors next.config.js `rewrites()`. A bare relative /api/… path with no Next
# route handler is proxied to the backend if it matches one of these prefixes,
# and 404s if it does not. Kept in sync by hand — if a rewrite is added there
# and not here, this check reports a false failure, which is the safe direction.
REWRITE_PREFIXES = ("/api/v1/", "/api/admin/intelligence", "/api/public/")


def served_by_next(path: str, nextjs: set[str]) -> bool:
    """Next filesystem routes are matched before rewrites, so a handler wins."""
    return matches(path, nextjs)


def rewritten_to_backend(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in REWRITE_PREFIXES)


def backend_routes() -> set[str]:
    """Import the app and read its route table — the only authoritative source."""
    os.environ.setdefault("TESTING", "1")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.main import app  # noqa: E402

    routes: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        routes.add(normalise(strip_api_prefix(path)))
    return routes


def next_routes(frontend: Path) -> set[str]:
    """Next route handlers: app/api/**/route.ts → the URL path it serves."""
    routes: set[str] = set()
    api_dir = frontend / "app" / "api"
    if not api_dir.is_dir():
        return routes
    for handler in api_dir.rglob("route.ts"):
        rel = handler.relative_to(frontend / "app").parent
        segments = []
        for seg in rel.parts:
            if seg.startswith("(") and seg.endswith(")"):
                continue  # route group — not part of the URL
            if seg.startswith("[[...") or seg.startswith("[..."):
                # Catch-all / optional catch-all: matches any depth. Missing
                # this made every enterprise call look like a 404, because
                # app/api/enterprise/[...path]/route.ts proxies all of them.
                segments.append("**")
            elif seg.startswith("["):
                segments.append("*")  # [id]
            else:
                segments.append(seg)
        routes.add(normalise("/" + "/".join(segments)))
    return routes


def scan_frontend(frontend: Path):
    """Yield (file, line, path, target) for every API call site found."""
    for path in frontend.rglob("*.ts*"):
        if path.suffix not in (".ts", ".tsx"):
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(frontend)
        for lineno, line in enumerate(lines, 1):
            # Comment lines are prose, not call sites. A comment explaining
            # *why* a call was repointed usually quotes the old, dead path —
            # scanning it reports the very bug the comment documents as fixed.
            # Only whole-line comments are skipped, so a trailing `// note`
            # after real code still leaves the code visible, and a `//` inside
            # a URL is never mistaken for one.
            if COMMENT_LINE.match(line):
                continue
            for pattern in BACKEND_PATTERNS:
                for m in pattern.finditer(line):
                    yield rel, lineno, m.group(1), "backend"
            for m in NEXT_PATTERN.finditer(line):
                yield rel, lineno, m.group(1), "next"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend", default="../booppa-nextjs")
    args = parser.parse_args()

    frontend = Path(args.frontend).resolve()
    if not frontend.is_dir():
        # The backend CI job may run without the sibling repo checked out.
        # Skipping loudly beats failing a build for a missing input, but this
        # must never be silent — a permanently skipped check is not a check.
        print(f"SKIP: frontend not found at {frontend}; contract check did not run.")
        return 0

    backend = backend_routes()
    nextjs = next_routes(frontend)
    endpoints = load_endpoints(frontend)
    print(f"Backend routes: {len(backend)}   Next route handlers: {len(nextjs)}   "
          f"endpoints constants: {len(endpoints)}")

    failures = []
    checked = 0
    unresolvable = 0
    for rel, lineno, raw, target in scan_frontend(frontend):
        checked += 1
        # Normalise BEFORE stripping the mount prefix: `/api/v1${p}` must become
        # `/api/v1/*`, not be mis-split on the literal `$`.
        resolved = ENDPOINT_REF.sub(lambda m: endpoints.get(m.group(1), "${?}"), raw)
        # Normalise BEFORE stripping the mount prefix: `/api/v1${p}` must become
        # `/api/v1/*`, not be mis-split on the literal `$`.
        path = normalise(QUERY_SUFFIX.sub("", resolved))

        if target == "next" and not served_by_next(path, nextjs):
            # A bare relative /api/… with no Next handler falls through to a
            # next.config.js rewrite, which sends it to the backend. Retarget
            # rather than reporting a 404 that does not happen.
            if rewritten_to_backend(path):
                target = "backend"
            else:
                failures.append((rel, lineno, raw, path, "Next route handler"))
                continue

        if target == "backend":
            candidate = normalise(strip_api_prefix(path))
            known, label = backend, "backend"
        else:
            continue  # served by a Next handler

        if is_unresolvable(candidate):
            # A catch-all proxy (`app/api/vendor/[...path]`) or an unresolved
            # constant. There is no concrete path to check; counted so that a
            # rise in these — the check quietly covering less — is visible.
            unresolvable += 1
            continue

        if candidate in ALLOWLIST or matches(candidate, known):
            continue
        failures.append((rel, lineno, raw, candidate, label))

    print(f"Checked {checked} call sites ({unresolvable} fully dynamic, skipped).")
    if not failures:
        print("OK: every frontend API call resolves to a route that exists.")
        return 0

    print(f"\n{len(failures)} unmatched API path(s):\n")
    for rel, lineno, raw, candidate, label in sorted(failures):
        print(f"  {rel}:{lineno}")
        print(f"      calls {raw}")
        print(f"      no matching {label} for {candidate}")
    print(
        "\nFix the call, add the route, or — only with a reason — add the path "
        "to ALLOWLIST in this script."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
