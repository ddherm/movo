"""Public OpenHands calls build in a staged copy and report the chosen project."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.core.folders import ProjectFolders
from backend.integrations import openhands
from backend.integrations.project_staging import ProjectSyncConflict


class FakeStage:
    def __init__(self, source_dir: Path, stage_dir: Path, *, conflict: bool = False):
        self.source_dir = source_dir
        self.stage_dir = stage_dir
        self.workspace_dir = stage_dir / "workspace"
        self.workspace_dir.mkdir(parents=True)
        self.conflict = conflict
        self.sync_count = 0

    def sync_back(self) -> None:
        self.sync_count += 1
        if self.conflict:
            raise ProjectSyncConflict(["src/main.js"])
        staged_index = self.workspace_dir / "dist" / "index.html"
        if staged_index.is_file():
            target = self.source_dir / "dist" / "index.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(staged_index.read_bytes())


def _prepare(tmp_path: Path, monkeypatch, *, conflict: bool = False) -> tuple[Path, FakeStage]:
    source_dir = tmp_path / "selected-project"
    source_dir.mkdir()
    stage = FakeStage(source_dir, tmp_path / "native-disk" / "run-1", conflict=conflict)
    state_dir = tmp_path / "native-disk" / "agent-state"
    seen: list[tuple[Path, Path]] = []

    def fake_stage_project(source: Path, state: Path) -> FakeStage:
        seen.append((source, state))
        return stage

    monkeypatch.setattr(openhands, "_agent_state_dir", lambda _source: state_dir)
    monkeypatch.setattr(openhands, "stage_project", fake_stage_project)
    stage.seen = seen
    return source_dir, stage


def _assert_staged_snapshot(snapshot: dict, source_dir: Path, stage: FakeStage) -> None:
    assert snapshot["source_dir"] == str(stage.workspace_dir)
    assert snapshot["workspace_dir"] == str(stage.workspace_dir)
    assert snapshot["workspace_root"] == str(stage.workspace_dir)
    # This remains the identity used for the persistent conversation and key.
    assert snapshot["project_source_dir"] == str(source_dir)


def test_run_build_syncs_ready_preview_into_selected_project(tmp_path, monkeypatch):
    source_dir, stage = _prepare(tmp_path, monkeypatch)
    staged_index = stage.workspace_dir / "dist" / "index.html"
    staged_index.parent.mkdir()
    staged_index.write_text("working preview", encoding="utf-8")

    def fake_run(snapshot, _loop, _on_event, _on_question):
        _assert_staged_snapshot(snapshot, source_dir, stage)
        return {
            "status": "ready", "conversation_id": "conversation-1",
            "source_dir": str(stage.workspace_dir), "preview_dir": str(staged_index.parent),
            "verification": {"build_status": "passed", "interaction_status": "passed", "details": {}},
            "reason": "verified",
        }

    monkeypatch.setattr(openhands, "_run_build_sync", fake_run)

    async def on_event(*_args):
        return None

    result = asyncio.run(openhands.run_build({"source_dir": str(source_dir)}, on_event))

    assert stage.seen == [(source_dir, tmp_path / "native-disk" / "agent-state")]
    assert stage.sync_count == 1
    assert result["status"] == "ready"
    assert result["source_dir"] == str(source_dir)
    assert result["preview_dir"] == str(source_dir / "dist")
    assert (source_dir / "dist" / "index.html").read_text(encoding="utf-8") == "working preview"


def test_run_build_syncs_agent_edits_even_if_verification_failed(tmp_path, monkeypatch):
    source_dir, stage = _prepare(tmp_path, monkeypatch)
    staged_index = stage.workspace_dir / "dist" / "index.html"
    staged_index.parent.mkdir()
    staged_index.write_text("partial preview", encoding="utf-8")

    def fake_run(snapshot, _loop, _on_event, _on_question):
        _assert_staged_snapshot(snapshot, source_dir, stage)
        return {
            "status": "failed", "conversation_id": "conversation-1",
            "source_dir": str(stage.workspace_dir), "preview_dir": None,
            "verification": {"build_status": "failed", "interaction_status": "not_run", "details": {"reason": "compile error"}},
            "reason": "compile error",
        }

    monkeypatch.setattr(openhands, "_run_build_sync", fake_run)

    async def on_event(*_args):
        return None

    result = asyncio.run(openhands.run_build({"source_dir": str(source_dir)}, on_event))

    assert stage.sync_count == 1
    assert (source_dir / "dist" / "index.html").read_text(encoding="utf-8") == "partial preview"
    assert result["status"] == "failed"
    assert result["source_dir"] == str(source_dir)
    assert result["preview_dir"] is None
    assert result["verification"]["details"]["reason"] == "compile error"


def test_sync_conflict_keeps_original_and_recovery_workspace(tmp_path, monkeypatch):
    source_dir, stage = _prepare(tmp_path, monkeypatch, conflict=True)
    original = source_dir / "src" / "main.js"
    original.parent.mkdir()
    original.write_text("user edit", encoding="utf-8")
    staged = stage.workspace_dir / "src" / "main.js"
    staged.parent.mkdir()
    staged.write_text("agent edit", encoding="utf-8")

    monkeypatch.setattr(openhands, "_run_build_sync", lambda *_args: {
        "status": "ready", "conversation_id": "conversation-1",
        "source_dir": str(stage.workspace_dir), "preview_dir": str(stage.workspace_dir / "dist"),
        "verification": {"build_status": "passed", "interaction_status": "passed", "details": {}},
        "reason": "verified",
    })

    async def on_event(*_args):
        return None

    result = asyncio.run(openhands.run_build({"source_dir": str(source_dir)}, on_event))

    assert stage.sync_count == 1
    assert result["status"] == "failed"
    assert result["preview_dir"] is None
    assert result["verification"]["details"]["recovery_workspace"] == str(stage.stage_dir)
    assert original.read_text(encoding="utf-8") == "user edit"
    assert staged.read_text(encoding="utf-8") == "agent edit"


def test_saved_verification_uses_staged_source_and_maps_preview(tmp_path, monkeypatch):
    source_dir, stage = _prepare(tmp_path, monkeypatch)

    def fake_verify(snapshot):
        _assert_staged_snapshot(snapshot, source_dir, stage)
        index = stage.workspace_dir / "dist" / "index.html"
        index.parent.mkdir()
        index.write_text("verified", encoding="utf-8")
        return {
            "status": "ready", "conversation_id": None,
            "source_dir": str(stage.workspace_dir), "preview_dir": str(index.parent),
            "verification": {"build_status": "passed", "interaction_status": "passed", "details": {}},
            "reason": "verified",
        }

    monkeypatch.setattr(openhands, "_verify_saved_build_sync", fake_verify)

    result = asyncio.run(openhands.verify_saved_build({"source_dir": str(source_dir)}))

    assert stage.sync_count == 1
    assert result["status"] == "ready"
    assert result["source_dir"] == str(source_dir)
    assert result["preview_dir"] == str(source_dir / "dist")
    assert (source_dir / "dist" / "index.html").read_text(encoding="utf-8") == "verified"


def test_native_disk_project_uses_real_staging_and_syncs_preview(tmp_path, monkeypatch):
    assistant_root = tmp_path / "assistant"
    assistant_root.mkdir()
    source_dir = ProjectFolders(assistant_root).create(None, "native-project")
    (source_dir / "app.js").write_text("before", encoding="utf-8")
    state_dir = tmp_path / "runtime" / "agent-state"
    monkeypatch.setattr(openhands, "_agent_state_dir", lambda _source: state_dir)

    def fake_verify(snapshot):
        workspace_dir = Path(snapshot["source_dir"])
        assert workspace_dir != source_dir
        assert snapshot["project_source_dir"] == str(source_dir)
        assert (workspace_dir / "app.js").read_text(encoding="utf-8") == "before"
        assert (source_dir / "app.js").read_text(encoding="utf-8") == "before"
        (workspace_dir / "app.js").write_text("after", encoding="utf-8")
        preview = workspace_dir / "dist"
        preview.mkdir()
        (preview / "index.html").write_text("native preview", encoding="utf-8")
        return {
            "status": "ready", "conversation_id": None,
            "source_dir": str(workspace_dir), "preview_dir": str(preview),
            "verification": {"build_status": "passed", "interaction_status": "passed", "details": {}},
            "reason": "verified",
        }

    monkeypatch.setattr(openhands, "_verify_saved_build_sync", fake_verify)
    result = asyncio.run(openhands.verify_saved_build({"source_dir": str(source_dir)}))

    assert result["status"] == "ready"
    assert result["source_dir"] == str(source_dir)
    assert result["preview_dir"] == str(source_dir / "dist")
    assert (source_dir / "app.js").read_text(encoding="utf-8") == "after"
    assert (source_dir / "dist" / "index.html").read_text(encoding="utf-8") == "native preview"
    assert not list((state_dir.parent / "project-staging" / state_dir.name).iterdir())
