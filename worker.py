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
POLL_IDLE_S = float(os.getenv("DEAL_SWEEP_POLL_IDLE_S", "5"))
RECLAIM_EVERY_S = float(os.getenv("DEAL_SWEEP_RECLAIM_EVERY_S", "120"))
BOOTSTRAP_TIMEOUT_S = float(os.getenv("DEAL_SWEEP_BOOTSTRAP_TIMEOUT_S", "240"))

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
        res = await sweep.analyze_one(server.agent_manager, opp, source="worker")
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
        if attempts <= MAX_RETRIES:
            await asyncio.to_thread(q.retry, opp_id, error=reason)
            _log(f"retry {opp_id} (attempt {attempts}/{MAX_RETRIES + 1}): {reason}")
        else:
            await asyncio.to_thread(q.mark_failed, opp_id, error=reason, duration_ms=dur)
            _log(f"failed {opp_id} after {attempts} attempts: {reason}")
    except Exception as e:  # noqa: BLE001 — one bad opp must never kill the worker
        dur = int((time.time() - t0) * 1000)
        err = f"{type(e).__name__}: {str(e)[:300]}"
        traceback.print_exc()
        if attempts <= MAX_RETRIES:
            await asyncio.to_thread(q.retry, opp_id, error=err)
            _log(f"retry {opp_id} after error (attempt {attempts}): {err}")
        else:
            await asyncio.to_thread(q.mark_failed, opp_id, error=err, duration_ms=dur)
            _log(f"failed {opp_id} after error: {err}")


async def _drain_loop() -> None:
    """Claim up to CONCURRENCY opps at a time and process them; sleep when idle.

    On startup, reclaim any `working` rows a previous (crashed) worker left
    behind. Periodically reclaim rows whose claim has gone stale (a worker that
    hung or vanished mid-opp) so they don't get stuck forever.
    """
    reclaimed = await asyncio.to_thread(q.reclaim_stragglers)
    if reclaimed:
        _log(f"startup: reclaimed {reclaimed} stuck 'working' row(s) -> waiting")
    _log(f"draining queue (concurrency={CONCURRENCY}, max_retries={MAX_RETRIES})")

    inflight: set[asyncio.Task] = set()
    last_reclaim = time.time()
    while not _stop.is_set():
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
