from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from backend.integrations import project_staging
from backend.integrations.project_staging import (
    ProjectSyncConflict,
    ProjectSyncUnavailable,
    StagedProject,
    stage_project,
)


def test_stages_safe_files_and_syncs_edits_additions_and_deletions(tmp_path):
    source = tmp_path / "external" / "my-project"
    (source / "src").mkdir(parents=True)
    (source / "dist" / "assets").mkdir(parents=True)
    (source / "node_modules" / "pkg").mkdir(parents=True)
    (source / ".git").mkdir()
    (source / "conversations").mkdir()
    (source / "src" / "app.js").write_text("old", encoding="utf-8")
    (source / "src" / "obsolete.js").write_text("delete", encoding="utf-8")
    (source / "dist" / "assets" / "old.js").write_text("old asset", encoding="utf-8")
    (source / "node_modules" / "pkg" / "index.js").write_text("dependency", encoding="utf-8")
    (source / ".git" / "config").write_text("private", encoding="utf-8")
    (source / "conversations" / "events.json").write_text("private", encoding="utf-8")
    (source / ".env.local").write_text("SECRET=keep", encoding="utf-8")
    (source / ".npmrc").write_text("token=keep", encoding="utf-8")
    (source / "credentials.json").write_text("secret", encoding="utf-8")
    (source / "._app.js").write_text("AppleDouble", encoding="utf-8")

    state = tmp_path / "native" / "state"
    staged = stage_project(source, state)

    assert staged.workspace_dir != source
    assert not staged.workspace_dir.is_relative_to(state)  # Not exposed through /agent-state.
    assert (staged.workspace_dir / "src" / "app.js").read_text() == "old"
    assert (staged.workspace_dir / "dist" / "assets" / "old.js").read_text() == "old asset"
    for excluded in ("node_modules", ".git", "conversations", ".env.local", ".npmrc", "credentials.json", "._app.js"):
        assert not (staged.workspace_dir / excluded).exists()

    (staged.workspace_dir / "src" / "app.js").write_text("new", encoding="utf-8")
    (staged.workspace_dir / "src" / "obsolete.js").unlink()
    (staged.workspace_dir / "dist" / "assets" / "new.js").write_text("new asset", encoding="utf-8")
    (staged.workspace_dir / "dist" / "assets" / "old.js").unlink()
    (staged.workspace_dir / ".env.production").write_text("SECRET=agent", encoding="utf-8")
    staged.sync_back()

    assert (source / "src" / "app.js").read_text() == "new"
    assert not (source / "src" / "obsolete.js").exists()
    assert (source / "dist" / "assets" / "new.js").read_text() == "new asset"
    assert not (source / "dist" / "assets" / "old.js").exists()
    assert (source / ".env.local").read_text() == "SECRET=keep"
    assert not (source / ".env.production").exists()
    assert (source / "node_modules" / "pkg" / "index.js").read_text() == "dependency"
    assert json.loads((staged.stage_dir / "stage.json").read_text())["synced"] is True


def test_source_edits_to_untouched_files_survive_and_same_result_is_idempotent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("old A")
    (source / "b.txt").write_text("old B")
    staged = stage_project(source, tmp_path / "state")
    (staged.workspace_dir / "a.txt").write_text("new A")
    (source / "b.txt").write_text("user B")

    staged.sync_back()
    staged.sync_back()

    assert (source / "a.txt").read_text() == "new A"
    assert (source / "b.txt").read_text() == "user B"


