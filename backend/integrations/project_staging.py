"""Stage a selected project on the service computer's native disk for Agent work.

The source folder remains the user's project.  Each run gets a private working
copy below the Agent state directory.  ``sync_back`` applies only changes made
to that copy, checks for concurrent edits in the source, and leaves the copy
intact if the selected folder disappears or a conflict needs attention.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


_EXCLUDED_DIRS = {
    ".git", ".openhands", ".openhands-state", ".venv", ".next",
    ".cache", ".pytest_cache", ".ssh", "__pycache__", "bash_events",
    "conversations", "node_modules", "venv",
}
_EXCLUDED_FILES = {
    ".npmrc", ".pypirc", ".netrc", ".ds_store", "credentials.json",
    "secrets.json", "token.json", "id_rsa", "id_ed25519",
    "decision-request.json",  # A transient Agent control file, not an artifact.
}
_SECRET_SUFFIXES = {".pem", ".p12", ".pfx", ".key", ".jks", ".keystore"}
_MANIFEST = "stage.json"


class ProjectSyncConflict(RuntimeError):
    """A user edit or unsafe path prevents a safe copy back to the project."""

    def __init__(self, paths: list[str]):
        self.paths = sorted(set(paths))
        super().__init__("项目文件在构建期间发生变化，已保留本机工作副本：" + ", ".join(self.paths[:8]))


class ProjectSyncUnavailable(RuntimeError):
    """The selected source folder or its original mount is unavailable."""


def _excluded(relative: PurePosixPath) -> bool:
    for part in relative.parts:
        lower = part.lower()
        if lower.startswith("._") or lower.startswith(".env"):
            return True
        if lower in _EXCLUDED_DIRS or lower in _EXCLUDED_FILES:
            return True
        if lower.endswith(tuple(_SECRET_SUFFIXES)):
            return True
    return False


def _is_link(path: Path) -> bool:
    """Windows junctions need the same protection as symbolic links."""
    return path.is_symlink() or path.is_junction()


def _relative_path(raw: str) -> Path:
    path = PurePosixPath(raw)
    if (not raw or not path.parts or path.is_absolute()
            or (os.name == "nt" and ("\\" in raw or ":" in raw))
            or any(part in {".", ".."} for part in path.parts) or _excluded(path)):
        raise ValueError("Unsafe staged project path")
    return Path(*path.parts)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _scan_tree(root: Path, *, reject_unsafe: bool = False) -> tuple[dict[str, str], set[str]]:
    """Hash ordinary files without traversing symlinks or excluded directories."""
    files: dict[str, str] = {}
    directories: set[str] = set()
    unsafe: list[str] = []
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        relative_dir = current.relative_to(root)
        kept_dirs = []
        for name in names:
            rel = (relative_dir / name).as_posix()
            if _excluded(PurePosixPath(rel)):
                continue
            path = current / name
            if _is_link(path):
                if reject_unsafe:
                    unsafe.append(rel)
                continue
            if not path.is_dir():
                if reject_unsafe:
                    unsafe.append(rel)
                continue
            directories.add(rel)
            kept_dirs.append(name)
        names[:] = kept_dirs
        for name in filenames:
            rel = (relative_dir / name).as_posix()
            if _excluded(PurePosixPath(rel)):
                continue
            path = current / name
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                if reject_unsafe:
                    unsafe.append(rel)
                continue
            files[rel] = _digest(path)
    if unsafe:
        raise ProjectSyncConflict(unsafe)
    return files, directories


def _mountpoint(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if os.path.ismount(candidate):
            return candidate
    return Path(path.anchor)


def _mount_is_present(source_dir: Path, mountpoint: Path) -> bool:
    # A disconnected macOS volume may leave /Volumes/T7 as a plain directory
    # on the startup disk.  Never copy project data into that placeholder.
    parts = source_dir.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        volume = Path("/Volumes") / parts[2]
        if not os.path.ismount(volume):
            return False
    return os.path.ismount(mountpoint)


def _assert_source_available(source_dir: Path, mountpoint: Path) -> None:
    if _is_link(source_dir) or not source_dir.is_dir() or not _mount_is_present(source_dir, mountpoint):
        raise ProjectSyncUnavailable("所选项目目录或外接磁盘不可用；本机工作副本已保留，重新连接后可同步。")


def _assert_safe_target(source_dir: Path, relative: Path) -> Path:
    """Prevent writes through symlinks created in the selected source tree."""
    target = source_dir / relative
    cursor = source_dir
    for part in relative.parts:
        cursor = cursor / part
        if _is_link(cursor):
            raise ProjectSyncConflict([relative.as_posix()])
    return target


def _ensure_source_directory(source_dir: Path, relative: Path, mountpoint: Path) -> Path:
    """Create descendants one at a time without ever recreating the source root."""
    cursor = source_dir
    for part in relative.parts:
        _assert_source_available(source_dir, mountpoint)
        cursor = cursor / part
        if _is_link(cursor):
            raise ProjectSyncConflict([relative.as_posix()])
        if not cursor.exists():
            cursor.mkdir()  # No parents=True: a lost mount cannot recreate /Volumes/T7.
        elif not cursor.is_dir():
            raise ProjectSyncConflict([relative.as_posix()])
    return cursor


def _copy_file(source: Path, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    with os.fdopen(os.open(source, flags), "rb") as readable, destination.open("xb") as writable:
        while block := readable.read(1024 * 1024):
            writable.write(block)
            digest.update(block)
    source_mode = source.stat(follow_symlinks=False).st_mode
    destination.chmod(0o755 if source_mode & 0o111 else 0o644)
    return digest.hexdigest()


def _write_manifest(stage_dir: Path, data: dict[str, object]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".stage-", dir=stage_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, stage_dir / _MANIFEST)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass
class StagedProject:
    source_dir: Path
    workspace_dir: Path
    stage_dir: Path
    mountpoint: Path
    baseline: dict[str, str]
    baseline_directories: set[str]

    @classmethod
    def load(cls, stage_dir: Path) -> "StagedProject":
        """Reopen a retained working copy after a failed sync or process restart."""
        stage_dir = Path(stage_dir).expanduser().resolve(strict=True)
        data = json.loads((stage_dir / _MANIFEST).read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("Unsupported project stage manifest")
        baseline = data.get("baseline")
        dirs = data.get("baseline_directories")
        if not isinstance(baseline, dict) or not isinstance(dirs, list):
            raise ValueError("Invalid project stage manifest")
        for rel, digest in baseline.items():
            _relative_path(rel)
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError("Invalid project stage manifest")
        for rel in dirs:
            _relative_path(rel)
        workspace_dir = stage_dir / "workspace"
        if _is_link(workspace_dir) or not workspace_dir.is_dir():
            raise ValueError("Staged workspace is missing or unsafe")
        return cls(
            source_dir=Path(data["source_dir"]), workspace_dir=workspace_dir,
            stage_dir=stage_dir, mountpoint=Path(data["mountpoint"]),
            baseline=baseline, baseline_directories=set(dirs),
        )

    def sync_back(self) -> None:
        """Copy Agent changes into the chosen folder, preserving concurrent edits.

        This is deliberately repeatable.  A lost mount or partial I/O failure
        leaves the full staged tree and manifest in place for a later retry.
        """
        try:
            self._sync_back()
        except OSError as exc:
            raise ProjectSyncUnavailable(
                "同步项目文件时磁盘不可用或发生 I/O 错误；本机工作副本已保留，可重新连接后重试。"
            ) from exc

    def _sync_back(self) -> None:
        _assert_source_available(self.source_dir, self.mountpoint)
        if _is_link(self.workspace_dir) or not self.workspace_dir.is_dir():
            raise ProjectSyncUnavailable("本机工作副本不可用。")
        staged, staged_dirs = _scan_tree(self.workspace_dir, reject_unsafe=True)
        source, _source_dirs = _scan_tree(self.source_dir)
        changed = sorted(rel for rel in self.baseline.keys() | staged.keys()
                         if self.baseline.get(rel) != staged.get(rel))
        conflicts: list[str] = []
        for rel in changed:
            relative = _relative_path(rel)
            try:
                _assert_safe_target(self.source_dir, relative)
            except ProjectSyncConflict:
                conflicts.append(rel)
                continue
            old = self.baseline.get(rel)
            present = source.get(rel)
            desired = staged.get(rel)
            # Another process may have made the same change; that is safe.
            if present != old and present != desired:
                conflicts.append(rel)
            elif (self.source_dir / relative).exists() and present is None:
                # A directory or special file now occupies the file path.
                conflicts.append(rel)
        if conflicts:
            raise ProjectSyncConflict(conflicts)

        for rel in changed:
            _assert_source_available(self.source_dir, self.mountpoint)
            relative = _relative_path(rel)
            target = _assert_safe_target(self.source_dir, relative)
            desired = staged.get(rel)
            expected = source.get(rel)
            current = _digest(target) if target.is_file() else None
            if current != expected:
                if current == desired:
                    continue
                raise ProjectSyncConflict([rel])
            if desired is None:
                if target.exists():
                    target.unlink()
                continue
            if current == desired:
                continue
            parent = _ensure_source_directory(self.source_dir, relative.parent, self.mountpoint)
            _assert_safe_target(self.source_dir, relative)
            fd, temporary = tempfile.mkstemp(prefix=".agent-sync-", dir=parent)
            os.close(fd)
            try:
                with os.fdopen(os.open(self.workspace_dir / relative, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as readable, open(temporary, "wb") as writable:
                    shutil.copyfileobj(readable, writable, length=1024 * 1024)
                    writable.flush()
                    os.fsync(writable.fileno())
                if _digest(Path(temporary)) != desired:
                    raise ProjectSyncConflict([rel])
                source_mode = (self.workspace_dir / relative).stat(follow_symlinks=False).st_mode
                os.chmod(temporary, 0o755 if source_mode & 0o111 else 0o644)
                _assert_source_available(self.source_dir, self.mountpoint)
                _assert_safe_target(self.source_dir, relative)
                current = _digest(target) if target.is_file() else None
                if current != expected:
                    if current == desired:
                        continue
                    raise ProjectSyncConflict([rel])
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        for rel in sorted(staged_dirs - self.baseline_directories, key=lambda p: (p.count("/"), p)):
            _ensure_source_directory(self.source_dir, _relative_path(rel), self.mountpoint)
        for rel in sorted(self.baseline_directories - staged_dirs, key=lambda p: (-p.count("/"), p)):
            target = _assert_safe_target(self.source_dir, _relative_path(rel))
            if target.is_dir():
                try:
                    target.rmdir()
                except OSError:
                    pass  # Preserve a directory containing concurrent user files.
        _assert_source_available(self.source_dir, self.mountpoint)
        for rel in changed:
            target = _assert_safe_target(self.source_dir, _relative_path(rel))
            actual = _digest(target) if target.is_file() else None
            if actual != staged.get(rel):
                raise ProjectSyncConflict([rel])
        data = json.loads((self.stage_dir / _MANIFEST).read_text(encoding="utf-8"))
        data["synced"] = True
        _write_manifest(self.stage_dir, data)


def stage_project(source_dir: Path, state_dir: Path) -> StagedProject:
    """Create a new native-disk working copy of a selected project folder."""
    source_dir = Path(source_dir).expanduser()
    if _is_link(source_dir) or not source_dir.is_dir():
        raise ProjectSyncUnavailable("所选项目目录或外接磁盘不可用。")
    source_dir = source_dir.resolve(strict=True)
    mountpoint = _mountpoint(source_dir)
    _assert_source_available(source_dir, mountpoint)
    state_dir = Path(state_dir).expanduser()
    if _is_link(state_dir):
        raise ValueError("Agent state directory cannot be a symbolic link")
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir = state_dir.resolve(strict=True)
    if state_dir.is_relative_to(source_dir) or source_dir.is_relative_to(state_dir):
        raise ValueError("Agent state and project source directories must be separate")
    # Docker mounts state_dir at /agent-state.  Keep the sync manifest outside
    # that mount so Agent edits cannot forge the baseline used for conflict
    # detection.  Only stage_dir/workspace is mounted separately at /workspace.
    stage_root = state_dir.parent / "project-staging" / state_dir.name
    if _is_link(stage_root):
        raise ValueError("Project staging directory cannot be a symbolic link")
    if stage_root.is_dir():
        for previous in sorted(stage_root.iterdir()):
            if _is_link(previous) or not previous.is_dir() or not (previous / _MANIFEST).is_file():
                continue
            prior = StagedProject.load(previous)
            if prior.source_dir != source_dir:
                raise ValueError("A pending project stage belongs to a different source directory")
            manifest = json.loads((previous / _MANIFEST).read_text(encoding="utf-8"))
            if not manifest.get("synced"):
                # Recover an interrupted sync before taking a new baseline.
                # A conflict blocks a fresh stage so unsynced Agent work is not
                # silently abandoned after the external disk returns.
                prior.sync_back()
    stage_dir = stage_root / uuid.uuid4().hex
    stage_dir.mkdir(mode=0o700, parents=True)
    workspace_dir = stage_dir / "workspace"
    workspace_dir.mkdir(mode=0o700)
    baseline: dict[str, str] = {}
    baseline_dirs: set[str] = set()
    for directory, names, filenames in os.walk(source_dir, topdown=True, followlinks=False):
        current = Path(directory)
        relative_dir = current.relative_to(source_dir)
        kept_dirs = []
        for name in names:
            rel = (relative_dir / name).as_posix()
            if _excluded(PurePosixPath(rel)) or _is_link(current / name):
                continue
            if (current / name).is_dir():
                baseline_dirs.add(rel)
                (workspace_dir / rel).mkdir(parents=True, exist_ok=True)
                kept_dirs.append(name)
        names[:] = kept_dirs
        for name in filenames:
            rel = (relative_dir / name).as_posix()
            if _excluded(PurePosixPath(rel)):
                continue
            path = current / name
            if not stat.S_ISREG(path.lstat().st_mode):
                continue
            baseline[rel] = _copy_file(path, workspace_dir / rel)
    _write_manifest(stage_dir, {
        "version": 1, "source_dir": str(source_dir), "mountpoint": str(mountpoint),
        "baseline": baseline, "baseline_directories": sorted(baseline_dirs), "synced": False,
    })
    return StagedProject(source_dir, workspace_dir, stage_dir, mountpoint, baseline, baseline_dirs)
