---
name: Per-chat single-run concurrency guard
description: Why run launchers reserve a per-chat_id slot before the try and release in finally, and the deferred-task-registration trap that defeats a naive live-task check.
---

# Per-chat single-run concurrency guard

Exactly ONE agent run may be in flight per `chat_id`. Two concurrent runs on one
chat corrupt the session: every tool fires twice, dedupe/cleanup state races, and
the shared agent gets reinitialised mid-run — the chat then wedges on "Thinking…".

The guard: `_reserve_run_slot(chat_id)` (no await; raises HTTP 409 if a live task
exists in `_running_tasks` OR `chat_id` is in the `_starting_chats` set; else adds
it) + `_release_run_slot(chat_id)` (idempotent discard). Applied to every endpoint
that accepts a CLIENT-supplied `chat_id`: `/api/chat`, `/api/chat/async`,
`/api/phased-run/start`. Endpoints that mint a fresh `uuid4` per call (the
structured ones) cannot collide and are intentionally NOT guarded.

**Why `_starting_chats` exists (the trap):** a naive "is there a live task in
`_running_tasks`?" check is NOT enough. Task registration was historically
DEFERRED — created lazily inside the `StreamingResponse` generator, which only runs
when the stream is first iterated, after the endpoint already returned. So two
near-simultaneous submits both pass the live-task check before either registers.
`_starting_chats` bridges the reserve→register gap.

**How to apply (the pattern, do not regress it):**
- `chat_id = ...; _reserve_run_slot(chat_id)` BEFORE the `try`. Reserving before
  the try keeps the duplicate-run 409 out of the endpoint's `except`/`finally`, so
  a rejected duplicate never releases the slot owned by the original request.
- Create AND register the background task in `_running_tasks` IN THE ENDPOINT BODY
  before returning the `StreamingResponse` — never inside the generator. Otherwise
  a client disconnect before the stream starts leaks the reservation.
- Release in a `finally` on the endpoint so every exit path (success, HTTP error,
  exception, cancellation) frees the slot. The release after handoff is safe: the
  task is already in `_running_tasks`, so the live-task check keeps blocking
  duplicates; the redundant `discard` cannot open a window.
- `/api/chat/stop` must NOT blindly release the reservation — doing so can clear a
  DIFFERENT in-flight request's reservation mid-setup and reopen the race. Stop
  only cancels the live `_running_tasks` entry.

Frontend must also stop double-submitting (disable send in-flight, dispatch once,
guard StrictMode effect double-invoke, treat 409 as "duplicate ignored"). See
`docs/frontend-anti-double-submit.md`.
