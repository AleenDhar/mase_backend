"""
Tests for the chat_id injection in _wrap_mcp_tool. The wrapper must override
the LLM-supplied chat_id for `lemlist_validated_push` and
`lemlist_get_push_receipts` with the real chat UUID from the
`_current_chat_id` ContextVar, and mirror the override into the returned
payload.

Run: python3 tests/test_chat_id_injection.py
"""
import asyncio
import io
import json
import os
import sys
import contextlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Avoid touching real Supabase / Anthropic at import time.
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import server  # noqa: E402


REAL_CHAT_ID = "c9e501c6-bf81-4835-a6ed-8cfbe41b1f6b"
FAKE_CHAT_ID = "current_session_aaf_001"


class _FakeTool:
    def __init__(self, name, fn):
        self.name = name
        self.func = fn
        self.coroutine = None
        self.description = f"fake {name}"
        self.args_schema = None


def _make_wrapped(name, fn):
    mgr = server.AgentManager.__new__(server.AgentManager)
    return mgr._wrap_mcp_tool(_FakeTool(name, fn))


def _capture(coro):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = asyncio.get_event_loop().run_until_complete(coro)
    return result, buf.getvalue()


def run(label, fn):
    try:
        fn()
        print(f"  PASS  {label}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {label}\n        {e}")
        return False
    except Exception as e:
        print(f"  ERROR {label}\n        {type(e).__name__}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# lemlist_validated_push: kwargs path, override fires, payload rewritten
# ─────────────────────────────────────────────────────────────────────────────
def test_validated_push_kwargs_override():
    seen = {}

    def fake_push(**kw):
        seen.update(kw)
        return [{"type": "text", "text": json.dumps({
            "chat_id": kw.get("chat_id"),
            "account_id": kw.get("account_id"),
            "pushed": [],
        })}]

    wrapped = _make_wrapped("lemlist_validated_push", fake_push)
    token = server._current_chat_id.set(REAL_CHAT_ID)
    try:
        result, logs = _capture(wrapped.coroutine(
            chat_id=FAKE_CHAT_ID,
            account_id="0010X00004cUsI5QAK",
        ))
    finally:
        server._current_chat_id.reset(token)

    assert seen.get("chat_id") == REAL_CHAT_ID, \
        f"underlying tool got chat_id={seen.get('chat_id')!r}, expected real UUID"
    assert "[CHAT-ID-INJECT]" in logs, f"missing inject log line in:\n{logs}"
    # Payload rewrite — content-block list shape
    assert isinstance(result, list), f"expected list result, got {type(result)}"
    inner = json.loads(result[0]["text"])
    assert inner["chat_id"] == REAL_CHAT_ID, \
        f"payload chat_id not rewritten: {inner['chat_id']!r}"


# ─────────────────────────────────────────────────────────────────────────────
# lemlist_get_push_receipts: positional arg path
# ─────────────────────────────────────────────────────────────────────────────
def test_get_receipts_positional_override():
    seen_args = []

    def fake_get(chat_id, campaign_id=None):
        seen_args.append((chat_id, campaign_id))
        return {"chat_id": chat_id, "campaign_id": campaign_id, "receipts": []}

    wrapped = _make_wrapped("lemlist_get_push_receipts", fake_get)
    token = server._current_chat_id.set(REAL_CHAT_ID)
    try:
        result, logs = _capture(wrapped.coroutine(FAKE_CHAT_ID, "cam_xyz"))
    finally:
        server._current_chat_id.reset(token)

    assert seen_args == [(REAL_CHAT_ID, "cam_xyz")], \
        f"underlying tool called with {seen_args!r}"
    assert "[CHAT-ID-INJECT]" in logs
    assert result["chat_id"] == REAL_CHAT_ID


# ─────────────────────────────────────────────────────────────────────────────
# Pass-through when no chat context (external /mcp client path)
# ─────────────────────────────────────────────────────────────────────────────
def test_passthrough_when_no_chat_context():
    seen = {}

    def fake_get(chat_id, campaign_id=None):
        seen["chat_id"] = chat_id
        return {"chat_id": chat_id, "receipts": []}

    wrapped = _make_wrapped("lemlist_get_push_receipts", fake_get)
    # No ContextVar set -> _current_chat_id.get(None) is None
    result, logs = _capture(wrapped.coroutine(chat_id="external-supplied-id"))

    assert seen["chat_id"] == "external-supplied-id", \
        f"passthrough broken: underlying got {seen['chat_id']!r}"
    assert "[CHAT-ID-INJECT]" not in logs, \
        f"override fired without chat context:\n{logs}"
    assert result["chat_id"] == "external-supplied-id"


# ─────────────────────────────────────────────────────────────────────────────
# No-op when supplied chat_id already matches real UUID (no log, no rewrite)
# ─────────────────────────────────────────────────────────────────────────────
def test_no_op_when_already_correct():
    seen = {}

    def fake_get(chat_id, campaign_id=None):
        seen["chat_id"] = chat_id
        return {"chat_id": chat_id, "receipts": []}

    wrapped = _make_wrapped("lemlist_get_push_receipts", fake_get)
    token = server._current_chat_id.set(REAL_CHAT_ID)
    try:
        result, logs = _capture(wrapped.coroutine(chat_id=REAL_CHAT_ID))
    finally:
        server._current_chat_id.reset(token)

    assert seen["chat_id"] == REAL_CHAT_ID
    assert "[CHAT-ID-INJECT]" not in logs, \
        f"override should NOT fire when already correct:\n{logs}"


# ─────────────────────────────────────────────────────────────────────────────
# Other lemlist tools are NOT touched (only the two receipt tools)
# ─────────────────────────────────────────────────────────────────────────────
def test_other_lemlist_tools_unaffected():
    seen = {}

    def fake_other(chat_id, **kw):
        seen["chat_id"] = chat_id
        return {"ok": True}

    wrapped = _make_wrapped("lemlist_get_campaign", fake_other)
    token = server._current_chat_id.set(REAL_CHAT_ID)
    try:
        result, logs = _capture(wrapped.coroutine(chat_id="some-other-value"))
    finally:
        server._current_chat_id.reset(token)

    assert seen["chat_id"] == "some-other-value", \
        f"chat_id wrongly overridden on non-receipt tool: {seen['chat_id']!r}"
    assert "[CHAT-ID-INJECT]" not in logs


if __name__ == "__main__":
    tests = [
        ("validated_push kwargs path: override fires + payload rewritten",
         test_validated_push_kwargs_override),
        ("get_push_receipts positional path: override fires",
         test_get_receipts_positional_override),
        ("no chat context -> LLM-supplied chat_id passes through",
         test_passthrough_when_no_chat_context),
        ("supplied chat_id already correct -> no-op, no log",
         test_no_op_when_already_correct),
        ("non-receipt lemlist tools are unaffected",
         test_other_lemlist_tools_unaffected),
    ]
    passed = sum(run(label, fn) for label, fn in tests)
    total = len(tests)
    print(f"\n{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)
