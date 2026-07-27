"""AST guards for bug classes that are syntactically valid but fail silently.

Each of these shipped to production at least once:

  1. A method defined twice in one class — Python keeps the second, the first
     becomes unreachable, and callers silently get the wrong behaviour
     (ReportRepository.get_by_stripe_session_id, 2026-07).
  2. A local `from x import Y` placed *after* a use of module-level `Y` in the
     same function — makes Y local for the whole body, so the earlier use raises
     UnboundLocalError (stripe_webhook.py twice; pdpa_monitor_monthly_rescan_task,
     which killed every monthly PDPA rescan until 2026-07).
  3. `cast(<json column>["k"], String) == "value"` — `json` (not `jsonb`) columns
     render CAST(data -> 'k' AS VARCHAR), which yields JSON-quoted text and never
     matches. The query returns nothing, with no error.
  4. A bare `await ….send_html_email(...)` statement — the call returns False on
     provider rejection instead of raising, so an ignored return means a silently
     undelivered email.

These run over `app/` with stdlib ast only; no DB, no network.
"""

import ast
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def _modules():
    for path in sorted(APP.rglob("*.py")):
        yield path, ast.parse(path.read_text())


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(APP.parent))


def _own_nodes(fn):
    """Nodes belonging to `fn` itself, excluding nested function bodies."""
    nested = {
        id(x)
        for inner in ast.walk(fn)
        if inner is not fn and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
        for x in ast.walk(inner)
    }
    for node in ast.walk(fn):
        if node is not fn and id(node) not in nested:
            yield node


def test_no_duplicate_definitions():
    """No name defined twice in the same class/module scope."""
    problems = []
    for path, tree in _modules():
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.Module, ast.ClassDef)):
                continue
            owner = getattr(scope, "name", "<module>")
            seen: dict[str, int] = {}
            for child in scope.body:
                names = []
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names = [child.name]
                elif isinstance(child, ast.Assign):
                    names = [t.id for t in child.targets if isinstance(t, ast.Name)]
                for name in names:
                    if name in seen:
                        problems.append(
                            f"{_rel(path)}:{child.lineno} redefines {owner}.{name} "
                            f"(first at line {seen[name]}) — the earlier one is dead code"
                        )
                    seen[name] = child.lineno
    assert not problems, "Duplicate definitions:\n" + "\n".join(problems)


def test_no_duplicate_dict_or_set_literal_keys():
    problems = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            elts = None
            if isinstance(node, ast.Dict):
                elts = node.keys
            elif isinstance(node, ast.Set):
                elts = node.elts
            if not elts:
                continue
            seen: dict[object, int] = {}
            for elt in elts:
                if not isinstance(elt, ast.Constant) or not isinstance(
                    elt.value, (str, int)
                ):
                    continue
                if elt.value in seen:
                    problems.append(
                        f"{_rel(path)}:{elt.lineno} duplicate literal key "
                        f"{elt.value!r} (first at line {seen[elt.value]})"
                    )
                seen[elt.value] = elt.lineno
    assert not problems, "Duplicate literal keys:\n" + "\n".join(problems)


def test_no_use_before_local_reimport():
    """A local import must not shadow a module-level name already used above it."""
    problems = []
    for path, tree in _modules():
        top = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                top.update(a.asname or a.name.split(".")[0] for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                top.add(node.name)
            elif isinstance(node, ast.Assign):
                top.update(t.id for t in node.targets if isinstance(t, ast.Name))

        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            own = list(_own_nodes(fn))
            local_imports: dict[str, int] = {}
            for node in own:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        name = alias.asname or alias.name.split(".")[0]
                        if name in top:
                            local_imports.setdefault(name, node.lineno)
            if not local_imports:
                continue
            for node in own:
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in local_imports
                    and node.lineno < local_imports[node.id]
                ):
                    problems.append(
                        f"{_rel(path)}:{node.lineno} {fn.name}() uses {node.id!r} "
                        f"before re-importing it locally at line "
                        f"{local_imports[node.id]} — UnboundLocalError at runtime"
                    )
    assert not problems, "Use-before-local-reimport:\n" + "\n".join(sorted(set(problems)))


