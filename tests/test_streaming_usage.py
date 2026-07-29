"""Tests for Anthropic/Codex streaming usage emission and capture."""
import json

import router


def _sse_events(chunks):
    """Parse OpenAI SSE data lines from generator output into JSON objects."""
    events = []
    for chunk in chunks:
        text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        for line in text.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                events.append(json.loads(line[6:]))
    return events


def _usage_from_events(events):
    for ev in events:
        if ev.get("usage"):
            return ev["usage"]
    return None


class _FakeAnthropicResp:
    def __init__(self, events: list[dict]):
        # Anthropic translator uses iter_content; feed one SSE line per chunk.
        lines = [("data: " + json.dumps(e) + "\n").encode() for e in events]
        self._chunks = lines

    def iter_content(self, chunk_size=None):
        yield from self._chunks


class _FakeCodexResp:
    def __init__(self, events: list[dict]):
        # Codex translator uses iter_lines with optional event: prefixes.
        self._lines = []
        for e in events:
            etype = e.get("type", "")
            if etype:
                self._lines.append(f"event: {etype}")
            self._lines.append("data: " + json.dumps(e))

    def iter_lines(self):
        yield from self._lines


def test_anthropic_streaming_emits_usage_chunk():
    events = [
        {"type": "message_start", "message": {
            "id": "msg_1", "model": "claude-sonnet-4-20250514",
            "usage": {"input_tokens": 12, "output_tokens": 0},
        }},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ]
    out = list(router._anthropic_streaming_generator(_FakeAnthropicResp(events)))
    usage = _usage_from_events(_sse_events(out))
    assert usage is not None
    assert usage["prompt_tokens"] == 12
    assert usage["completion_tokens"] == 3
    assert usage["total_tokens"] == 15
    assert any(
        (c.decode("utf-8") if isinstance(c, bytes) else c).strip() == "data: [DONE]"
        for c in out
    )


def test_codex_streaming_emits_usage_chunk():
    events = [
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.completed", "response": {
            "id": "resp_1", "model": "codex", "output": [],
            "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        }},
    ]
    out = list(router._codex_streaming_generator(_FakeCodexResp(events)))
    usage = _usage_from_events(_sse_events(out))
    assert usage is not None
    assert usage["prompt_tokens"] == 20
    assert usage["completion_tokens"] == 5
    assert usage["total_tokens"] == 25


def test_streaming_with_usage_derives_total_from_split():
    name = "_test_stream_usage_provider"
    before = router._provider_tokens[name]

    def gen():
        chunk = {
            "id": "c1", "object": "chat.completion.chunk", "created": 1,
            "model": "x", "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }
        yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    wrapped = router._streaming_with_usage(gen(), name, model="gpt-4o-mini")
    list(wrapped)
    assert router._provider_tokens[name] == before + 14
    assert wrapped._hermes_stream_usage["prompt_tokens"] == 10
    assert wrapped._hermes_stream_usage["completion_tokens"] == 4
    assert wrapped._hermes_stream_usage["total_tokens"] == 14


def test_streaming_with_usage_exposes_usage_for_log_patch():
    name = "_test_stream_log_patch"

    def gen():
        chunk = {
            "id": "c2", "object": "chat.completion.chunk", "created": 1,
            "model": "x", "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        }
        yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    wrapped = router._streaming_with_usage(gen(), name, model="x")
    entry = {
        "status": "success",
        "prompt_tokens": None,
        "completion_tokens": None,
    }
    assert router.request_log.append(entry) is entry

    # Mimic chat/_finalize: consume stream, then patch the log entry.
    list(wrapped)
    router._patch_stream_log_tokens(entry, wrapped)

    assert entry["prompt_tokens"] == 7
    assert entry["completion_tokens"] == 2
    snap = router.request_log.snapshot(limit=5)
    patched = next(e for e in snap if e is entry)
    assert patched["prompt_tokens"] == 7
    assert patched["completion_tokens"] == 2


def test_update_entry_noop_when_not_in_buffer():
    orphan = {"prompt_tokens": None, "completion_tokens": None}
    router.request_log.update_entry(orphan, prompt_tokens=99, completion_tokens=1)
    assert orphan["prompt_tokens"] is None
    assert orphan["completion_tokens"] is None
