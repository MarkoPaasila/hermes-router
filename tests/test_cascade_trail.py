from cascade_trail import CascadeTrail, _prio, http_reason, reason_label


def test_ttft_deadline_label_and_priority():
    assert reason_label("ttft_deadline") == "TTFT deadline exceeded"
    assert _prio("ttft_deadline") == 95
    assert _prio("network") > _prio("ttft_deadline") > _prio("http_5xx")


def test_http_reason_mapping():
    assert http_reason(429) == "http_429"
    assert http_reason(401) == "http_401"
    assert http_reason(403) == "http_403"
    assert http_reason(400) == "http_400"
    assert http_reason(404) == "http_404"
    assert http_reason(413) == "http_413"
    assert http_reason(503) == "http_5xx"
    assert http_reason(418) == "http_418"


def test_reason_label_known_and_unknown():
    assert "headroom" in reason_label("rate_headroom").lower()
    assert reason_label("totally_new") == "totally_new"
    assert reason_label(None) == ""


def test_skip_and_success_counts():
    t = CascadeTrail()
    t.skip("groq", "llama", "rate_headroom")
    t.skip("cerebras", "llama", "token_cap")
    t.success("openrouter", "gpt")
    fields = t.as_log_fields()
    assert fields["failed"] == 0
    assert fields["skipped"] == 2
    assert fields["cascades"] == 2
    assert [s["outcome"] for s in fields["cascade"]] == ["skipped", "skipped", "success"]
    assert fields["cascade"][-1]["reason"] is None


def test_skip_stores_wait_s():
    t = CascadeTrail()
    t.skip("groq", "llama", "rate_hold", wait_s=90)
    step = t.as_log_fields()["cascade"][0]
    assert step["wait_s"] == 90.0


def test_note_coalesces_wait_s_on_same_reason():
    t = CascadeTrail()
    t.note("groq", "llama", "skipped", "rate_hold", wait_s=30)
    t.note("groq", "llama", "skipped", "rate_hold", wait_s=90)
    t.flush()
    step = t.as_log_fields()["cascade"][0]
    assert step["reason"] == "rate_hold"
    assert step["wait_s"] == 90.0


def test_note_replaces_wait_s_when_reason_changes():
    t = CascadeTrail()
    t.note("groq", "llama", "skipped", "keys_cooling", wait_s=10)
    t.note("groq", "llama", "failed", "http_429", wait_s=120)
    t.flush()
    step = t.as_log_fields()["cascade"][0]
    assert step["reason"] == "http_429"
    assert step["wait_s"] == 120.0


def test_wait_s_omitted_when_zero():
    t = CascadeTrail()
    t.skip("groq", "llama", "token_cap", wait_s=0)
    assert "wait_s" not in t.as_log_fields()["cascade"][0]


def test_note_coalesces_keys_preferring_informative_reason():
    t = CascadeTrail()
    t.note("groq", "llama", "skipped", "keys_cooling")
    t.note("groq", "llama", "skipped", "rate_headroom")
    t.note("groq", "llama", "failed", "http_429")
    t.flush()
    fields = t.as_log_fields()
    assert fields["failed"] == 1
    assert fields["skipped"] == 0
    assert fields["cascade"] == [
        {"provider": "groq", "model": "llama", "outcome": "failed", "reason": "http_429"}
    ]


def test_note_then_different_model_flushes():
    t = CascadeTrail()
    t.note("a", "m1", "failed", "network")
    t.note("a", "m2", "skipped", "rate_headroom")
    t.flush()
    assert len(t.as_log_fields()["cascade"]) == 2


def test_empty_trail():
    assert CascadeTrail().as_log_fields() == {
        "cascade": [], "failed": 0, "skipped": 0, "cascades": 0
    }
