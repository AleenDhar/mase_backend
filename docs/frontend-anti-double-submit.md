# Chat UI: prevent duplicate message submits

Hand these instructions to the chat frontend's AI agent. They fix the root cause
of a bug where a single message produced **two concurrent agent runs on the same
`chat_id`**, which fired every tool twice and left the chat stuck on "Thinking…".

The backend now has its own backstop (see "Backend contract" below), but the
frontend must still stop sending duplicates — the backend will reject the second
one with a `409`, and a UI that keeps spamming will just show errors.

There are three independent causes of duplicate submits. Fix all three.

---

## 1. Disable the send action while a run is in flight

Keep a single `isSending` (a.k.a. `isStreaming` / `inFlight`) state per chat.

- Set it `true` the instant a submit starts, **before** the network call.
- Set it back to `false` only when the run reaches a terminal state (the `final`
  or `error` event / the stream's `done`, or the HTTP request rejects).
- While `true`:
  - Disable the Send button (`disabled={isSending}`).
  - Ignore the Enter key in the composer (return early if `isSending`).
  - Ignore repeat clicks.

This alone stops the common "user clicks twice / hits Enter twice" case.

## 2. Dispatch exactly once per submit (guard against double dispatch)

A disabled button is not enough — rapid double-clicks, IME/Enter quirks, and
re-renders can still fire the handler twice before state updates flush. Add a
synchronous guard that does not depend on React state timing:

```ts
const sendingRef = useRef(false);

async function handleSend(text: string) {
  if (sendingRef.current) return;   // synchronous, fires before any re-render
  sendingRef.current = true;
  setIsSending(true);
  try {
    await sendMessage({ chatId, text });   // exactly one network call
  } finally {
    sendingRef.current = false;
    setIsSending(false);
  }
}
```

Rules:
- Send **one** request per submit. Do not call the endpoint from more than one
  place (e.g. both an `onSubmit` and an `onClick`).
- Generate/choose the `chat_id` once and reuse it; do not create a second request
  with the same `chat_id` for the same user message.

## 3. Handle React StrictMode / effect double-invoke

In development, React 18 StrictMode intentionally mounts, unmounts, and remounts
components, and runs effects **twice**. If a request is fired from a `useEffect`
(e.g. "auto-send the first message on mount", or an effect that reacts to a new
message in state), it will fire twice.

- **Do not start agent runs from `useEffect`.** Start them from the explicit user
  action (the submit handler above).
- If you must trigger from an effect, guard it with a ref so it runs once:

```ts
const startedRef = useRef(false);
useEffect(() => {
  if (startedRef.current) return;
  startedRef.current = true;
  // ...start the run...
}, []);
```

- Also ensure any WebSocket/EventSource is opened once and cleaned up in the
  effect's return function, so a remount doesn't leave two live connections each
  re-sending.

---

## Backend contract (what the server now does)

The server enforces **one run per `chat_id` at a time** on `/api/chat` and
`/api/chat/async`. If a second run is started for a
`chat_id` that already has one in flight, the server responds:

- **HTTP 409 Conflict**
- JSON body: `{ "detail": "A run is already in progress for this chat. Wait for
  it to finish, or stop it, before sending another message." }`

Frontend handling of a `409`:
- **Do not** show it as a hard failure and **do not** retry automatically.
- Treat it as "my duplicate was correctly ignored." Keep showing the existing
  in-flight run (the original request is still streaming its results).
- Optionally surface a subtle, non-blocking note ("Already working on your last
  message…"). Do not clear the composer's `isSending` state on a 409 — the real
  run is still going.

To send a new message before the current run finishes, call the stop endpoint
(`POST /api/chat/stop?chat_id=...`) first, wait for it to return, then submit.

---

## Quick verification checklist

- [ ] Double-clicking Send fires exactly one network request.
- [ ] Pressing Enter twice quickly fires exactly one request.
- [ ] Send button is visibly disabled while a run streams.
- [ ] In dev (StrictMode on), opening a chat does not send the first message twice.
- [ ] A forced second submit for the same `chat_id` gets a 409 and the UI keeps
      showing the original run instead of erroring or duplicating bubbles.
