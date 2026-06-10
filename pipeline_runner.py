"""
Multi-phase pipeline runner — POST /api/run-pipeline orchestrator.

Spec (locked-in with caller):
  - Background execution. HTTP returns within ~100ms with {ok:true, started:true}.
  - In-memory idempotency lock keyed on chat_id; released in finally.
  - Per-phase hard cap = 900s; single phase failure -> phase_error row, continue.
  - Per-request Supabase client is built from the body's url + service_role JWT
    (NOT from env). All writes bypass RLS.
  - The orchestrator only loops + tags. LLM work is reused via the existing
    /api/chat endpoint (passed in as agent_chat_url).
  - Only assistant text from `token` and `final` SSE events is accumulated as
    phase output (used as Prior Phase Outputs for later phases).
  - After each phase's stream closes, tag prior assistant rows with the phase
    metadata BEFORE moving on to the next phase. Do not parallelize.
  - Skip tagging rows whose metadata.phase.position already equals this phase
    (idempotent re-tag).
  - DELETE /api/run-pipeline/{chat_id} pops the lock manually (escape hatch).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import certifi
import httpx
import ssl
import traceback
from urllib.parse import urlparse, urlunparse

# Belt-and-braces for any side-channel library (e.g. supabase-py via requests,
# postgrest-py via httpx with its own default context) that builds its own
# SSL context off SSL_CERT_FILE / REQUESTS_CA_BUNDLE. Set BEFORE building our
# own context so the env is consistent. setdefault keeps an operator override.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

# Pre-built SSL context using certifi's CA bundle. Some Replit/Nix images
# don't ship a system CA trust store, so httpx's default verification can
# fail with "unable to get local issuer certificate".
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Boot diagnostics — print to stdout so they land in the workflow log on
# import. Helps prove certifi is actually wired up at runtime.
print(f"[pipeline] SSL_CERT_FILE env      = {os.environ.get('SSL_CERT_FILE')}",
      flush=True)
print(f"[pipeline] REQUESTS_CA_BUNDLE env = {os.environ.get('REQUESTS_CA_BUNDLE')}",
      flush=True)
print(f"[pipeline] certifi.where()        = {certifi.where()}", flush=True)
print(f"[pipeline] _SSL_CONTEXT cert_store_stats = "
      f"{_SSL_CONTEXT.cert_store_stats()}", flush=True)
print(f"[pipeline] ssl.OPENSSL_VERSION    = {ssl.OPENSSL_VERSION}", flush=True)

# Loopback override. When PIPELINE_USE_LOOPBACK=1, the orchestrator rewrites
# any agent_chat_url to the local FastAPI port — bypassing Replit's edge TLS
# gateway entirely. Useful for diagnosing whether an SSL failure lives in our
# Python stack or in the upstream gateway. Off by default.
_USE_LOOPBACK = os.environ.get("PIPELINE_USE_LOOPBACK", "").strip() in ("1", "true", "yes")
_LOOPBACK_BASE = os.environ.get("PIPELINE_LOOPBACK_BASE", "http://127.0.0.1:5000")
if _USE_LOOPBACK:
    print(f"[pipeline] LOOPBACK ENABLED -> {_LOOPBACK_BASE}", flush=True)


def _maybe_loopback(url: str) -> str:
    """Rewrite scheme+host of `url` to _LOOPBACK_BASE, preserving path/query."""
    if not _USE_LOOPBACK:
        return url
    try:
        parsed = urlparse(url)
        base = urlparse(_LOOPBACK_BASE)
        return urlunparse((
            base.scheme, base.netloc, parsed.path, parsed.params,
            parsed.query, parsed.fragment,
        ))
    except Exception:
        return url
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from supabase import create_client

log = logging.getLogger("pipeline_runner")
log.setLevel(logging.INFO)

router = APIRouter()

# Default per-phase wall-clock cap. Env-overridable; per-request override on
# RunPipelineRequest.phase_timeout_seconds wins over both. Raised from the
# old hard-coded 900s after chat d1d9260d hit the cap ~9s after pushing
# Lead #1 of 5 and the rest of the batch never went out.
try:
    PHASE_TIMEOUT_SECONDS = int(os.environ.get("PHASE_TIMEOUT_SECONDS", "1800"))
except ValueError:
    PHASE_TIMEOUT_SECONDS = 1800
print(f"[pipeline] PHASE_TIMEOUT_SECONDS (default) = {PHASE_TIMEOUT_SECONDS}s",
      flush=True)

# Module-level idempotency state. Cleared on process restart, which is what we
# want — a restart implicitly unlocks any chat that was mid-pipeline.
_active_pipelines: set[str] = set()
_active_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class PhaseSpec(BaseModel):
    id: str
    position: int
    name: str
    model_id: str
    system_prompt: str
    enabled: bool = True


class PriorPhaseOutput(BaseModel):
    phase: Dict[str, Any]
    content: str


class RunPipelineRequest(BaseModel):
    chat_id: str
    project_id: str
    shared_system_prefix: str = ""
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    phases: List[PhaseSpec] = Field(default_factory=list)
    api_keys: Dict[str, str] = Field(default_factory=dict)
    agent_chat_url: str
    supabase_url: str
    supabase_service_key: str
    # Amendment 1: per-phase rerun support. When non-empty, the orchestrator
    # pre-populates its accumulator so the first phase in `phases` sees these
    # entries in its "## Prior Phase Outputs" section exactly as if they had
    # just run. Default [] = full pipeline run from scratch.
    prior_phase_outputs: List[PriorPhaseOutput] = Field(default_factory=list)
    # Amendment 2: cooperative stop. When set, the orchestrator polls
    # automation_tasks.stop_requested between phases and exits cleanly on True.
    # Omit for regular chat runs (no task row exists).
    task_id: Optional[str] = None
    # Task #32 — phase-skip alarm. When the caller knows the pipeline shape it
    # is launching (e.g. ABM = 6 phases), it can declare the minimum number of
    # enabled phases it expects to run. If fewer are enabled, the runner
    # rejects the request up front and writes a loud `type='error'` row so the
    # situation is visible without log-diving. Omit (or set to 0) to disable.
    # Falls back to env var PIPELINE_MIN_PHASES if not provided on the request.
    expected_phases_min: Optional[int] = None
    # Optional human label for the alarm message (e.g. "ABM", "outreach").
    pipeline_shape: Optional[str] = None
    # Task #33 — per-phase wall-clock cap override. None = use module default
    # (env PHASE_TIMEOUT_SECONDS, fallback 1800s). Values <= 0 are treated as
    # "use default" too. The cap applies independently per phase, not per run.
    phase_timeout_seconds: Optional[int] = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _require_dispatch_secret(authorization: Optional[str]) -> None:
    expected = os.environ.get("DISPATCH_SECRET", "")
    if not expected:
        raise HTTPException(status_code=503, detail="DISPATCH_SECRET not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid Bearer token")


# ---------------------------------------------------------------------------
# Supabase helpers (per-request client — never reuse the global one)
# ---------------------------------------------------------------------------
def _sb_client(req: RunPipelineRequest):
    return create_client(req.supabase_url, req.supabase_service_key)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_INSERT_RETRY_ATTEMPTS = 3
_INSERT_RETRY_BASE_DELAY = 0.25  # seconds; backoff: 0.25, 0.5, 1.0
_chats_updated_at_ok = True


def _bump_chat_updated_at(sb, chat_id: str) -> None:
    """Advance the parent chats.updated_at so the client gets a freshness
    signal even if a Realtime event was dropped. Best-effort: disables itself
    after one quiet log if the column is absent in this schema."""
    global _chats_updated_at_ok
    if not _chats_updated_at_ok or not chat_id:
        return
    try:
        sb.table("chats").update(
            {"updated_at": _now_iso()}).eq("id", chat_id).execute()
    except Exception as exc:
        msg = str(exc).lower()
        if "updated_at" in msg or "column" in msg or "schema" in msg:
            _chats_updated_at_ok = False
            log.info("[pipeline] chats.updated_at bump disabled "
                     "(column unavailable): %s", exc)
        else:
            log.warning("[pipeline] chats.updated_at bump failed chat=%s: %s",
                        chat_id, exc)


def _insert_row(sb, chat_id: str, role: str, msg_type: str,
                content: str, metadata: Dict[str, Any]) -> bool:
    """Insert a chat_messages row with a short bounded retry. Transient
    Supabase blips are retried; an insert that fails every attempt is logged
    loudly with chat id + row type so dropped writes are visible. Returns True
    only on a confirmed insert (response carries data), else False — so callers
    can guarantee a terminal row was actually persisted."""
    row = {
        "chat_id": chat_id,
        "role": role,
        "type": msg_type,
        "content": content,
        "metadata": metadata,
        "created_at": _now_iso(),
    }
    for attempt in range(1, _INSERT_RETRY_ATTEMPTS + 1):
        try:
            resp = sb.table("chat_messages").insert(row).execute()
            # postgrest raises on hard errors, but guard the rare success-shaped
            # response that carries an error / no inserted row.
            if getattr(resp, "data", None) is None:
                raise RuntimeError(
                    f"insert returned no data (error="
                    f"{getattr(resp, 'error', None)})")
            _bump_chat_updated_at(sb, chat_id)
            return True
        except Exception as exc:
            if attempt < _INSERT_RETRY_ATTEMPTS:
                delay = _INSERT_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                log.warning("[pipeline] insert_row failed chat=%s type=%s "
                            "attempt %d/%d: %s — retrying in %.2fs",
                            chat_id, msg_type, attempt,
                            _INSERT_RETRY_ATTEMPTS, exc, delay)
                time.sleep(delay)
            else:
                log.error("[pipeline] insert_row DROPPED chat=%s type=%s "
                          "after %d attempts: %s",
                          chat_id, msg_type, _INSERT_RETRY_ATTEMPTS, exc)
    return False


def _tag_phase_rows(sb, chat_id: str, phase_start_iso: str,
                    phase_meta: Dict[str, Any]) -> int:
    """SELECT assistant rows since phase_start_iso, merge phase metadata into
    rows that aren't already tagged with this phase position. Returns the
    number of rows that were updated."""
    pos = phase_meta.get("position")
    try:
        res = (
            sb.table("chat_messages")
              .select("id,metadata")
              .eq("chat_id", chat_id)
              .eq("role", "assistant")
              .gte("created_at", phase_start_iso)
              .execute()
        )
        rows = res.data or []
    except Exception as exc:
        log.warning("[pipeline] tag SELECT failed chat=%s: %s", chat_id, exc)
        return 0

    updated = 0
    for row in rows:
        raw_md = row.get("metadata")
        # Supabase can return jsonb columns as either a dict (parsed) or a
        # JSON-encoded string (when the column was written as text or by a
        # different client). Normalize to dict; on parse failure, treat as
        # empty so we still tag the row instead of crashing the whole phase.
        if isinstance(raw_md, str):
            try:
                md = json.loads(raw_md) if raw_md else {}
            except Exception:
                md = {}
        elif isinstance(raw_md, dict):
            md = raw_md
        else:
            md = {}
        if not isinstance(md, dict):
            md = {}

        existing_phase = md.get("phase")
        if isinstance(existing_phase, str):
            try:
                existing_phase = json.loads(existing_phase)
            except Exception:
                existing_phase = {}
        if not isinstance(existing_phase, dict):
            existing_phase = {}
        if existing_phase.get("position") == pos:
            continue  # already tagged with this phase — idempotent skip

        new_md = dict(md)
        new_md["phase"] = phase_meta
        try:
            sb.table("chat_messages").update({"metadata": new_md}).eq("id", row["id"]).execute()
            updated += 1
        except Exception as exc:
            log.warning("[pipeline] tag UPDATE failed row=%s: %s",
                        row.get("id"), exc)
    return updated


# ---------------------------------------------------------------------------
# Per-phase SSE consumer
# ---------------------------------------------------------------------------
async def _stream_phase(agent_chat_url: str, payload: Dict[str, Any],
                        read_timeout: Optional[float] = None) -> str:
    """POST agent_chat_url with stream=True, consume SSE, return accumulated
    assistant text. Only `token` and `final` events contribute. `final` is
    preferred when its content is longer than the token-stream accumulation.
    `read_timeout` controls the httpx per-read timeout; defaults to the
    module-level PHASE_TIMEOUT_SECONDS."""
    token_acc = ""
    final_text = ""

    timeout = httpx.Timeout(
        connect=30.0,
        read=float(read_timeout if read_timeout and read_timeout > 0
                   else PHASE_TIMEOUT_SECONDS),
        write=30.0,
        pool=30.0,
    )

    # Authenticate the internal self-call. `/api/chat` is currently on the
    # public allowlist so this is belt-and-suspenders, but sending the token
    # keeps the call working if that allowlist is later tightened. Mirrors
    # server._api_auth_token() (API_AUTH_TOKEN wins, else DISPATCH_SECRET).
    headers: Dict[str, str] = {}
    _auth_token = (os.environ.get("API_AUTH_TOKEN", "")
                   or os.environ.get("DISPATCH_SECRET", ""))
    if _auth_token:
        headers["Authorization"] = f"Bearer {_auth_token}"

    # Retry transient pre-stream failures (gateway/connection hiccups) before
    # any tokens are consumed. We never retry once streaming has started, and
    # never retry 4xx (auth/policy errors don't self-heal), so an expensive
    # agent phase can't be double-run.
    _RETRYABLE_STATUS = (429, 500, 502, 503, 504)
    _MAX_ATTEMPTS = 3

    async with httpx.AsyncClient(timeout=timeout, verify=_SSL_CONTEXT) as client:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with client.stream("POST", agent_chat_url, json=payload,
                                         headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        if (resp.status_code in _RETRYABLE_STATUS
                                and attempt < _MAX_ATTEMPTS):
                            wait = 1.5 * attempt
                            log.warning(
                                "[pipeline] agent_chat_url %s (attempt %d/%d) "
                                "-> retrying in %.1fs",
                                resp.status_code, attempt, _MAX_ATTEMPTS, wait)
                            await asyncio.sleep(wait)
                            continue
                        raise RuntimeError(
                            f"agent_chat_url returned {resp.status_code}: "
                            f"{body[:500]!r}"
                        )
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[len("data:"):].strip()
                        if not data_str:
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        etype = event.get("type")
                        if etype == "token":
                            token_acc += event.get("content", "") or ""
                        elif etype == "final":
                            final_text = event.get("content", "") or ""
                        # All other event types (thinking, tool_call,
                        # tool_result, status, ping, error, done, cancelled,
                        # chat_id, etc.) are ignored for phase-output
                        # accumulation.
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Connection never established → no agent work done → safe to
                # retry the POST.
                if attempt < _MAX_ATTEMPTS:
                    wait = 1.5 * attempt
                    log.warning(
                        "[pipeline] agent_chat_url connect error %s "
                        "(attempt %d/%d) -> retrying in %.1fs",
                        type(exc).__name__, attempt, _MAX_ATTEMPTS, wait)
                    await asyncio.sleep(wait)
                    continue
                raise

    if final_text and len(final_text) >= len(token_acc):
        return final_text
    return token_acc


# ---------------------------------------------------------------------------
# Per-phase orchestration (with 900s hard cap)
# ---------------------------------------------------------------------------
async def _run_phase(sb, req: RunPipelineRequest, phase: PhaseSpec,
                     index: int, total: int,
                     prior_outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run a single phase. Returns a dict with:
        text:    accumulated assistant text (empty on failure)
        outcome: "ok" | "timeout" | "error"
        error:   short error string ("" on ok)
    """
    # Defensive: callers sometimes send an empty phase.name. The UI uses
    # this as the cell title, so fall back to "Phase {position}" rather
    # than rendering an empty string.
    safe_name = (phase.name or "").strip() or f"Phase {phase.position}"
    phase_meta = {
        "index": index,
        "total": total,
        "position": phase.position,
        "name": safe_name,
        "model_id": phase.model_id,
    }
    _insert_row(sb, req.chat_id, "assistant", "status", "", {
        "phase": phase_meta,
        "kind": "phase_start",
    })
    phase_start_iso = _now_iso()
    # Snapshot cumulative chat_usage so we can compute this phase's delta
    # (tokens + USD) after the phase finishes. chat_usage is keyed by chat_id
    # not by phase, so deltas are the only way to attribute cost per phase.
    usage_start = _snapshot_chat_usage(sb, req.chat_id)
    # Bump last_phase_* so the automations UI shows which phase is in flight.
    if req.task_id:
        _mark_task_phase_progress(sb, req.task_id, phase_meta)

    # Build per-phase system prompt
    parts: List[str] = []
    if req.shared_system_prefix:
        parts.append(req.shared_system_prefix.strip())
    if prior_outputs:
        parts.append("## Prior Phase Outputs")
        for prev in prior_outputs:
            pm = prev["phase"]
            parts.append(f"### Phase {pm['position']}: {pm['name']}")
            parts.append(prev["content"])
    parts.append(phase.system_prompt.strip())
    # Three cases for the phase-context footer:
    #   total == 1  -> single-phase project. Skip the orchestrator ceremony
    #                  entirely; the agent should just run the work end-to-end
    #                  exactly like a normal chat. If we tell it "Phase 1 of 1"
    #                  it writes a "Pending: Phase 2..." footer and stops mid-
    #                  flow waiting for a follow-up call that will never come.
    #   i  == total -> last phase of a multi-phase run. Tell the agent this is
    #                  the FINAL phase so it completes the end-to-end task and
    #                  doesn't promise a "next phase".
    #   else        -> intermediate phase. Old behaviour: do only this phase,
    #                  orchestrator will call again for the next one.
    is_single_phase_run = total == 1
    is_final_phase = index == total
    if is_single_phase_run:
        pass  # no orchestration footer at all — behave like a normal chat
    elif is_final_phase:
        parts.append(
            f"## Pipeline Context\n"
            f"This is the FINAL phase ({phase.position} of {total}: "
            f"{phase.name}). The prior phases have already produced their "
            f"outputs (provided above). Complete this phase end-to-end and "
            f"finish the task — there is no next phase to wait for."
        )
    else:
        parts.append(
            f"## Pipeline Context\n"
            f"This is phase {phase.position} of {total} ({phase.name}).\n"
            f"You are being driven by an external orchestrator that will call "
            f"you once per phase. Do **only** the work for phase "
            f"{phase.position} in this turn. Do NOT preview, start, or attempt "
            f"phases {phase.position + 1}..{total}. After you produce the "
            f"Phase {phase.position} state block / deliverable, stop. The "
            f"orchestrator will issue a separate call for the next phase with "
            f"its own scoped instructions and the outputs you produce here "
            f"pre-loaded."
        )
    composed_system = "\n\n".join(p for p in parts if p)

    # Per-phase user message: keep the original conversation intact, but append
    # an explicit, phase-scoped instruction so the agent doesn't barrel through
    # multiple phases inside a single call (which mis-tags rows downstream).
    # Strip any prior orchestrator control prompts so reruns / long histories
    # don't accumulate conflicting "[Pipeline Orchestrator]" instructions.
    _ORCH_TAG = "[Pipeline Orchestrator"
    phase_messages = [
        m for m in req.messages
        if not (
            isinstance(m, dict)
            and m.get("role") == "user"
            and isinstance(m.get("content"), str)
            and m["content"].lstrip().startswith(_ORCH_TAG)
        )
    ]
    # Only append the orchestrator control message for multi-phase runs.
    # Single-phase = act like a normal chat (no extra control prompt).
    if is_single_phase_run:
        pass
    elif is_final_phase:
        phase_messages.append({
            "role": "user",
            "content": (
                f"[Pipeline Orchestrator — FINAL phase {phase.position} of "
                f"{total}]\n"
                f"Execute Phase {phase.position}: {phase.name} now, using the "
                f"prior-phase outputs already in your system prompt. This is "
                f"the LAST phase — complete the end-to-end task and finish. "
                f"Do not promise a follow-up phase."
            ),
        })
    else:
        phase_messages.append({
            "role": "user",
            "content": (
                f"[Pipeline Orchestrator — phase {phase.position} of {total}]\n"
                f"Execute **only Phase {phase.position}: {phase.name}** right "
                f"now, using the prior-phase outputs already provided in your "
                f"system prompt. Produce the Phase {phase.position} state "
                f"block / deliverable and then STOP. Do not begin Phase "
                f"{phase.position + 1}. I will call you again for the next "
                f"phase."
            ),
        })

    payload = {
        "messages": phase_messages,
        "system_prompt": composed_system,
        "model": phase.model_id,
        "stream": True,
        "chat_id": req.chat_id,
        "project_id": req.project_id,
        "api_keys": req.api_keys,
    }

    phase_text = ""
    outcome = "ok"
    error_str = ""
    effective_url = _maybe_loopback(req.agent_chat_url)
    if effective_url != req.agent_chat_url:
        log.info("[pipeline] phase %d loopback override: %s -> %s",
                 index, req.agent_chat_url, effective_url)
    # Resolve effective per-phase wall-clock cap. Per-request override wins
    # over the module default (which is itself env-overridable).
    eff_timeout = req.phase_timeout_seconds
    if not eff_timeout or eff_timeout <= 0:
        eff_timeout = PHASE_TIMEOUT_SECONDS
    log.info("[pipeline] chat=%s phase %d timeout=%ds",
             req.chat_id, index, eff_timeout)
    try:
        phase_text = await asyncio.wait_for(
            _stream_phase(effective_url, payload, read_timeout=eff_timeout),
            timeout=eff_timeout,
        )
    except asyncio.TimeoutError:
        outcome = "timeout"
        error_str = f"timeout after {eff_timeout}s"
        _insert_row(sb, req.chat_id, "assistant", "error",
                    f"Phase {phase.position} ({phase.name}) timed out after "
                    f"{eff_timeout}s.",
                    {"phase": phase_meta, "kind": "phase_error",
                     "error": "timeout",
                     "timeout_seconds": eff_timeout})
        # Task #33 bug #2 — pipeline_runner.wait_for only closes the local
        # httpx connection; the /api/chat handler running on the same process
        # (registered in server._running_tasks[chat_id]) keeps streaming
        # tokens, calling tools, and burning $$ until it naturally ends. On
        # chat d1d9260d we saw ~60s of post-timeout tool calls. Force the
        # in-process agent task to cancel, then yield the event loop a few
        # times so the cancellation lands before we move on to _tag_phase_rows
        # / the next phase. Lazy import to avoid a server <-> pipeline_runner
        # circular import at module load.
        try:
            from server import cancel_running_chat as _cancel  # noqa: WPS433
            cancelled = _cancel(req.chat_id)
            log.info("[pipeline] chat=%s phase %d timeout: in-process agent "
                     "cancel=%s", req.chat_id, index, cancelled)
        except Exception as cancel_exc:
            log.warning("[pipeline] chat=%s phase %d cancel hook failed: %s",
                        req.chat_id, index, cancel_exc)
        # Give the cancelled task a brief window to actually unwind so its
        # in-flight tool_call / tool_result rows aren't written into the
        # NEXT phase's time window.
        for _ in range(10):  # ~1s total
            await asyncio.sleep(0.1)
    except Exception as exc:
        # Capture the full traceback so we can tell WHICH SSL context blew up:
        # the orchestrator -> /api/chat call, the agent calling Anthropic/OpenAI
        # downstream, or a Supabase write. Dump to stderr (workflow logs) AND
        # persist a truncated copy in the marker row's metadata.
        outcome = "error"
        error_str = f"{type(exc).__name__}: {exc}"[:500]
        tb = traceback.format_exc()
        log.error(
            "[pipeline] phase %d FAILED with %s: %s\nURL=%s\n%s",
            index, type(exc).__name__, exc, effective_url, tb,
        )
        _insert_row(sb, req.chat_id, "assistant", "error",
                    f"Phase {phase.position} ({phase.name}) error: "
                    f"{type(exc).__name__}: {exc}",
                    {"phase": phase_meta, "kind": "phase_error",
                     "error": str(exc)[:500],
                     "error_type": type(exc).__name__,
                     "agent_chat_url": effective_url,
                     "traceback": tb[-4000:]})

    # Tag any assistant rows written during this phase BEFORE the next phase
    # starts streaming (spec calls this out explicitly — do not parallelize).
    _tag_phase_rows(sb, req.chat_id, phase_start_iso, phase_meta)

    # Circuit-breaker detection (added 2026-05-22 after chat 8359d7a6).
    # When server.py's auto-continuation circuit-breaker fires
    # (budget_continuations / budget_cost / budget_time) the terminal row
    # carries metadata.status='budget_exhausted'. Since Task #49 server.py
    # writes EXACTLY ONE terminal row per turn: a `type='final'` row (budget
    # note appended) when the agent produced text — the common case, a long
    # "thinking process" dump — or a `type='error'` row when it produced none.
    # Either way the empty-text check below would otherwise leave outcome='ok'
    # and the driver would mark the pipeline `pipeline_complete`. That lies to
    # the UI — the run actually died mid-phase. Scan BOTH terminal row types in
    # the phase window and downgrade outcome to 'error' if a breaker is found.
    if outcome == "ok":
        try:
            res = (
                sb.table("chat_messages")
                .select("metadata, content")
                .eq("chat_id", req.chat_id)
                .in_("type", ["error", "final"])
                .gte("created_at", phase_start_iso)
                .order("created_at", desc=False)
                .execute()
            )
            for row in (res.data or []):
                md = row.get("metadata") or {}
                if isinstance(md, str):
                    try:
                        md = json.loads(md)
                    except Exception:
                        md = {}
                if (md.get("status") == "budget_exhausted"
                        or (md.get("terminal") or {}).get("reason") in
                            ("budget_cost", "budget_time", "budget_continuations")):
                    reason = (md.get("terminal") or {}).get("reason", "budget_exhausted")
                    outcome = "error"
                    error_str = f"circuit_breaker:{reason}"
                    log.warning(
                        "[pipeline] chat=%s phase %d hit circuit-breaker %s "
                        "during phase window — downgrading outcome ok -> error",
                        req.chat_id, index, reason,
                    )
                    # Mark the row in pipeline state so the driver writes
                    # pipeline_partial / pipeline_failed instead of
                    # pipeline_complete.
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[pipeline] chat=%s phase %d breaker-scan failed (non-fatal): %s",
                req.chat_id, index, exc,
            )

    # Compute this phase's usage delta (tokens + USD) from the cumulative
    # chat_usage row. server.py upserts chat_usage AFTER the SSE stream
    # closes, so when _stream_phase returns the upsert may not have landed
    # yet. Poll for up to ~5s waiting for either total_tokens or cost_usd
    # to change vs usage_start; fall back to whatever we have on timeout.
    usage_end = usage_start
    if phase_text and phase_text.strip():
        for _ in range(20):  # 20 * 0.25s = 5s max
            await asyncio.sleep(0.25)
            usage_end = _snapshot_chat_usage(sb, req.chat_id)
            if (usage_end["total_tokens"] != usage_start["total_tokens"]
                    or usage_end["cost_usd"] != usage_start["cost_usd"]):
                break
    phase_usage = {
        "input_tokens": max(0, usage_end["input_tokens"] - usage_start["input_tokens"]),
        "output_tokens": max(0, usage_end["output_tokens"] - usage_start["output_tokens"]),
        "total_tokens": max(0, usage_end["total_tokens"] - usage_start["total_tokens"]),
        "cost_usd": max(0.0, usage_end["cost_usd"] - usage_start["cost_usd"]),
    }

    # Merge this phase's content into automation_tasks.phase_outputs so the
    # automations UI can render the cell. Only on success (non-empty text).
    if req.task_id and phase_text and phase_text.strip():
        _mark_task_phase_complete(sb, req.task_id, phase_meta, phase_text, phase_usage)

    # If the stream returned nothing usable, downgrade an apparent "ok" to a
    # soft error so the driver loop doesn't count it as success.
    if outcome == "ok" and not (phase_text and phase_text.strip()):
        outcome = "error"
        if not error_str:
            error_str = "empty_phase_output"

    return {"text": phase_text, "outcome": outcome, "error": error_str}