def test_conflicting_user_edit_preserved_and_staged_copy_can_be_reopened(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.js").write_text("before")
    staged = stage_project(source, tmp_path / "state")
    (staged.workspace_dir / "app.js").write_text("agent edit")
    (source / "app.js").write_text("user edit")

    with pytest.raises(ProjectSyncConflict) as exc:
        staged.sync_back()

    assert exc.value.paths == ["app.js"]
    assert (source / "app.js").read_text() == "user edit"
    reopened = StagedProject.load(staged.stage_dir)
    assert (reopened.workspace_dir / "app.js").read_text() == "agent edit"
    assert json.loads((staged.stage_dir / "stage.json").read_text())["synced"] is False


def test_missing_source_is_not_recreated_and_stage_survives(tmp_path):
    source = tmp_path / "external" / "project"
    source.mkdir(parents=True)
    (source / "app.js").write_text("before")
    staged = stage_project(source, tmp_path / "native" / "state")
    (staged.workspace_dir / "new" / "nested").mkdir(parents=True)
    (staged.workspace_dir / "new" / "nested" / "app.js").write_text("agent edit")
    shutil.rmtree(source)

    with pytest.raises(ProjectSyncUnavailable):
        staged.sync_back()

    assert not source.exists()
    assert (staged.workspace_dir / "new" / "nested" / "app.js").read_text() == "agent edit"
    source.mkdir()
    (source / "app.js").write_text("before")
    StagedProject.load(staged.stage_dir).sync_back()
    assert (source / "new" / "nested" / "app.js").read_text() == "agent edit"


def test_next_stage_recovers_pending_sync_before_copying_source(tmp_path):
    source = tmp_path / "external" / "project"
    source.mkdir(parents=True)
    (source / "app.js").write_text("before")
    state = tmp_path / "native" / "state"
    first = stage_project(source, state)
    (first.workspace_dir / "app.js").write_text("agent edit")

    second = stage_project(source, state)

    assert (source / "app.js").read_text() == "agent edit"
    assert (second.workspace_dir / "app.js").read_text() == "agent edit"
    assert first.stage_dir != second.stage_dir


def test_next_stage_does_not_abandon_pending_conflict(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.js").write_text("before")
    state = tmp_path / "state"
    first = stage_project(source, state)
    (first.workspace_dir / "app.js").write_text("agent edit")
    (source / "app.js").write_text("user edit")

    with pytest.raises(ProjectSyncConflict):
        stage_project(source, state)

    assert (source / "app.js").read_text() == "user edit"
    assert (first.workspace_dir / "app.js").read_text() == "agent edit"


def test_missing_mount_does_not_write_even_if_path_exists(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    staged = stage_project(source, tmp_path / "state")
    (staged.workspace_dir / "new.txt").write_text("artifact")
    monkeypatch.setattr(project_staging, "_mount_is_present", lambda _source, _mount: False)

    with pytest.raises(ProjectSyncUnavailable):
        staged.sync_back()

    assert not (source / "new.txt").exists()
    assert (staged.workspace_dir / "new.txt").exists()


def test_symlinks_are_never_followed_or_synced(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (source / "external-link").symlink_to(outside)
    (source / "app.js").write_text("before")
    staged = stage_project(source, tmp_path / "state")
    assert not (staged.workspace_dir / "external-link").exists()
    (staged.workspace_dir / "app.js").unlink()
    (staged.workspace_dir / "app.js").symlink_to(outside)

    with pytest.raises(ProjectSyncConflict):
        staged.sync_back()

    assert (source / "app.js").read_text() == "before"
    assert outside.read_text() == "private"


def test_user_created_symlink_parent_cannot_redirect_new_agent_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    staged = stage_project(source, tmp_path / "state")
    (staged.workspace_dir / "assets").mkdir()
    (staged.workspace_dir / "assets" / "new.js").write_text("agent")
    (source / "assets").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectSyncConflict):
        staged.sync_back()

    assert not (outside / "new.js").exists()


def test_windows_junction_like_directory_is_not_staged(tmp_path, monkeypatch):
    source = tmp_path / "source"
    junction = source / "junction"
    junction.mkdir(parents=True)
    (junction / "private.txt").write_text("private")
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == junction or original(path))

    staged = stage_project(source, tmp_path / "state")

    assert not (staged.workspace_dir / "junction").exists()
