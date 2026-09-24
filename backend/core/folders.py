"""Server-side project folder selection shared by desktop and phone browsers."""

from __future__ import annotations

import os
import string
import sys
import uuid
from datetime import datetime
from pathlib import Path

from .store import StateError


_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
_RESERVED_NAMES.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
_INVALID_NAME_CHARS = set('<>:"/\\|?*')


class ProjectFolders:
    def __init__(self, assistant_root: Path, extra_roots: str = "") -> None:
        self.default_root = assistant_root.resolve().parent
        self.extra_roots = [Path(item).expanduser() for item in extra_roots.split(os.pathsep) if item.strip()]
        self.roots: list[Path] = []
        self.root_labels: dict[Path, str] = {}
        self._refresh_roots()

    @staticmethod
    def _computer_roots() -> list[tuple[Path, str]]:
        """Offer common local locations without requiring .env edits."""
        roots = [(Path.home(), "用户文件夹")]
        if sys.platform == "darwin":
            volumes = Path("/Volumes")
            try:
                roots.extend((child, child.name) for child in volumes.iterdir() if os.path.ismount(child))
            except OSError:
                pass
        elif os.name == "nt":
            import ctypes

            kernel = ctypes.windll.kernel32
            kernel.GetLogicalDrives.restype = ctypes.c_uint
            kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
            kernel.GetDriveTypeW.restype = ctypes.c_uint
            drives = kernel.GetLogicalDrives()
            for index, letter in enumerate(string.ascii_uppercase):
                if drives & (1 << index):
                    drive = Path(f"{letter}:\\")
                    if kernel.GetDriveTypeW(str(drive)) in {2, 3}:  # Removable or fixed disk.
                        roots.append((drive, f"{letter}:"))
        return roots

    @staticmethod
    def _available(path: Path) -> bool:
        # A disconnected macOS volume may leave a plain /Volumes/<name>
        # directory on the startup disk. Never create a project there.
        parts = path.parts
        if sys.platform == "darwin" and len(parts) >= 3 and parts[1] == "Volumes":
            return os.path.ismount(Path("/Volumes") / parts[2])
        return True

    def _refresh_roots(self) -> None:
        candidates = [(self.default_root, "项目旁"), *self._computer_roots()]
        candidates.extend((path, path.name or str(path)) for path in self.extra_roots)
        roots: list[Path] = []
        labels: dict[Path, str] = {}
        for candidate, label in candidates:
            try:
                resolved = candidate.resolve()
                if resolved.is_dir() and self._available(resolved) and resolved not in roots:
                    roots.append(resolved)
                    labels[resolved] = label
            except (OSError, RuntimeError, ValueError):
                continue
        self.roots = roots
        self.root_labels = labels

    @staticmethod
    def _visible_relative(root: Path, path: Path) -> bool:
        relative = path.relative_to(root)
        return all(not part.startswith(".") for part in relative.parts)

    def _allowed(self, path: Path) -> bool:
        return any(path.is_relative_to(root) and self._visible_relative(root, path) for root in self.roots)

    def parent(self, raw: str | None) -> Path:
        self._refresh_roots()
        try:
            path = Path(raw).expanduser().resolve(strict=True) if raw else (self.default_root if self.default_root in self.roots else self.roots[0])
        except (OSError, RuntimeError, ValueError):
            raise StateError("所选文件夹不存在或无法访问。") from None
        except IndexError:
            raise StateError("没有可用的项目存放位置。") from None
        if not path.is_dir() or not self._available(path) or not self._allowed(path):
            raise StateError("只能选择这台电脑上可用的存放位置。")
        return path

    def browse(self, raw: str | None = None) -> dict:
        path = self.parent(raw)
        folders = []
        try:
            children = list(path.iterdir())
        except OSError:
            raise StateError("无法读取这个文件夹。") from None
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                resolved = child.resolve(strict=True)
                if resolved.is_dir() and self._allowed(resolved):
                    folders.append({"name": child.name, "path": str(resolved)})
            except (OSError, RuntimeError):
                continue
        folders.sort(key=lambda item: item["name"].casefold())
        parent = path.parent if self._allowed(path.parent) else None
        return {
            "path": str(path),
            "parent": str(parent) if parent else None,
            "default_path": str(self.default_root if self.default_root in self.roots else self.roots[0]),
            "roots": [{"path": str(root), "label": self.root_labels[root]} for root in self.roots],
            "folders": folders,
        }

    def create(self, raw_parent: str | None, folder_name: str | None) -> Path:
        parent = self.parent(raw_parent)
        name = (folder_name or "").strip() or f"新项目-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        if (
            len(name) > 80 or name in {".", ".."} or name.startswith(".")
            or name.endswith(".") or any(char in _INVALID_NAME_CHARS or ord(char) < 32 for char in name)
            or name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES
        ):
            raise StateError("文件夹名称不能包含系统保留字符或名称、隐藏目录前缀，且不能超过 80 字。")
        target = parent / name
        try:
            if not self._available(parent):
                raise StateError("所选存放位置已断开，请重新选择。")
            target.mkdir()
        except FileExistsError:
            raise StateError("同名文件夹已存在，请换一个项目文件夹名称。") from None
        except OSError:
            raise StateError("无法在所选位置新建项目文件夹，请检查这台电脑上的写入权限。") from None
        return target
