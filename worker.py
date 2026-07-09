"""worker.py — the deal-engine sweep worker (crash-safe, out-of-process).

The sweep used to run as an asyncio batch INSIDE the web process, holding all
progress in an in-memory dict. A big run starved web requests, and any restart
lost the whole run. This worker is the other half of the fix (see sweep_queue.py
+ migrations/0009_sweep_queue.sql): it is a SEPARATE process that drains the
durable `sweep_queue` table — `waiting -> working -> done|failed` — so:

  * the web process never runs the heavy batch and stays responsive, and
  * a crash/restart resumes exactly where it left off (claimed-but-unfinished
    rows are reclaimed back to `waiting`; per-opp records are already idempotently
    upserted, so finished opps are never re-charged).

It reuses the EXACT same agent + analyze_one() as the web process by importing
server.py (uvicorn only runs under server.py's own __main__, so importing it does
NOT start a second web server). To keep its memory footprint small and give the
sweep its OWN Avoma subprocess (so it never contends with interactive chat), it
loads ONLY the salesforce + avoma MCP servers via MCP_SERVER_ALLOWLIST.

Run:  python3 worker.py
Concurrency: DEAL_SWEEP_CONCURRENCY (default 2 — low on purpose: Avoma is a single
subprocess and high concurrency OOM-crashes the box).
"""
import asyncio
import os
import random
import signal
import time
import traceback

# Load ONLY the two MCP servers the sweep needs. MUST be set before importing
# server (config reads the env at import time). Gives the worker its own isolated
# salesforce + avoma subprocesses and keeps its memory footprint small.
os.environ.setdefault("MCP_SERVER_ALLOWLIST", "salesforce,avoma")

import server  # noqa: E402  (env must be set first)
import deal_engine_sweep as sweep  # noqa: E402
import sweep_queue as q  # noqa: E402

CONCURRENCY = max(1, int(os.getenv("DEAL_SWEEP_CONCURRENCY", "2")))
MAX_RETRIES = max(0, int(os.getenv("DEAL_SWEEP_MAX_RETRIES", "2")))
# A TRANSIENT upstream failure (LLM/MCP rate limit or overload — see _is_transient)
# must NOT permanently fail a deal. It gets a much larger attempt budget and an
# exponential, jittered backoff so the deal simply WAITS for pressure to drop and
# succeeds later, instead of landing in `failed`. This is the core resilience to
# Anthropic / Avoma / Salesforce rate limits: we never burn a deal on a 429/529/520
# /timeout — we pace and retry.
MAX_TRANSIENT_RETRIES = max(MAX_RETRIES, int(os.getenv("DEAL_SWEEP_MAX_TRANSIENT_RETRIES", "10")))
BACKOFF_BASE_S = float(os.getenv("DEAL_SWEEP_BACKOFF_BASE_S", "8"))
BACKOFF_MAX_S = float(os.getenv("DEAL_SWEEP_BACKOFF_MAX_S", "180"))
POLL_IDLE_S = float(os.getenv("DEAL_SWEEP_POLL_IDLE_S", "5"))
RECLAIM_EVERY_S = float(os.getenv("DEAL_SWEEP_RECLAIM_EVERY_S", "120"))
BOOTSTRAP_TIMEOUT_S = float(os.getenv("DEAL_SWEEP_BOOTSTRAP_TIMEOUT_S", "240"))

# Substrings that mark a TRANSIENT upstream failure (rate limit / overload / network
# blip from Anthropic, Avoma, or Salesforce). These self-heal once load drops, so we
# re-queue with backoff rather than failing the deal.
_TRANSIENT_MARKERS = (
    "apitimeouterror", "ratelimit", "rate limit", "rate_limit", "429",
    "overloaded", "overload", "529", "520", "502", "503", "504",
    "internalservererror", "service unavailable", "timed out", "timeout",
    "connection", "econnreset", "temporarily", "try again", "too many requests",
    # Agent-output errors that a fresh attempt usually fixes: a parse_error is
    # often a one-off malformed/truncated JSON generation (esp. under load), so
    # re-queue + retry rather than failing the deal. Raising DEAL_SWEEP_MAX_TOKENS
    # cuts the truncation cause; this covers the residual one-offs.
    "parse_error", "unparseable", "json_parse", "json parse",
)


