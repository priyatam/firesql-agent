"""Gate: no unit test may reach Fireworks or any other remote host. Any non-local socket connection raises."""

from __future__ import annotations

import socket

import pytest


def _is_local(address) -> bool:
    if not isinstance(address, tuple):
        return True
    host = address[0]
    return isinstance(host, str) and (host in ("localhost", "::1", "") or host.startswith("127."))


@pytest.fixture(autouse=True)
def no_remote_calls(monkeypatch):
    original = socket.socket.connect

    def guarded(self, address):
        if not _is_local(address):
            raise RuntimeError(f"unit test tried to reach {address}")
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