# ---------------------------------------------------------------------------
# Pipeline driver (background task)
# ---------------------------------------------------------------------------
def _check_stop_requested(sb, task_id: str) -> bool:
    """Poll automation_tasks.stop_requested. Returns True if the row exists
    and stop_requested is truthy. Errors are logged and treated as 'not
    requested' (we don't want a transient DB blip to kill a running pipeline)."""
    try:
        res = (
            sb.table("automation_tasks")
              .select("stop_requested")
              .eq("id", task_id)
              .limit(1)
              .execute()
        )
        rows = res.data or []
        if rows and rows[0].get("stop_requested"):
            return True
    except Exception as exc:
        log.warning("[pipeline] stop poll failed task=%s: %s", task_id, exc)
    return False


def _mark_task_stopped(sb, task_id: str) -> bool:
    try:
        sb.table("automation_tasks").update({
            "status": "stopped",
            "completed_at": _now_iso(),
        }).eq("id", task_id).execute()
        return True
    except Exception as exc:
        log.warning("[pipeline] mark task stopped failed task=%s: %s",
                    task_id, exc)
        return False


# ---------------------------------------------------------------------------
# automation_tasks progress writes
#
# The automations UI polls listTasks() every 2s while any row is `running`.
# It reads phase_outputs[].content to populate each phase column and
# last_phase_* to show progress. Without these writes the row stays empty
# and `running` until the manual reset.
# ---------------------------------------------------------------------------
def _mark_task_phase_progress(sb, task_id: str,
                              phase_meta: Dict[str, Any]) -> None:
    """At phase_start: bump last_phase_* so the UI shows which phase is in
    flight. Best-effort; a Supabase failure here must not abort the phase."""
    try:
        sb.table("automation_tasks").update({
            "last_phase_index": phase_meta["index"],
            "last_phase_total": phase_meta["total"],
            "last_phase_name": phase_meta["name"],
        }).eq("id", task_id).execute()
    except Exception as exc:
        log.warning("[pipeline] mark task phase progress failed task=%s: %s",
                    task_id, exc)