def _is_transient(err: str) -> bool:
    e = (err or "").lower()
    return any(m in e for m in _TRANSIENT_MARKERS)


def _backoff_delay(attempts: int) -> float:
    """Exponential backoff with jitter (the jitter de-syncs the worker fleet so
    re-queued deals don't all retry in the same instant and re-trip the limit)."""
    raw = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** max(0, attempts - 1)))
    return raw * random.uniform(0.5, 1.0)


_stop = asyncio.Event()


def _log(msg: str) -> None:
    print(f"[SWEEP-WORKER] {msg}", flush=True)


async def _bootstrap_agent() -> None:
    """Bring the shared AgentManager up with salesforce + avoma tools loaded,
    mirroring the web server's startup (initialize_agent(skip_mcp=True) then
    background MCP loading), and block until both servers' tools are present."""
    am = server.agent_manager
    await am.initialize_agent(skip_mcp=True)
    am.start_mcp_background_loading()
    _log("waiting for salesforce + avoma MCP tools to load…")
    deadline = time.time() + BOOTSTRAP_TIMEOUT_S
    while time.time() < deadline:
        by = am._cached_mcp_tools_by_server or {}
        if by.get("salesforce") and by.get("avoma"):
            sf, av = len(by["salesforce"]), len(by["avoma"])
            _log(f"MCP ready: salesforce={sf} tools, avoma={av} tools")
            return
        await asyncio.sleep(2)
    by = server.agent_manager._cached_mcp_tools_by_server or {}
    raise RuntimeError(
        "MCP tools did not load in time "
        f"(salesforce={bool(by.get('salesforce'))}, avoma={bool(by.get('avoma'))})")