def _plain_json_columns() -> set[str]:
    """Attribute names declared as `Column(JSON…)` — not JSONB — in models.py."""
    tree = ast.parse((APP / "core" / "models.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Name) and func.id == "Column"):
            continue
        for arg in node.value.args:
            if isinstance(arg, ast.Name) and arg.id == "JSON":
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_no_cast_on_plain_json_column():
    """`json` columns need .as_string() (`->>`); cast() of `->` never matches."""
    json_cols = _plain_json_columns()
    assert json_cols, "expected to find Column(JSON) declarations in models.py"

    problems = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "cast"
                and node.args
            ):
                continue
            target = node.args[0]
            if isinstance(target, ast.Subscript) and isinstance(
                target.value, ast.Attribute
            ):
                attr = target.value.attr
                if attr in json_cols:
                    problems.append(
                        f"{_rel(path)}:{node.lineno} cast() on plain-json column "
                        f"{attr!r} — yields JSON-quoted text and never matches; "
                        f"use .as_string()"
                    )
    assert not problems, "cast() on json column:\n" + "\n".join(problems)


# Sites still ignoring the send_html_email return value. Must stay empty — a
# False return is a silently undelivered email, never an exception.
UNCHECKED_EMAIL_ALLOWLIST: set[str] = set()


def test_send_html_email_return_value_is_checked():
    problems = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Expr):
                continue
            value = node.value
            if isinstance(value, ast.Await):
                value = value.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "send_html_email"
            ):
                site = f"{_rel(path)}:{node.lineno}"
                if site not in UNCHECKED_EMAIL_ALLOWLIST:
                    problems.append(
                        f"{site} discards the send_html_email() return value — "
                        f"it returns False on provider rejection without raising"
                    )
    assert not problems, "Unchecked send_html_email:\n" + "\n".join(problems)


# ── 5. Hand-rolled PDPA findings fallback chains ─────────────────────────────
#
# Findings live at `assessment_data["booppa_report"]["detailed_findings"]`, but
# older paths wrote them at the top level, under `risk_assessment`, or as
# `violations` — and some AI paths returned a dict instead of a list. Every
# consumer that hand-rolls its own `a.get(x) or b.get(y) or []` chain gets a
# *different subset* right. `_fulfill_pdpa` had a 3-key copy missing
# `risk_assessment.findings`, `violations` and the dict->list coercion, so the
# PDPA PDF resolved [] and printed "found no PDPA violations" above a score
# table full of Non-Compliant rows (report 22fb2871, sold at SGD 299).
#
# `app/services/pdpa_findings.py::resolve_pdpa_findings` is the single source of
# truth. Nothing else may reimplement the ladder.

_FINDING_KEYS = {"detailed_findings", "findings", "violations"}

# The resolver itself is the one legitimate implementation.
_RESOLVER_MODULE = "app/services/pdpa_findings.py"


def _findings_get_call(node) -> bool:
    """True for `<expr>.get("detailed_findings" | "findings" | "violations")`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) >= 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in _FINDING_KEYS
    )


def test_no_hand_rolled_pdpa_findings_fallback():
    """No `or`-chain over two or more findings keys outside the resolver."""
    problems = []
    for path, tree in _modules():
        if _rel(path).endswith(_RESOLVER_MODULE):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
                continue
            hits = [v for v in node.values if _findings_get_call(v)]
            if len(hits) >= 2:
                problems.append(
                    f"{_rel(path)}:{node.lineno} hand-rolls a PDPA findings "
                    f"fallback chain over "
                    f"{[h.args[0].value for h in hits]} — call "
                    f"resolve_pdpa_findings(assessment_data) instead; a partial "
                    f"copy resolves [] on legacy reports and renders them clean"
                )
    assert not problems, "Hand-rolled findings chain:\n" + "\n".join(problems)
