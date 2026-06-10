---
name: asyncio atomic check-and-claim for per-resource mutual exclusion
description: How to make two async code paths mutually exclusive over a shared resource without a lock, and when that breaks.
---

# Atomic check-and-claim under asyncio

To make two coroutines mutually exclusive over a per-key resource (e.g. one
"Run-All" vs one "single-cell re-run" per analysis), you do NOT need an
`asyncio.Lock`. Put the membership check and the claim (`set.add(key)`) in the
same synchronous block with **no `await` between them**. Under asyncio's
single-threaded cooperative scheduling, no other coroutine can interleave inside
a run of code that contains no `await`, so check-and-claim is atomic. Release the
claim in a `finally`.

**Why:** the original bug was a TOCTOU race — a path checked `is_running()` once,
then did several `await` DB calls before executing; a competing path could start
in between and both would run. A single up-front check is not enough when work
follows across `await` points.

**How to apply:** both competing entry points must check *and* claim the SAME
shared structure before their first `await`. A check that only guards one
direction (e.g. only the rerun path checks the run flag, but start_run doesn't
check the rerun flag) still races. Make the exclusion symmetric.

**Caveat:** this guard is **process-local**. It is invalid the moment the app
runs multiple workers/processes/event loops — then you need a real distributed
lock (DB advisory lock / Redis). Document the single-process assumption next to
the guard.
