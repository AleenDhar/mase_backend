"""Pure check engine. No I/O.

Given (1) a list of `ToolCall`s and (2) a `FlowSpec` describing what should
have fired, produce a `Verdict`. Caller wires this to Supabase reads/writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .loader import ToolCall


# --- Matchers --------------------------------------------------------------

@dataclass(frozen=True)
class ToolMatcher:
    """Predicate: which tool calls satisfy a single expected call.

    `tool_names` — accept any of these tool names (handles waterfall + aliases
    like `web_search` / `web_search_with_urls`).
    `arg_predicate` — optional fn(args_dict) -> bool. None means "any args".
    """

    tool_names: tuple[str, ...]
    arg_predicate: Callable[[dict], bool] | None = None

    def matches(self, call: ToolCall) -> bool:
        if call.tool not in self.tool_names:
            return False
        if self.arg_predicate is None:
            return True
        try:
            return bool(self.arg_predicate(call.args))
        except Exception:
            return False


def args_contain(field: str, *needles: str, case_insensitive: bool = True) -> Callable[[dict], bool]:
    """Helper: predicate matching when `args[field]` contains any needle."""
    needles_norm = tuple(n.lower() for n in needles) if case_insensitive else needles

    def _pred(args: dict) -> bool:
        v = args.get(field)
        if not isinstance(v, str):
            return False
        hay = v.lower() if case_insensitive else v
        return any(n in hay for n in needles_norm)

    return _pred


def args_field_equals(field: str, value: Any) -> Callable[[dict], bool]:
    def _pred(args: dict) -> bool:
        return args.get(field) == value
    return _pred


# --- Expectation spec ------------------------------------------------------

@dataclass(frozen=True)
class ExpectedCall:
    """One thing the agent was supposed to do.

    Multiple `matchers` (logical OR) lets one expectation cover aliases or
    waterfall tools. `min_count` is how many distinct calls must satisfy
    *any* matcher to mark the expectation PASS.
    """

    id: str
    description: str
    phase: str
    matchers: tuple[ToolMatcher, ...]
    min_count: int = 1
    severity: str = "expected"  # "expected" | "advisory"

    def count_matches(self, calls: Iterable[ToolCall]) -> list[ToolCall]:
        out: list[ToolCall] = []
        for c in calls:
            if any(m.matches(c) for m in self.matchers):
                out.append(c)
        return out


# Custom checks let a flow plug in logic that can't be expressed as a single
# ExpectedCall — e.g. the C7 7-angle web_search matcher, where each angle
# needs whole-word role-keyword matching against the query string + an
# account-context guard.
CustomCheckFn = Callable[[list[ToolCall], dict], list["CheckResult"]]


@dataclass(frozen=True)
class FlowSpec:
    name: str
    version: str
    project_ids: tuple[str, ...]  # which project_ids map to this flow
    expected: tuple[ExpectedCall, ...]
    custom_checks: tuple[CustomCheckFn, ...] = ()


# --- Verdict shape ---------------------------------------------------------

@dataclass
class CheckResult:
    id: str
    description: str
    phase: str
    severity: str
    status: str  # "pass" | "miss" | "partial"
    expected_min: int
    observed: int
    sample_calls: list[dict] = field(default_factory=list)


@dataclass
class Verdict:
    chat_id: str
    flow: str
    flow_version: str
    project_id: str | None
    total_tool_calls: int
    results: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.status == "pass" for r in self.results if r.severity == "expected")

    @property
    def missed_ids(self) -> list[str]:
        return [r.id for r in self.results if r.status != "pass" and r.severity == "expected"]

    def summary_line(self) -> str:
        passes = sum(1 for r in self.results if r.status == "pass")
        total = len(self.results)
        if self.passed:
            return (
                f"✅ All expected tool calls fired ({passes}/{total} checks, "
                f"{self.total_tool_calls} tool calls observed) — "
                f"{self.flow} v{self.flow_version}"
            )
        missed = self.missed_ids
        return (
            f"⚠️ Verifier flagged {len(missed)} missing call"
            f"{'s' if len(missed) != 1 else ''}: "
            f"{', '.join(missed)} — {self.flow} v{self.flow_version} "
            f"({passes}/{total} checks passed, {self.total_tool_calls} tool calls observed)"
        )


# --- Engine ----------------------------------------------------------------

def _sample(call: ToolCall) -> dict:
    return {
        "seq": call.sequence,
        "tool": call.tool,
        "tool_call_id": call.tool_call_id,
        "args_preview": _truncate_args(call.args),
    }


def _truncate_args(args: dict, limit: int = 240) -> dict:
    out: dict = {}
    for k, v in (args or {}).items():
        s = str(v)
        if len(s) > limit:
            out[k] = s[:limit] + "…"
        else:
            out[k] = v
    return out


def evaluate_flow(
    chat_id: str,
    project_id: str | None,
    calls: list[ToolCall],
    spec: FlowSpec,
    context: dict | None = None,
) -> Verdict:
    """Pure: produce a verdict by evaluating spec against observed calls."""
    ctx = context or {}
    results: list[CheckResult] = []

    for exp in spec.expected:
        matched = exp.count_matches(calls)
        observed = len(matched)
        if observed >= exp.min_count:
            status = "pass"
        elif observed > 0:
            status = "partial"
        else:
            status = "miss"
        results.append(
            CheckResult(
                id=exp.id,
                description=exp.description,
                phase=exp.phase,
                severity=exp.severity,
                status=status,
                expected_min=exp.min_count,
                observed=observed,
                sample_calls=[_sample(c) for c in matched[:3]],
            )
        )

    for custom in spec.custom_checks:
        try:
            results.extend(custom(calls, ctx) or [])
        except Exception as e:
            results.append(
                CheckResult(
                    id=f"custom_check_error_{getattr(custom, '__name__', 'unknown')}",
                    description=f"custom check raised: {e}",
                    phase="?",
                    severity="advisory",
                    status="miss",
                    expected_min=0,
                    observed=0,
                )
            )

    return Verdict(
        chat_id=chat_id,
        flow=spec.name,
        flow_version=spec.version,
        project_id=project_id,
        total_tool_calls=len(calls),
        results=results,
    )


def render_verdict(verdict: Verdict) -> str:
    """Plain-text render — used for chat_messages.content + CLI output."""
    lines = [verdict.summary_line()]
    by_phase: dict[str, list[CheckResult]] = {}
    for r in verdict.results:
        by_phase.setdefault(r.phase, []).append(r)
    for phase, checks in sorted(by_phase.items()):
        lines.append(f"\n  {phase}:")
        for r in checks:
            mark = {"pass": "✓", "partial": "~", "miss": "✗"}.get(r.status, "?")
            lines.append(
                f"    {mark} {r.id:32s}  observed={r.observed} "
                f"min={r.expected_min}  {r.description}"
            )
    if verdict.missed_ids:
        lines.append(
            f"\n  Missed (expected): {', '.join(verdict.missed_ids)}"
        )
    if verdict.notes:
        lines.append("\n  Notes:")
        for n in verdict.notes:
            lines.append(f"    - {n}")
    return "\n".join(lines)


def verdict_to_dict(verdict: Verdict) -> dict:
    """Serialise a Verdict to a JSON-safe dict for Supabase metadata."""
    return {
        "chat_id": verdict.chat_id,
        "flow": verdict.flow,
        "flow_version": verdict.flow_version,
        "project_id": verdict.project_id,
        "total_tool_calls": verdict.total_tool_calls,
        "passed": verdict.passed,
        "missed_ids": verdict.missed_ids,
        "results": [
            {
                "id": r.id,
                "description": r.description,
                "phase": r.phase,
                "severity": r.severity,
                "status": r.status,
                "expected_min": r.expected_min,
                "observed": r.observed,
                "sample_calls": r.sample_calls,
            }
            for r in verdict.results
        ],
        "notes": verdict.notes,
        "version": 1,
    }
