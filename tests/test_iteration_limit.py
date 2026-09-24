"""Agent iteration exhaustion may still leave a verifiable static prototype."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.integrations import openhands


def _run_error(code: str | None):
    exceptions = pytest.importorskip("openhands.sdk.conversation.exceptions")
    return exceptions.ConversationRunError(
        uuid4(), RuntimeError("Agent ended"),
        conversation_error=SimpleNamespace(code=code) if code else None,
    )


def _recover(monkeypatch, tmp_path, exc, *, outcome=("passed", "passed"), stop_reason=None, timed_out=False):
    calls = []

    def verify(workspace, source_dir):
        calls.append((workspace, source_dir))
        return outcome[0], outcome[1], {"entrypoint": "dist/index.html"}

    monkeypatch.setattr(openhands, "_verify_site", verify)
    workspace = object()
    events = []
    result = openhands._run_failure_result(
        exc, workspace, tmp_path, "conversation-1",
        {"stop_reason": stop_reason, "cost_usd": 0.25},
        timed_out=timed_out, max_seconds=900,
        emit=lambda *args: events.append(args),
    )
    return result, calls, events


def test_structured_iteration_limit_can_finish_when_both_checks_pass(monkeypatch, tmp_path):
    (tmp_path / "dist").mkdir()
    result, calls, events = _recover(monkeypatch, tmp_path, _run_error("MaxIterationsReached"))

    assert result["status"] == "ready"
    assert result["verification"]["build_status"] == "passed"
    assert result["verification"]["interaction_status"] == "passed"
    assert result["verification"]["details"]["agent_stop_reason"] == "iteration_limit"
    assert calls and len(events) == 1


@pytest.mark.parametrize("outcome", [("failed", "not_run"), ("passed", "failed"), ("passed", "not_run")])
def test_iteration_limit_preserves_failure_if_verification_incomplete(monkeypatch, tmp_path, outcome):
    result, calls, _ = _recover(monkeypatch, tmp_path, _run_error("MaxIterationsReached"), outcome=outcome)

    assert len(calls) == 1
    assert result["status"] == "failed"
    assert result["verification"]["build_status"] == outcome[0]
    assert result["verification"]["interaction_status"] == outcome[1]
    assert "ConversationRunError" in result["reason"]


@pytest.mark.parametrize(
    ("exc", "stop_reason", "timed_out"),
    [
        (RuntimeError("MaxIterationsReached"), None, False),
        (RuntimeError("other failure"), "action_limit", False),
    ],
)
def test_unrelated_run_errors_never_trigger_verification(monkeypatch, tmp_path, exc, stop_reason, timed_out):
    result, calls, events = _recover(monkeypatch, tmp_path, exc, stop_reason=stop_reason, timed_out=timed_out)

    assert result["status"] == "failed"
    assert calls == []
    assert events == []


def test_structured_other_error_and_budget_stop_skip_verification(monkeypatch, tmp_path):
    for exc, stop_reason in [(_run_error("OtherFailure"), None), (_run_error("MaxIterationsReached"), "soft_cost_limit")]:
        result, calls, _ = _recover(monkeypatch, tmp_path, exc, stop_reason=stop_reason)
        assert result["status"] == "failed"
        assert calls == []


def test_timeout_takes_priority_over_iteration_limit(monkeypatch, tmp_path):
    result, calls, _ = _recover(monkeypatch, tmp_path, _run_error("MaxIterationsReached"), timed_out=True)

    assert result["status"] == "paused"
    assert result["verification"]["details"]["limit_seconds"] == 900
    assert calls == []


def test_saved_build_verification_runs_without_model_key(monkeypatch, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>Ready</h1>", encoding="utf-8")
    workspace = SimpleNamespace(cleanups=0)
    workspace.cleanup = lambda: setattr(workspace, "cleanups", workspace.cleanups + 1)
    required_keys = []
    monkeypatch.setattr(openhands, "build_status", lambda *, require_api_key=True: (required_keys.append(require_api_key), {"available": True, "missing": []})[1])
    monkeypatch.setattr(openhands, "_open_workspace", lambda source, **_kwargs: workspace)
    monkeypatch.setattr(openhands, "_verify_site", lambda work, source: ("passed", "passed", {"entrypoint": "dist/index.html"}))

    result = openhands._verify_saved_build_sync({"source_dir": str(tmp_path), "agent_conversation_id": "previous-session"})

    assert required_keys == [False]
    assert result["status"] == "ready"
    assert result["conversation_id"] == "previous-session"
    assert result["preview_dir"] == str(tmp_path / "dist")
    assert result["verification"]["details"]["verification_only"] is True
    assert workspace.cleanups == 1
