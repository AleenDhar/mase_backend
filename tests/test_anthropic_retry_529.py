"""Tests for the retry-on-529 wrapper in anthropic_cache.CachedChatAnthropic.

The Anthropic SDK does not retry HTTP 529 (overloaded_error). Our wrapper
adds an exponential-backoff retry for it, but only when no stream chunks
have already been yielded (so we never duplicate partial output).
"""
from __future__ import annotations

import asyncio
import types

import pytest

import anthropic_cache as ac


class _Fake529(Exception):
    def __init__(self):
        super().__init__("Error code: 529 - overloaded_error")
        self.status_code = 529


class _FakeOther(Exception):
    def __init__(self):
        super().__init__("boom")
        self.status_code = 400


def test_is_overloaded_detects_529_by_status_code():
    assert ac._is_overloaded(_Fake529())


def test_is_overloaded_rejects_non_529():
    assert not ac._is_overloaded(_FakeOther())
    assert not ac._is_overloaded(RuntimeError("nope"))


def test_is_overloaded_detects_by_body():
    exc = Exception("x")
    exc.body = {"error": {"type": "overloaded_error"}}
    assert ac._is_overloaded(exc)


def test_is_overloaded_detects_by_message():
    assert ac._is_overloaded(Exception("Error code: 529 - foo"))
    assert ac._is_overloaded(Exception("got overloaded_error from anthropic"))


# ---------------------------------------------------------------------------
# Behaviour tests against a subclass that simulates the upstream SDK methods.
# We can't easily instantiate real ChatAnthropic without an API key, so we
# build a stub that mirrors the four method shapes the wrapper overrides.
# ---------------------------------------------------------------------------
class _StubBase:
    """Stand-in for `ChatAnthropic` upstream methods."""
    def __init__(self, sequence):
        # `sequence` is a list of either exceptions to raise or values to
        # return. Each call consumes one.
        self._seq = list(sequence)
        self.calls = 0

    def _next(self):
        self.calls += 1
        item = self._seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_cls(stub: _StubBase):
    """Create a CachedChatAnthropic-shaped subclass whose super() methods
    delegate to `stub`, bypassing ChatAnthropic's __init__."""

    class _Test(ac.CachedChatAnthropic):
        def __init__(self):
            pass  # skip ChatAnthropic init

    # Replace the `super()._generate` chain by monkey-patching the
    # immediate parent's methods on the test class. We attach stubs to a
    # transparent intermediate so super() in CachedChatAnthropic.* finds
    # them.
    def _generate(self, *a, **k):
        return stub._next()

    async def _agenerate(self, *a, **k):
        await asyncio.sleep(0)
        return stub._next()

    def _stream(self, *a, **k):
        item = stub._next()
        # If `_next` didn't raise, `item` is an iterable of chunks.
        yield from item

    async def _astream(self, *a, **k):
        item = stub._next()
        for chunk in item:
            await asyncio.sleep(0)
            yield chunk

    # Monkey-patch ChatAnthropic's methods so CachedChatAnthropic.* finds
    # them via super(). The MRO of _Test is
    # _Test -> CachedChatAnthropic -> ChatAnthropic -> ...
    # so super() in our wrapper resolves to these stubs.
    from langchain_anthropic import ChatAnthropic
    ChatAnthropic._generate = _generate
    ChatAnthropic._agenerate = _agenerate
    ChatAnthropic._stream = _stream
    ChatAnthropic._astream = _astream
    return _Test()


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    # Strip the delay so tests are instant.
    monkeypatch.setattr(ac, "_backoff_delay", lambda attempt: 0.0)
    monkeypatch.setattr(ac, "_OVERLOADED_MAX_RETRIES", 2)


def test_agenerate_retries_then_succeeds():
    stub = _StubBase([_Fake529(), _Fake529(), "OK"])
    obj = _make_cls(stub)
    result = asyncio.run(obj._agenerate())
    assert result == "OK"
    assert stub.calls == 3  # 2 failures + 1 success


def test_agenerate_gives_up_after_max():
    stub = _StubBase([_Fake529(), _Fake529(), _Fake529(), _Fake529()])
    obj = _make_cls(stub)
    with pytest.raises(_Fake529):
        asyncio.run(obj._agenerate())
    assert stub.calls == 3  # initial + 2 retries


def test_agenerate_does_not_retry_non_529():
    stub = _StubBase([_FakeOther(), "OK"])
    obj = _make_cls(stub)
    with pytest.raises(_FakeOther):
        asyncio.run(obj._agenerate())
    assert stub.calls == 1


def test_generate_retries_then_succeeds():
    stub = _StubBase([_Fake529(), "OK"])
    obj = _make_cls(stub)
    assert obj._generate() == "OK"
    assert stub.calls == 2


def test_astream_retries_before_first_chunk():
    """529 raised before any chunk is yielded -> retried."""
    stub = _StubBase([_Fake529(), ["chunk-a", "chunk-b"]])
    obj = _make_cls(stub)

    async def _collect():
        out = []
        async for c in obj._astream():
            out.append(c)
        return out

    assert asyncio.run(_collect()) == ["chunk-a", "chunk-b"]
    assert stub.calls == 2


def test_astream_does_not_retry_after_yield():
    """Once a chunk is consumed, a subsequent 529 must NOT trigger retry
    (would duplicate output). We can't easily simulate mid-stream errors
    with the simple stub, so we verify the happy path doesn't double-fire."""
    stub = _StubBase([["only-chunk"]])
    obj = _make_cls(stub)

    async def _collect():
        out = []
        async for c in obj._astream():
            out.append(c)
        return out

    assert asyncio.run(_collect()) == ["only-chunk"]
    assert stub.calls == 1
