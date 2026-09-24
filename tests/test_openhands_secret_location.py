from __future__ import annotations

import os

from backend.integrations.openhands import _agent_state_dir, _ensure_secret_key, _set_launch_env


def test_new_project_agent_key_stays_in_private_app_data(tmp_path, monkeypatch):
    source = tmp_path / "chosen" / "作品甲"
    source.mkdir(parents=True)
    state = tmp_path / "app-data"
    monkeypatch.setenv("VOICE_COMPANION_DATA_DIR", str(state))

    key = _ensure_secret_key(source)
    assert key == _ensure_secret_key(source)
    assert len(list((state / "agent-keys").glob("*.key"))) == 1
    assert not (source.parent / ".作品甲.openhands-secret-key").exists()


def test_existing_agent_key_remains_usable_after_location_change(tmp_path, monkeypatch):
    source = tmp_path / "existing" / "old-project"
    source.mkdir(parents=True)
    legacy = source.parent / ".old-project.openhands-secret-key"
    legacy.write_text("existing-key", encoding="utf-8")
    monkeypatch.setenv("VOICE_COMPANION_DATA_DIR", str(tmp_path / "app-data"))

    assert _ensure_secret_key(source) == "existing-key"
    assert not (tmp_path / "app-data" / "agent-keys").exists()


def test_agent_state_is_separate_from_selected_source_and_migrates_valid_session(tmp_path, monkeypatch):
    source = tmp_path / "external" / "作品甲"
    old = source / "conversations" / "session-1"
    old.mkdir(parents=True)
    (old / "meta.json").write_text('{"id":"session-1"}', encoding="utf-8")
    (old / "base_state.json").write_text('{"state":"idle"}', encoding="utf-8")
    root = tmp_path / "internal-state"
    monkeypatch.setenv("OPENHANDS_STATE_ROOT", str(root))

    state = _agent_state_dir(source)

    assert state.parent == root
    assert (state / "conversations" / "session-1" / "meta.json").is_file()
    assert (state / "conversations" / "session-1" / "base_state.json").is_file()
    assert (old / "meta.json").is_file()
    assert state == _agent_state_dir(source)


def test_launch_env_uses_dedicated_state_mount_and_restores_process_env(monkeypatch):
    monkeypatch.setenv("OH_CONVERSATIONS_PATH", "previous-value")
    monkeypatch.delenv("OH_PERSISTENCE_DIR", raising=False)
    monkeypatch.delenv("OH_SECRET_KEY", raising=False)

    with _set_launch_env("private-key"):
        assert os.environ["OH_CONVERSATIONS_PATH"] == "/agent-state/conversations"
        assert os.environ["OH_PERSISTENCE_DIR"] == "/agent-state/.openhands"
        assert os.environ["OH_SECRET_KEY"] == "private-key"

    assert os.environ["OH_CONVERSATIONS_PATH"] == "previous-value"
    assert "OH_PERSISTENCE_DIR" not in os.environ
    assert "OH_SECRET_KEY" not in os.environ