def _snapshot_chat_usage(sb, chat_id: str) -> Dict[str, Any]:
    """Read the current cumulative usage row for this chat. Returns a dict
    with input_tokens / output_tokens / total_tokens / cost_usd (zeros if no
    row yet). Used by the orchestrator to compute per-phase cost deltas."""
    zero = {"input_tokens": 0, "output_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0}
    try:
        res = (
            sb.table("chat_usage")
              .select("input_tokens,output_tokens,total_tokens,cost_usd")
              .eq("chat_id", chat_id)
              .limit(1)
              .execute()
        )
        rows = res.data or []
        if not rows:
            return zero
        row = rows[0] or {}
        return {
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
            "total_tokens": int(row.get("total_tokens") or 0),
            "cost_usd": float(row.get("cost_usd") or 0.0),
        }
    except Exception as exc:
        log.warning("[pipeline] usage snapshot failed chat=%s: %s",
                    chat_id, exc)
        return zero


def _mark_task_phase_complete(sb, task_id: str,
                              phase_meta: Dict[str, Any],
                              phase_text: str,
                              phase_usage: Optional[Dict[str, Any]] = None) -> None:
    """After a phase completes successfully: merge its content into
    automation_tasks.phase_outputs (replacing any prior entry at the same
    position, sorted by position) and bump last_phase_*. Idempotent on
    re-run since we filter by phase_position first."""
    try:
        res = (
            sb.table("automation_tasks")
              .select("phase_outputs")
              .eq("id", task_id)
              .limit(1)
              .execute()
        )
        rows = res.data or []
        prior = (rows[0] if rows else {}).get("phase_outputs") or []
        if not isinstance(prior, list):
            prior = []

        position = phase_meta["position"]
        filtered = [
            o for o in prior
            if isinstance(o, dict) and o.get("phase_position") != position
        ]
        usage = phase_usage or {}
        new_entry = {
            "phase_index": phase_meta["index"],
            "phase_position": position,
            "phase_name": phase_meta["name"],
            "phase_model_id": phase_meta["model_id"],
            "content": phase_text,
            "completed_at": _now_iso(),
            # Per-phase usage delta computed by the orchestrator from
            # chat_usage snapshots taken at phase_start and phase_end.
            # All fields are floats/ints; UI can render as-is.
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "cost_usd": round(float(usage.get("cost_usd") or 0.0), 6),
        }
        merged = sorted(filtered + [new_entry],
                        key=lambda o: o.get("phase_position", 0))

        sb.table("automation_tasks").update({
            "last_phase_index": phase_meta["index"],
            "last_phase_total": phase_meta["total"],
            "last_phase_name": phase_meta["name"],
            "phase_outputs": merged,
        }).eq("id", task_id).execute()
    except Exception as exc:
        log.warning("[pipeline] mark task phase complete failed "
                    "task=%s position=%s: %s",
                    task_id, phase_meta.get("position"), exc)


def _mark_task_completed(sb, task_id: str) -> bool:
    try:
        sb.table("automation_tasks").update({
            "status": "completed",
            "completed_at": _now_iso(),
        }).eq("id", task_id).execute()
        return True
    except Exception as exc:
        # Loud: a swallowed failure here leaves the automations UI spinning on
        # "running" forever (this is exactly the `last_error`-column bug that
        # made every failed task look stuck — see _mark_task_failed below).
        log.error("[pipeline] mark task COMPLETED failed task=%s: %s",
                  task_id, exc)
        return False


def _mark_task_failed(sb, task_id: str, error: str) -> bool:
    # NOTE: the column is `error`, NOT `last_error`. Writing a non-existent
    # column makes Postgres reject the ENTIRE update, so `status` never flips
    # and the row is stuck on "running". Keep this name in sync with the table.
    try:
        sb.table("automation_tasks").update({
            "status": "failed",
            "completed_at": _now_iso(),
            "error": error[:1000],
        }).eq("id", task_id).execute()
        return True
    except Exception as exc:
        log.error("[pipeline] mark task FAILED failed task=%s: %s",
                  task_id, exc)
        return False


async def _run_pipeline(req: RunPipelineRequest) -> None:
    sb = _sb_client(req)
    # Terminal-row guard (task #49): every pipeline run must persist EXACTLY
    # ONE terminal row — type 'final' on success, 'error' on failure/stop. The
    # UI treats BOTH 'final' and 'error' as the turn-ending marker, so the old
    # belt-and-braces pattern (write 'error' then also 'final') produced a
    # duplicate terminal. `_emit_terminal` writes the first terminal and no-ops
    # on every later call; the `finally` safety net guarantees a terminal exists
    # for automation runs (task_id set) that the pipeline owns. Non-task runs
    # have a concurrent live agent that owns the terminal, so the safety net is
    # gated on task_id to avoid duplicate stub rows.
    terminal_written = {"v": False}
    # Parallel guard for the automation_tasks.status flip. Set True once the row
    # has been moved to a terminal status ("completed"/"failed"). The `finally`
    # block uses it to guarantee the row never stays stuck on "running" — even
    # on cancellation (graceful shutdown/restart) or any early-return path that
    # the try/except branches don't cover.
    task_status_finalized = {"v": False}

    def _emit_terminal(msg_type: str, content: str,
                       metadata: Dict[str, Any]) -> None:
        if terminal_written["v"]:
            return
        if _insert_row(sb, req.chat_id, "assistant", msg_type, content,
                       metadata):
            terminal_written["v"] = True

    # Amendment 1: pre-populate the accumulator with any prior outputs the
    # caller supplied (per-phase rerun support).
    prior_outputs: List[Dict[str, Any]] = [
        {"phase": p.phase, "content": p.content} for p in req.prior_phase_outputs
    ]
    if prior_outputs:
        log.info("[pipeline] chat=%s seeded with %d prior phase outputs",
                 req.chat_id, len(prior_outputs))
    try:
        enabled_phases = [p for p in req.phases if p.enabled]
        disabled_phases = [p for p in req.phases if not p.enabled]
        total = len(enabled_phases)
        log.info("[pipeline] chat=%s starting with %d enabled phase(s) "
                 "(of %d submitted): %s",
                 req.chat_id, total, len(req.phases),
                 [p.name for p in enabled_phases])

        # ── Unified-agent mode (project chats behave like the normal chat) ──
        # A project chat should act like ONE smart agent that holds ALL phase
        # instructions at once and works to the final goal in a single continuous
        # turn — with the full conversation history and the whole tool catalog —
        # exactly like the normal /api/chat endpoint, NOT a chain of isolated
        # per-phase calls that each forget the conversation between them. So
        # collapse the enabled phases into ONE combined phase: their system
        # prompts concatenated in order, run on the first phase's model.
        # _run_phase already special-cases total==1 to drop all orchestration
        # ceremony and behave like a normal chat, and already passes the full
        # req.messages history — so this single change delivers the behaviour.
        # Toggle off with PIPELINE_UNIFY_PHASES=0 to restore per-phase chaining.
        if os.environ.get("PIPELINE_UNIFY_PHASES", "1") != "0" and len(enabled_phases) > 1:
            _combined = "\n\n".join(
                (f"=== {p.name} ===\n{p.system_prompt.strip()}"
                 if (p.name or "").strip() else p.system_prompt.strip())
                for p in enabled_phases if (p.system_prompt or "").strip()
            )
            _first = enabled_phases[0]
            log.info("[pipeline] chat=%s unifying %d phases into a single agentic "
                     "turn (combined prompt %d chars, model=%s)",
                     req.chat_id, len(enabled_phases), len(_combined),
                     _first.model_id)
            enabled_phases = [PhaseSpec(
                id=_first.id, position=_first.position,
                name=_first.name or "Agent", model_id=_first.model_id,
                system_prompt=_combined, enabled=True,
            )]
            total = len(enabled_phases)

        # Task #32 — phase-skip alarm: resolve the expected minimum phase
        # count. Caller-supplied value wins; otherwise fall back to env
        # PIPELINE_MIN_PHASES. 0/None means the check is disabled.
        expected_min = req.expected_phases_min
        if expected_min is None:
            try:
                expected_min = int(os.environ.get("PIPELINE_MIN_PHASES", "0"))
            except ValueError:
                expected_min = 0
        shape_label = req.pipeline_shape or "pipeline"

        # Early rejection: if the caller declared an expected minimum and the
        # enabled count falls short, refuse to start instead of silently
        # running a truncated run. This is the chat-88f73936 failure mode:
        # the caller submitted exactly 1 PhaseSpec and the runner happily
        # ran "1 of 1" with no signal that 5 more phases were missing.
        if expected_min and total < expected_min:
            msg = (
                f"Pipeline misconfigured for '{shape_label}': caller enabled "
                f"{total} phase(s) but at least {expected_min} are expected. "
                f"Refusing to start a truncated run. Enabled phase names: "
                f"{[p.name for p in enabled_phases] or '[]'}. "
                f"Submitted-but-disabled: "
                f"{[p.name for p in disabled_phases] or '[]'}."
            )
            log.error("[pipeline] chat=%s %s", req.chat_id, msg)
            # Single terminal: the UI treats this `error` row as turn-ending.
            # (Previously this also wrote a redundant `final` — see task #49.)
            _emit_terminal("error", msg, {
                "kind": "pipeline_misconfigured",
                "shape": shape_label,
                "enabled_count": total,
                "expected_min": expected_min,
                "enabled_phases": [p.name for p in enabled_phases],
                "disabled_phases": [p.name for p in disabled_phases],
            })
            if req.task_id:
                task_status_finalized["v"] = _mark_task_failed(
                    sb, req.task_id, msg[:1000])
            return

        stopped_early = False
        phases_actually_run = 0
        # Task #33 — per-phase outcome ledger. Each entry:
        #   {"position": int, "name": str, "outcome": "ok"|"timeout"|"error",
        #    "error": str, "duration_s": float}
        phase_outcomes: List[Dict[str, Any]] = []
        for i, phase in enumerate(enabled_phases, start=1):
            # Amendment 2: cooperative stop. Poll BEFORE running the next
            # phase (the row-tag UPDATE for the prior phase has already
            # completed at this point).
            if req.task_id and _check_stop_requested(sb, req.task_id):
                log.info("[pipeline] chat=%s task=%s stop_requested, exiting "
                         "before phase %d", req.chat_id, req.task_id, i)
                _insert_row(sb, req.chat_id, "assistant", "status", "", {
                    "kind": "pipeline_stopped",
                    "phases_run": i - 1,
                    "phases_total": total,
                    "task_id": req.task_id,
                })
                # Single terminal `final` so the UI's "Thinking…" spinner stops
                # (a status row alone is not terminal). See task #49.
                _emit_terminal("final",
                               f"Run stopped by user after {i - 1} of {total} "
                               f"phase(s).",
                               {"kind": "pipeline_stopped",
                                "phases_run": i - 1,
                                "phases_total": total,
                                "task_id": req.task_id})
                task_status_finalized["v"] = _mark_task_stopped(
                    sb, req.task_id)
                stopped_early = True
                break

            t0 = time.time()
            log.info("[pipeline] chat=%s phase %d/%d start name=%s model=%s",
                     req.chat_id, i, total, phase.name, phase.model_id)
            try:
                result = await _run_phase(sb, req, phase, i, total, prior_outputs)
            except Exception as exc:
                # Belt-and-braces: _run_phase already catches, but never let
                # a single phase abort the whole pipeline.
                log.exception("[pipeline] phase %d hard failure: %s", i, exc)
                result = {"text": "", "outcome": "error",
                          "error": f"{type(exc).__name__}: {exc}"[:500]}
            # Tolerate the legacy `_run_phase -> str` return shape used by
            # older tests that stub the function. New shape is a dict
            # with text/outcome/error.
            if isinstance(result, str):
                result = {"text": result,
                          "outcome": "ok" if (result and result.strip()) else "error",
                          "error": "" if (result and result.strip()) else "empty_phase_output"}
            elif not isinstance(result, dict):
                result = {"text": "", "outcome": "error",
                          "error": f"unexpected _run_phase return type: {type(result).__name__}"}
            text = result.get("text") or ""
            outcome = result.get("outcome") or "error"
            err = result.get("error") or ""
            duration_s = round(time.time() - t0, 1)
            if text and text.strip():
                prior_outputs.append({
                    "phase": {
                        "index": i, "total": total,
                        "position": phase.position, "name": phase.name,
                        "model_id": phase.model_id,
                    },
                    "content": text,
                })
            phases_actually_run += 1
            phase_outcomes.append({
                "position": phase.position,
                "name": phase.name,
                "outcome": outcome,
                "error": err,
                "duration_s": duration_s,
            })
            log.info("[pipeline] chat=%s phase %d done in %.1fs "
                     "(outcome=%s text_len=%d)",
                     req.chat_id, i, duration_s, outcome, len(text))

        if not stopped_early:
            # Task #32 — post-run phase-skip alarm. The early-rejection branch
            # above catches the case where the caller declared an expected
            # minimum and missed it. This branch catches the other variant:
            # caller submitted a multi-phase shape but disabled most of them
            # without declaring expected_phases_min. This is a non-terminal
            # advisory, so it is written as a `status` row (kind preserved) —
            # NOT a `type='error'` row, which the UI would treat as a turn-
            # ending terminal and double up with the success `final` below.
            # The metadata kind=pipeline_phases_skipped keeps it queryable and
            # the operator still sees it rendered in the chat (task #49).
            if disabled_phases:
                _insert_row(
                    sb, req.chat_id, "assistant", "status",
                    f"Pipeline ran {total} phase(s) but "
                    f"{len(disabled_phases)} were skipped because the caller "
                    f"submitted them with enabled=false. Skipped: "
                    f"{[p.name for p in disabled_phases]}. "
                    f"If this was intentional, ignore. If not, re-run with "
                    f"all phases enabled.",
                    {"kind": "pipeline_phases_skipped",
                     "shape": shape_label,
                     "reason": "disabled-by-caller",
                     "skipped_phases": [p.name for p in disabled_phases],
                     "enabled_phases": [p.name for p in enabled_phases],
                     "phases_run": phases_actually_run})
                log.warning(
                    "[pipeline] chat=%s phase-skip alarm: %d disabled phase(s) "
                    "(%s) not run", req.chat_id, len(disabled_phases),
                    [p.name for p in disabled_phases])
            # Task #33 bug #3 — branch on per-phase outcomes. The pre-fix
            # code wrote pipeline_complete unconditionally because a timed-
            # out phase doesn't *raise* out of _run_phase, it just writes an
            # `error` row and returns "". On chat d1d9260d that meant the
            # final marker was pipeline_complete{phases_run:1} for a run
            # that pushed only 1 of 5 leads. Now we count outcomes and
            # report partial/failed when any phase didn't finish cleanly.
            ok_count = sum(1 for o in phase_outcomes if o["outcome"] == "ok")
            failed_count = total - ok_count
            failed_outcomes = [o for o in phase_outcomes if o["outcome"] != "ok"]
            timeout_count = sum(1 for o in failed_outcomes if o["outcome"] == "timeout")

            if failed_count == 0:
                # Pure-success path — unchanged from pre-Task-#33 behaviour.
                _insert_row(sb, req.chat_id, "assistant", "status", "", {
                    "kind": "pipeline_complete",
                    "phases_run": total,
                    "phase_outcomes": phase_outcomes,
                })
                # Single terminal `final` so the UI's "Thinking…" spinner stops
                # (a status row alone is not terminal). See task #49.
                # GATED on task_id: when the pipeline is invoked for a chat that
                # already has a live agent (no task_id), the agent's own `final`
                # is the terminal marker. Writing our own here produces a
                # duplicate 35-char stub ("Run complete — 1 phase(s) finished.")
                # that masks the real agent content in the UI (see chat eeb18295
                # 08:37:50, 08:45:50). Automation tasks (task_id set) still get
                # the final + task-completion flip, which is what the UI needs.
                if req.task_id:
                    _emit_terminal("final",
                                   f"Run complete — {total} phase(s) finished.",
                                   {"kind": "pipeline_complete",
                                    "phases_run": total})
                    task_status_finalized["v"] = _mark_task_completed(
                        sb, req.task_id)
                log.info("[pipeline] chat=%s pipeline_complete (%d phases)",
                         req.chat_id, total)
            else:
                # Partial or full failure. If at least one phase succeeded,
                # mark as pipeline_partial; if every phase failed, mark as
                # pipeline_failed. Either way, the metadata carries the full
                # per-phase outcome ledger so a SQL query can answer "which
                # phase died and how" without grepping logs.
                kind = "pipeline_partial" if ok_count > 0 else "pipeline_failed"
                bad_names = ", ".join(
                    f"{o['name']} ({o['outcome']})" for o in failed_outcomes
                )
                if kind == "pipeline_partial":
                    human = (
                        f"Run partial — {ok_count} of {total} phase(s) "
                        f"finished cleanly; "
                        f"{failed_count} failed: {bad_names}."
                    )
                else:
                    human = (
                        f"Run failed — 0 of {total} phase(s) finished "
                        f"cleanly; "
                        f"{failed_count} failed: {bad_names}."
                    )
                if timeout_count:
                    human += (
                        f" {timeout_count} phase(s) hit the wall-clock cap; "
                        f"raise phase_timeout_seconds on the request if this "
                        f"is the normal case."
                    )
                # Single terminal `error` row (UI treats it as turn-ending and
                # surfaces it in the chat). Match the misconfigured branch's
                # shape so the UI can render it identically. (Previously this
                # also wrote a redundant `final` — see task #49.)
                meta_common = {
                    "kind": kind,
                    "phases_run": total,
                    "phases_ok": ok_count,
                    "phases_failed": failed_count,
                    "phase_outcomes": phase_outcomes,
                }
                _emit_terminal("error", human, meta_common)
                log.warning(
                    "[pipeline] chat=%s %s ok=%d/%d failed=%d (timeouts=%d)",
                    req.chat_id, kind, ok_count, total, failed_count,
                    timeout_count,
                )
                # Flip the automation_tasks row to failed (not completed)
                # so the UI reflects reality and the partial output is
                # presented as an incomplete run.
                if req.task_id:
                    task_status_finalized["v"] = _mark_task_failed(
                        sb, req.task_id,
                        f"{kind}: {ok_count}/{total} ok; {bad_names}"[:1000],
                    )
    except Exception as outer_exc:
        # Uncaught error in the driver itself (not a per-phase failure —
        # those are caught and turned into phase_error rows inside _run_phase).
        # Mark the task as failed so the UI doesn't spin forever.
        tb = traceback.format_exc()
        log.exception("[pipeline] chat=%s uncaught driver error: %s",
                      req.chat_id, outer_exc)
        # Single terminal `error` row (the UI treats it as turn-ending). The
        # `_insert_row` retry layer handles transient Supabase blips, and the
        # `finally` safety net below covers the case where it still drops, so
        # the old belt-and-braces `final` is no longer needed. See task #49.
        try:
            _emit_terminal("error",
                           f"Pipeline driver error: "
                           f"{type(outer_exc).__name__}: {outer_exc}",
                           {"kind": "pipeline_error",
                            "error": str(outer_exc)[:500],
                            "error_type": type(outer_exc).__name__,
                            "traceback": tb[-4000:]})
        except Exception:
            pass
        if req.task_id:
            task_status_finalized["v"] = _mark_task_failed(
                sb, req.task_id,
                f"{type(outer_exc).__name__}: {outer_exc}",
            )
    finally:
        # Terminal-row safety net: for automation runs the pipeline owns the
        # terminal, so guarantee one exists even if every prior terminal insert
        # was dropped (or an early `return`/cancellation skipped the emit). Non-
        # task runs rely on the concurrent live agent's terminal, so skip them
        # to avoid a duplicate stub row.
        if req.task_id and not terminal_written["v"]:
            try:
                _insert_row(sb, req.chat_id, "assistant", "error",
                            "Pipeline ended without a terminal status "
                            "(internal).",
                            {"kind": "pipeline_error",
                             "reason": "missing_terminal"})
                log.error("[pipeline] chat=%s safety-net terminal error row "
                          "written (no terminal emitted)", req.chat_id)
            except Exception as _e:
                log.error("[pipeline] chat=%s safety-net terminal write "
                          "failed: %s", req.chat_id, _e)
        # Task-status safety net: if the run reached here without flipping the
        # automation_tasks row to a terminal status — cancellation (graceful
        # shutdown/restart raises CancelledError, which skips `except Exception`
        # but still runs `finally`), an early `return`, or a flip whose UPDATE
        # was rejected — force it to "failed" so the automations UI can never
        # spin on "running" forever. (Does NOT cover hard SIGKILL/OOM, where no
        # code runs at all; a stale-task reaper is the backstop for that.)
        if req.task_id and not task_status_finalized["v"]:
            ok = _mark_task_failed(
                sb, req.task_id,
                "Run ended without a terminal status "
                "(cancelled, crashed, or interrupted).",
            )
            log.error("[pipeline] chat=%s safety-net task→failed flip %s "
                      "(no terminal status reached)",
                      req.chat_id, "ok" if ok else "FAILED")
        async with _active_lock:
            _active_pipelines.discard(req.chat_id)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("/api/run-pipeline")
async def run_pipeline(
    body: RunPipelineRequest,
    authorization: Optional[str] = Header(default=None),
):
    _require_dispatch_secret(authorization)

    if not body.chat_id or not body.project_id:
        raise HTTPException(status_code=422,
                            detail="chat_id and project_id are required")
    if not body.agent_chat_url:
        raise HTTPException(status_code=422,
                            detail="agent_chat_url is required")
    if not body.supabase_url or not body.supabase_service_key:
        raise HTTPException(status_code=422,
                            detail="supabase_url and supabase_service_key are required")

    async with _active_lock:
        if body.chat_id in _active_pipelines:
            return {"ok": False, "error": "already running",
                    "chat_id": body.chat_id}
        _active_pipelines.add(body.chat_id)

    # Spawn detached background task — return immediately.
    asyncio.create_task(_run_pipeline(body))
    log.info("[pipeline] chat=%s queued (%d phases)",
             body.chat_id, len(body.phases))
    return {"ok": True, "started": True, "chat_id": body.chat_id,
            "phases": len([p for p in body.phases if p.enabled])}


@router.delete("/api/run-pipeline/{chat_id}")
async def release_pipeline_lock(
    chat_id: str,
    authorization: Optional[str] = Header(default=None),
):
    """Manually pop the in-memory idempotency lock for a chat.

    Use when a pipeline finished but the finally block somehow didn't release
    the lock (e.g., the process is stuck but didn't restart). Returns whether
    a lock was actually held.
    """
    _require_dispatch_secret(authorization)
    async with _active_lock:
        was_active = chat_id in _active_pipelines
        _active_pipelines.discard(chat_id)
    return {"ok": True, "chat_id": chat_id, "was_active": was_active,
            "currently_active": sorted(_active_pipelines)}


@router.get("/api/run-pipeline/_active")
async def list_active_pipelines(
    authorization: Optional[str] = Header(default=None),
):
    """List chat_ids that currently have a pipeline running. Diagnostic only."""
    _require_dispatch_secret(authorization)
    async with _active_lock:
        return {"active": sorted(_active_pipelines),
                "count": len(_active_pipelines)}
