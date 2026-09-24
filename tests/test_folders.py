from __future__ import annotations

import pytest

from backend.core.folders import ProjectFolders
from backend.core.store import StateError


def test_folder_picker_creates_new_sibling_and_rejects_collision(tmp_path):
    folders = ProjectFolders(tmp_path / "assistant")
    assert folders.browse()["default_path"] == str(tmp_path)

    target = folders.create(None, "学生工具")
    assert target == tmp_path / "学生工具"
    assert target.is_dir()
    assert {item["name"] for item in folders.browse()["folders"]} == {"学生工具"}
    with pytest.raises(StateError, match="同名文件夹"):
        folders.create(None, "学生工具")


def test_folder_picker_rejects_traversal_and_symlink_escape(tmp_path):
    folders = ProjectFolders(tmp_path / "assistant")
    outside = tmp_path.parent
    (tmp_path / "external").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StateError):
        folders.parent(str(outside))
    with pytest.raises(StateError):
        folders.parent(str(tmp_path / "external"))
    with pytest.raises(StateError):
        folders.create(None, "../escape")
    assert "external" not in {item["name"] for item in folders.browse()["folders"]}


def test_existing_home_folder_can_be_selected_without_precreating_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    parent = home / "Documents"
    parent.mkdir(parents=True)
    monkeypatch.setattr(ProjectFolders, "_computer_roots", staticmethod(lambda: [(home, "用户文件夹")]))

    folders = ProjectFolders(tmp_path / "assistant")
    assert {item["path"] for item in folders.browse()["roots"]} >= {str(tmp_path), str(home)}
    assert folders.browse(str(parent))["path"] == str(parent)

    created = folders.create(str(parent), None)
    assert created.parent == parent
    assert created.is_dir()
    assert created.name.startswith("新项目-")


def test_unavailable_default_volume_falls_back_to_home(tmp_path, monkeypatch):
    external = tmp_path / "external"
    external.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ProjectFolders, "_computer_roots", staticmethod(lambda: [(home, "用户文件夹")]))
    monkeypatch.setattr(ProjectFolders, "_available", staticmethod(lambda path: not path.is_relative_to(external)))

    folders = ProjectFolders(external / "assistant")

    assert folders.browse()["default_path"] == str(home)
    with pytest.raises(StateError):
        folders.create(str(external), "project")


@pytest.mark.parametrize("name", ["CON", "con.txt", "LPT1.log", "bad:name", "bad?.js", "trailing."])
def test_folder_picker_rejects_windows_reserved_names_on_every_host(tmp_path, name):
    folders = ProjectFolders(tmp_path / "assistant")
    with pytest.raises(StateError, match="系统保留"):
        folders.create(None, name)