async def _retry_or_fail(opp_id: str, attempts: int, reason: str, dur) -> None:
    """Decide a non-completed opp's fate. A TRANSIENT upstream error (LLM/MCP rate
    limit, overload, 5xx, timeout) is re-queued with exponential, jittered backoff
    under a large attempt budget — so a temporary rate limit NEVER permanently fails
    a deal; it just waits for pressure to drop and succeeds later. A genuine error
    gets the normal small retry budget, then fails."""
    transient = _is_transient(reason)
    cap = MAX_TRANSIENT_RETRIES if transient else MAX_RETRIES
    if attempts <= cap:
        if transient:
            delay = _backoff_delay(attempts)
            _log(f"transient upstream error {opp_id} (attempt {attempts}/{cap}); "
                 f"backoff {delay:.0f}s then re-queue: {reason[:140]}")
            try:
                # Interruptible sleep: a shutdown during backoff leaves the row
                # `working`, which the next worker reclaims — nothing is lost.
                await asyncio.wait_for(_stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass
        await asyncio.to_thread(q.retry, opp_id, error=reason)
        _log(f"retry {opp_id} (attempt {attempts}/{cap}"
             f"{', transient' if transient else ''}): {reason[:140]}")
    else:
        await asyncio.to_thread(q.mark_failed, opp_id, error=reason, duration_ms=dur)
        _log(f"failed {opp_id} after {attempts} attempts: {reason[:140]}")


async def _process(row: dict) -> None:
    """Analyze one claimed opp and record the outcome on its queue row.

    analyze_one() is idempotent and persists the canonical record itself; here we
    only translate its result into the row's terminal state. A `completed` read
    that came back thin (degraded/no-calls) is treated as retryable. Retries are
    bounded by attempts (already bumped on claim), so a poison opp eventually
    lands in `failed` instead of looping forever.
    """
    opp_id = row.get("opp_id")
    attempts = row.get("attempts") or 0
    opp = {
        "id": opp_id,
        "account": row.get("account_name"),
        "owner_name": row.get("owner_name"),
        "name": row.get("opp_name"),
    }
    t0 = time.time()
    try:
        # A from-scratch PURGE enqueues its rows under a "fromscratch-*" run_id so the
        # worker rebuilds the record with NO carry-forward (drops poisoned living memory)
        # — identical to the synchronous /update-living-memory endpoint, but on the
        # autoscaled fleet. Normal rows keep source="worker" (incremental carry-forward).
        # The run_id prefix carries the ORIGIN so the dashboard shows the real source
        # instead of a blanket "worker": sftrig-* = Salesforce CDC trigger,
        # trigger-* = manual re-run, fromscratch-* = purge rebuild, else = scheduled/book.
        _rid = str(row.get("run_id") or "")
        # Reliable labeling (2026-07-05): a claimed row sometimes reaches the worker
        # without its run_id, so a Salesforce/manual trigger was logged under the
        # generic "worker" source (proven: 100% of recent "worker" runs were sftrig-
        # rows). When the run_id is absent OR lacks a known origin prefix, re-read the
        # authoritative run_id from the queue row before deriving the label. Correctly
        # prefixed rows skip the extra read (no behaviour change for them).
        if not _rid.startswith(("fromscratch", "sftrig", "trigger")):
            try:
                _fresh = await asyncio.to_thread(q.get_run_id, opp_id)
                if _fresh:
                    _rid = str(_fresh)
            except Exception as _e:  # noqa: BLE001 — labeling is best-effort
                _log(f"run_id re-read failed opp={opp_id}: {type(_e).__name__}: {_e}")
        if _rid.startswith("fromscratch"):
            _src = "update_living_memory"
        elif _rid.startswith("sftrig"):
            _src = "salesforce_trigger"
        elif _rid.startswith("trigger"):
            _src = "manual"
        else:
            _src = "worker"
        _log(f"source label opp={opp_id} run_id={_rid!r} -> {_src}")
        # HEARTBEAT (2026-07-09 zombie fix): while this sweep runs, refresh the claim
        # every HEARTBEAT_EVERY_S so `claimed_at` stays fresh and the row is never
        # mistaken for a dead claim by reclaim_stale. Cancelled the instant the sweep
        # returns. Best-effort — a heartbeat REST hiccup never disturbs the sweep.
        async def _hb() -> None:
            try:
                while True:
                    await asyncio.sleep(q.HEARTBEAT_EVERY_S)
                    try:
                        await asyncio.to_thread(q.heartbeat, opp_id)
                    except Exception as _hbe:  # noqa: BLE001
                        _log(f"heartbeat miss opp={opp_id}: {type(_hbe).__name__}")
            except asyncio.CancelledError:
                return
        _hb_task = asyncio.create_task(_hb())
        try:
            res = await sweep.analyze_one(server.agent_manager, opp, source=_src)
        finally:
            _hb_task.cancel()
            try:
                await _hb_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        status = (res or {}).get("status")
        thin = bool((res or {}).get("thin"))
        dur = (res or {}).get("duration_ms") or int((time.time() - t0) * 1000)
        if status == "completed" and not thin:
            await asyncio.to_thread(q.mark_done, opp_id, duration_ms=dur)
            _log(f"done {opp_id} ({dur} ms, attempt {attempts})")
            return
        reason = ((res or {}).get("error")
                  or (res or {}).get("thin_reason")
                  or status or "incomplete")
        await _retry_or_fail(opp_id, attempts, reason, dur)
    except Exception as e:  # noqa: BLE001 — one bad opp must never kill the worker
        dur = int((time.time() - t0) * 1000)
        err = f"{type(e).__name__}: {str(e)[:300]}"
        traceback.print_exc()
        await _retry_or_fail(opp_id, attempts, err, dur)


async def _drain_loop() -> None:
    """Claim up to CONCURRENCY opps at a time and process them; sleep when idle.

    On startup, reclaim any `working` rows a previous (crashed) worker left
    behind. Periodically reclaim rows whose claim has gone stale (a worker that
    hung or vanished mid-opp) so they don't get stuck forever.
    """
    # Startup recovery MUST be age-based (reclaim_stale), NOT a blanket
    # reclaim_stragglers. With a multi-task worker fleet, a blanket "flip every
    # `working` row to waiting" steals rows that OTHER LIVE workers are actively
    # processing -> the same opp gets swept twice (double cost + API load). An
    # age-based reclaim only touches rows whose claim is older than STALE_CLAIM_S
    # (genuinely orphaned, since no healthy run takes that long), so it recovers a
    # crashed worker's rows without ever stealing a peer's in-flight work.
    _manual = os.getenv("DEAL_SWEEP_MANUAL_ONLY", "false").strip().lower() in ("1", "true", "yes", "on")
    if not _manual:
        reclaimed = await asyncio.to_thread(q.reclaim_stale)
        if reclaimed:
            _log(f"startup: reclaimed {reclaimed} stale 'working' row(s) -> waiting")
        _log(f"draining queue (concurrency={CONCURRENCY}, max_retries={MAX_RETRIES}, "
             f"stale_after={q.STALE_CLAIM_S}s)")
    else:
        _log("startup: DEAL_SWEEP_MANUAL_ONLY is ON — NOT reclaiming/draining the queue")

    inflight: set[asyncio.Task] = set()
    last_reclaim = time.time()
    _manual_logged = False
    while not _stop.is_set():
        # MANUAL-ONLY TEST PAUSE (2026-07-09): when DEAL_SWEEP_MANUAL_ONLY is set, the
        # worker does NOT claim/drain the queue at all — automated sweeping is off and
        # manual triggers run synchronously on the web process. Idle until the flag clears
        # (env change takes effect on the worker's next restart/deploy) or a stop signal.
        if os.getenv("DEAL_SWEEP_MANUAL_ONLY", "false").strip().lower() in ("1", "true", "yes", "on"):
            if not _manual_logged:
                _log("DEAL_SWEEP_MANUAL_ONLY is ON — worker IDLE, not draining the queue "
                     "(automated sweeping paused; manual triggers run on the web process)")
                _manual_logged = True
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_IDLE_S)
            except asyncio.TimeoutError:
                pass
            continue
        if time.time() - last_reclaim > RECLAIM_EVERY_S:
            try:
                n = await asyncio.to_thread(q.reclaim_stale)
                if n:
                    _log(f"reclaimed {n} stale 'working' row(s) -> waiting")
            except Exception as e:  # noqa: BLE001
                _log(f"reclaim_stale error: {type(e).__name__}: {e}")
            last_reclaim = time.time()

        claimed_any = False
        while len(inflight) < CONCURRENCY and not _stop.is_set():
            try:
                row = await asyncio.to_thread(q.claim_one)
            except Exception as e:  # noqa: BLE001
                _log(f"claim error: {type(e).__name__}: {e}")
                row = None
            if not row:
                break
            claimed_any = True
            task = asyncio.create_task(_process(row))
            inflight.add(task)
            task.add_done_callback(inflight.discard)

        if inflight:
            await asyncio.wait(inflight, timeout=POLL_IDLE_S,
                               return_when=asyncio.FIRST_COMPLETED)
        elif not claimed_any:
            # queue empty — wait for either the poll interval or a stop signal.
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_IDLE_S)
            except asyncio.TimeoutError:
                pass

    if inflight:
        _log(f"shutdown: {len(inflight)} in-flight opp(s) left to the next worker "
             f"(they'll be reclaimed as stragglers)")


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Stop the loop promptly on SIGTERM/SIGINT. In-flight opps are left to be
    reclaimed by the next worker (idempotent) so shutdown is fast and never
    blocks a restart's SIGTERM->SIGKILL escalation."""
    def _request_stop():
        if not _stop.is_set():
            _log("stop signal received; finishing the poll cycle and exiting")
            _stop.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, ValueError):  # pragma: no cover
            signal.signal(sig, lambda *_a: _request_stop())


async def _main() -> None:
    _install_signal_handlers(asyncio.get_running_loop())
    _log("starting up")
    await _bootstrap_agent()
    await _drain_loop()
    _log("exited cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
