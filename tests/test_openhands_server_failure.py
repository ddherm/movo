"""Preserve useful server failures without leaking Docker logs into chat."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from backend.integrations import openhands


def test_conversation_500_keeps_redacted_server_exception(monkeypatch):
    key = "sk-secret-for-this-test"
    monkeypatch.setenv("DEEPSEEK_API_KEY", key)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stderr=f"user content should not be stored\nPermissionError: /workspace/state denied with {key}\n",
            stdout="",
        )

    monkeypatch.setattr(openhands.subprocess, "run", fake_run)
    request = httpx.Request("POST", "http://127.0.0.1:33941/api/conversations")
    response = httpx.Response(500, request=request)
    error = httpx.HTTPStatusError("Server error", request=request, response=response)

    result = openhands._conversation_start_error(error, SimpleNamespace(_container_id="container-123"))

    assert calls == [["docker", "logs", "--tail", "120", "container-123"]]
    assert result.startswith("HTTPStatusError: 500 /api/conversations；服务端异常：PermissionError:")
    assert "[本地路径]" in result
    assert key not in result
    assert "user content should not be stored" not in result


def test_conversation_500_without_server_detail_does_not_guess(monkeypatch):
    monkeypatch.setattr(
        openhands.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr="no traceback", stdout=""),
    )
    request = httpx.Request("POST", "http://127.0.0.1:33941/api/conversations")
    response = httpx.Response(500, request=request)
    error = httpx.HTTPStatusError("Server error", request=request, response=response)

    assert openhands._conversation_start_error(error, SimpleNamespace(_container_id="container-123")) == "HTTPStatusError: 500 /api/conversations"


def test_conversation_500_prefers_server_exception_field(monkeypatch):
    def unexpected_logs(*_args, **_kwargs):
        raise AssertionError("server response already provides the exception")

    monkeypatch.setattr(openhands.subprocess, "run", unexpected_logs)
    request = httpx.Request("POST", "http://127.0.0.1:33941/api/conversations")
    response = httpx.Response(500, request=request, json={
        "detail": "Internal Server Error",
        "exception": "FileNotFoundError: /workspace/conversations/.owner_lease.lock",
    })
    error = httpx.HTTPStatusError("Server error", request=request, response=response)

    result = openhands._conversation_start_error(error, SimpleNamespace(_container_id="container-123"))

    assert "FileNotFoundError" in result
    assert "[本地路径]" in result
    assert "/workspace" not in result
