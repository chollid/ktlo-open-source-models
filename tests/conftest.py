"""Test-wide guardrails."""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail every test that accidentally attempts a real network connection."""

    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
