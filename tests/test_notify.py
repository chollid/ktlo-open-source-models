from __future__ import annotations

import sys
import types

from lib.notify import notify


def test_notify_is_silent_noop_when_webhook_unset(monkeypatch):
    monkeypatch.delenv("NOTIFY_WEBHOOK", raising=False)
    fake_requests = types.ModuleType("requests")

    def unexpected_post(*args, **kwargs):
        raise AssertionError("requests.post must not be called")

    fake_requests.post = unexpected_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert notify("nothing to send") is None


def test_notify_redacts_secrets_and_never_raises(monkeypatch):
    webhook = "https://notify.invalid/a-secret-url"
    token = "hf_do-not-send"
    dispatch_pat = "github_pat_do-not-send"
    monkeypatch.setenv("NOTIFY_WEBHOOK", webhook)
    monkeypatch.setenv("HF_TOKEN", token)
    monkeypatch.setenv("DISPATCH_PAT", dispatch_pat)
    calls = []
    fake_requests = types.ModuleType("requests")

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        raise RuntimeError(f"failed with {token}")

    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    assert notify(
        f"token={token}; pat={dispatch_pat}; hook={webhook}", level="error"
    ) is None
    assert calls[0][0] == webhook
    payload_text = calls[0][1]["json"]["text"]
    assert token not in payload_text
    assert dispatch_pat not in payload_text
    assert webhook not in payload_text
