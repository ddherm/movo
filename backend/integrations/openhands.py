"""OpenHands SDK execution inside a per-project Docker workspace.

Only the project's source tree and a separate SDK state directory are mounted.
The host never runs generated code. All SDK imports are deferred so that the
backend can start and report an unavailable integration without the SDK.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

from .project_staging import stage_project

logger = logging.getLogger(__name__)

EventCallback = Callable[[str, str, dict[str, Any] | None], Awaitable[None]]
QuestionCallback = Callable[[dict[str, Any]], Awaitable[str]]

_active: dict[str, Any] = {}
_active_lock = threading.RLock()
_env_lock = threading.RLock()


_TEST_COMMAND = re.compile(
    r"^\s*(?:(?:npm|pnpm|yarn)\s+(?:run\s+)?test(?:[:\w-]*)?|"
    r"(?:python(?:3)?\s+-m\s+)?pytest|"
    r"npx\s+playwright\s+test)(?:\s|$)",
    re.IGNORECASE,
)
_BUILD_COMMAND = re.compile(
    r"^\s*(?:npm|pnpm|yarn)\s+(?:run\s+)?build(?:\s|$)", re.IGNORECASE
)
_INSTALL_COMMAND = re.compile(
    r"^\s*(?:npm\s+(?:ci|install)|pnpm\s+install|yarn\s+install)(?:\s|$)",
    re.IGNORECASE,
)


def _agent_event_summary(event: Any) -> str | None:
    """Describe SDK events without persisting commands, paths, or model text."""
    name = type(event).__name__
    if name == "ActionEvent":
        tool_name = getattr(event, "tool_name", None)
        action = getattr(event, "action", None)
        if tool_name == "file_editor":
            command = getattr(action, "command", None)
            if command == "view":
                return "Agent 正在查看项目文件"
            if command == "create":
                return "Agent 正在创建项目文件"
            if command in {"str_replace", "insert", "undo_edit"}:
                return "Agent 正在修改项目文件"
            return "Agent 正在操作项目文件"
        if tool_name in {"apply_patch", "write_file", "edit"}:
            return "Agent 正在修改项目文件"
        if tool_name in {"read_file", "list_directory", "grep", "glob"}:
            return "Agent 正在查看项目文件"
        if tool_name == "terminal":
            command = getattr(action, "command", None)
            if isinstance(command, str):
                if _TEST_COMMAND.search(command):
                    return "Agent 正在运行项目测试"
                if _BUILD_COMMAND.search(command):
                    return "Agent 正在执行构建检查"
                if _INSTALL_COMMAND.search(command):
                    return "Agent 正在安装项目依赖"
            return "Agent 正在执行终端操作"
        if tool_name == "task_tracker":
            return "Agent 正在更新任务清单"
        if tool_name == "think":
            return "Agent 正在规划实现步骤"
        if tool_name == "finish":
            return "Agent 正在提交构建结果"
        return "Agent 正在执行开发操作"
    if name == "ObservationEvent":
        tool_name = getattr(event, "tool_name", None)
        if tool_name in {"file_editor", "apply_patch", "write_file", "edit"}:
            return "项目文件操作已返回结果"
        if tool_name == "terminal":
            return "终端操作已返回结果"
        if tool_name == "task_tracker":
            return "任务清单操作已返回结果"
        return "开发工具已返回结果"
    return {
        "ConversationErrorEvent": "Agent 执行遇到问题",
        "ConversationStateUpdateEvent": "Agent 状态已更新",
    }.get(name)


def _loopback_docker_workspace_type(docker_type: type, remote_type: type) -> type:
    """Pin the published agent-server port to host loopback.

    OpenHands DockerWorkspace maps ``host_port:8000`` on every host
    interface. Its launch method has no bind-address option. This small
    subclass keeps the SDK lifecycle/API while replacing that one launch
    command with ``127.0.0.1:host_port:8000``.
    """
    from openhands.workspace.docker.workspace import check_port_available, find_available_tcp_port

    class LoopbackDockerWorkspace(docker_type):
        def _start_container(self, image: str, context: Any) -> None:
            self._image_name = image
            if self.host_port is None:
                self.host_port = find_available_tcp_port()
            else:
                self.host_port = int(self.host_port)
            if self.host_port < 1 or not check_port_available(self.host_port):
                raise RuntimeError("No available local Docker port")
            if self.extra_ports:
                raise ValueError("Extra exposed ports are disabled for this private workspace")
            if subprocess.run(["docker", "version"], capture_output=True, timeout=10, check=False).returncode:
                raise RuntimeError("Docker Desktop/daemon is unavailable")
            command = [
                "docker", "run", "-d", "--platform", self.platform, "--rm",
                "--ulimit", "nofile=65536:65536", "--name", f"agent-server-{uuid.uuid4()}",
            ]
            for name in self.forward_env:
                if name in os.environ:
                    command += ["-e", f"{name}={os.environ[name]}"]
            for volume in self.volumes:
                command += ["-v", volume]
            command += ["-p", f"127.0.0.1:{self.host_port}:8000"]
            if self.network:
                command += ["--network", self.network]
            command += [image, "--host", "0.0.0.0", "--port", "8000"]
            started = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
            if started.returncode:
                raise RuntimeError(f"Docker container failed to start: {started.stderr[:300]}")
            self._container_id = started.stdout.strip()
            object.__setattr__(self, "host", f"http://127.0.0.1:{self.host_port}")
            object.__setattr__(self, "api_key", None)
            try:
                self._wait_for_health(timeout=self.health_check_timeout)
                remote_type.model_post_init(self, context)
            except Exception:
                self.cleanup()
                raise

    return LoopbackDockerWorkspace


def build_status(*, require_api_key: bool = True) -> dict[str, Any]:
    """Check local prerequisites without starting a container or spending API credits."""
    missing: list[str] = []
    if require_api_key and not os.getenv("DEEPSEEK_API_KEY", "").strip():
        missing.append("DEEPSEEK_API_KEY")
    try:
        import openhands.sdk  # noqa: F401
        import openhands.tools.preset.default  # noqa: F401
        import openhands.workspace  # noqa: F401
        import pydantic  # noqa: F401
    except ImportError:
        missing.append("OpenHands SDK packages")
    if not shutil.which("docker"):
        missing.append("Docker CLI")
    else:
        try:
            probe = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                timeout=6,
                check=False,
            )
            if probe.returncode:
                missing.append("Docker daemon/socket")
        except (OSError, subprocess.TimeoutExpired):
            missing.append("Docker daemon/socket")
    return {"available": not missing, "missing": missing}


def _result(
    status: str,
    source_dir: Path | None,
    reason: str,
    conversation_id: str | None = None,
    *,
    build_status_value: str = "not_run",
    interaction_status: str = "not_run",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview = source_dir / "dist" if source_dir and status == "ready" else None
    return {
        "status": status,
        "conversation_id": conversation_id,
        "preview_dir": str(preview) if preview and preview.is_dir() else None,
        "source_dir": str(source_dir) if source_dir else None,
        "verification": {
            "build_status": build_status_value,
            "interaction_status": interaction_status,
            "details": details or {},
        },
        "reason": reason,
    }


def _source_dir(snapshot: dict[str, Any]) -> Path | None:
    raw = snapshot.get("source_dir") or snapshot.get("workspace_dir")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else None


def _ensure_secret_key(source_dir: Path) -> str:
    """Keep the server encryption key outside both mounted directories."""
    legacy_path = source_dir.parent / f".{source_dir.name}.openhands-secret-key"
    if legacy_path.is_file():
        existing = legacy_path.read_text(encoding="utf-8").strip()
        if not existing:
            raise RuntimeError("saved Agent encryption key is empty")
        return existing
    data_root = os.getenv("VOICE_COMPANION_DATA_DIR", "").strip()
    if data_root:
        root = Path(data_root).expanduser()
        if not root.is_absolute():
            root = Path(__file__).resolve().parents[2] / root
        key_dir = root.resolve() / "agent-keys"
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_path = key_dir / f"{hashlib.sha256(str(source_dir.resolve()).encode()).hexdigest()}.key"
    else:
        key_path = legacy_path
    try:
        fd = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = key_path.read_text(encoding="utf-8").strip()
        if not existing:
            raise RuntimeError("saved Agent encryption key is empty")
        return existing
    key = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(key)
    return key


def _agent_state_dir(source_dir: Path) -> Path:
    """Keep OpenHands leases and events on the Mac's native filesystem.

    An exFAT project folder can hold generated source, but OpenHands' file
    locks fail there during conversation creation. The per-project state mount
    is private and lives under the user's home by default.
    """
    configured = os.getenv("OPENHANDS_STATE_ROOT", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".voice-programming-companion" / "agent-state"
    if not root.is_absolute():
        raise RuntimeError("OPENHANDS_STATE_ROOT must be an absolute path")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir = root.resolve() / hashlib.sha256(str(source_dir.resolve()).encode()).hexdigest()
    if state_dir.is_symlink():
        raise RuntimeError("OpenHands state directory cannot be a symbolic link")
    state_dir.mkdir(mode=0o700, exist_ok=True)

    # Earlier versions persisted sessions inside the project. Copy a valid
    # session once so an approved project can still attach to its existing ID.
    legacy_conversations = source_dir / "conversations"
    target_conversations = state_dir / "conversations"
    if legacy_conversations.is_symlink():
        raise RuntimeError("Legacy OpenHands state cannot be a symbolic link")
    has_legacy_session = legacy_conversations.is_dir() and any(legacy_conversations.glob("*/meta.json"))
    has_migrated_session = target_conversations.is_dir() and any(target_conversations.glob("*/meta.json"))
    if has_legacy_session and not has_migrated_session:
        shutil.copytree(legacy_conversations, target_conversations, dirs_exist_ok=True, symlinks=True)
        legacy_settings = source_dir / ".openhands-state"
        if legacy_settings.is_symlink():
            raise RuntimeError("Legacy OpenHands settings cannot be a symbolic link")
        if legacy_settings.is_dir():
            shutil.copytree(legacy_settings, state_dir / ".openhands", dirs_exist_ok=True, symlinks=True)
    return state_dir


def _set_launch_env(encryption_key: str):
    """Temporarily set only names DockerWorkspace forwards during construction."""
    class LaunchEnv:
        def __enter__(self):
            _env_lock.acquire()
            self.old = {name: os.environ.get(name) for name in ("OH_PERSISTENCE_DIR", "OH_CONVERSATIONS_PATH", "OH_SECRET_KEY")}
            os.environ["OH_PERSISTENCE_DIR"] = "/agent-state/.openhands"
            os.environ["OH_CONVERSATIONS_PATH"] = "/agent-state/conversations"
            os.environ["OH_SECRET_KEY"] = encryption_key

        def __exit__(self, *_args):
            for name, old_value in self.old.items():
                if old_value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old_value
            _env_lock.release()

    return LaunchEnv()


def _safe_error(exc: BaseException) -> str:
    message = str(exc)
    for name in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "NTFY_TOKEN"):
        secret = os.getenv(name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    return f"{type(exc).__name__}: {message[:280]}"


def _conversation_start_error(exc: BaseException, workspace: Any) -> str:
    """Preserve a small, redacted server exception before --rm removes logs."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status != 500:
        return _safe_error(exc)

    from backend.core.diagnostics import _safe_text

    hint = ""
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = body.get("exception") or body.get("detail")
                if isinstance(detail, str) and detail.lower() not in {"internal server error", "server error"}:
                    hint = _safe_text(detail, 170)
        except (ValueError, TypeError, AttributeError):
            pass
    container_id = getattr(workspace, "_container_id", None)
    if not hint and isinstance(container_id, str) and container_id:
        try:
            logs = subprocess.run(
                ["docker", "logs", "--tail", "120", container_id],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if logs.returncode == 0:
                # Keep only the final exception line. Full logs may contain
                # user input or tool output and must not enter project state.
                lines = (logs.stderr + "\n" + logs.stdout).splitlines()
                candidates = [
                    line.strip() for line in lines
                    if re.search(r"(?:[A-Za-z]+(?:Error|Exception)|permission denied|operation not permitted|not supported)", line, re.I)
                ]
                if candidates:
                    hint = _safe_text(candidates[-1], 170)
        except (OSError, subprocess.TimeoutExpired):
            pass
    base = "HTTPStatusError: 500 /api/conversations"
    return f"{base}；服务端异常：{hint}" if hint else base


def _read_question(source_dir: Path) -> dict[str, Any] | None:
    path = source_dir / "decision-request.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    question = data.get("question")
    options = data.get("options")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(options, list) or len(options) < 2:
        return None
    clean_options = []
    seen_ids: set[str] = set()
    for option in options[:4]:
        if not isinstance(option, dict):
            return None
        option_id, label, impact = option.get("id"), option.get("label"), option.get("impact")
        if not isinstance(option_id, str) or not isinstance(label, str) or not isinstance(impact, str):
            return None
        option_id = option_id[:80]
        if not option_id or option_id in seen_ids:
            return None
        seen_ids.add(option_id)
        clean_options.append({"id": option_id, "label": label[:120], "impact": impact[:500]})
    recommended = data.get("recommended_option_id")
    if not isinstance(recommended, str) or recommended not in seen_ids:
        return None
    return {
        "trigger": str(data.get("trigger", ""))[:500],
        "question": question[:500],
        "impact": str(data.get("impact", ""))[:500],
        "options": clean_options,
        "recommended_option_id": recommended,
        "blocking_scope": str(data.get("blocking_scope", "build"))[:120],
    }


def _verify_site(workspace: Any, source_dir: Path) -> tuple[str, str, dict[str, Any]]:
    """Build and run an explicit interaction command inside Docker."""
    from backend.core.diagnostics import _safe_text

    def failed_command(reason: str, result: Any) -> dict[str, Any]:
        stdout = str(getattr(result, "stdout", "") or "")[-180:]
        stderr = str(getattr(result, "stderr", "") or "")
        error_head = stderr[:450]
        error_tail = ("\n...\n" + stderr[-100:]) if len(stderr) > 450 else ""
        output = (stdout + "\n" + error_head + error_tail).strip()
        return {"reason": reason, "exit_code": result.exit_code, "output": _safe_text(output, 700)}

    details: dict[str, Any] = {}
    manifest = source_dir / "package.json"
    if not manifest.is_file():
        return "failed", "not_run", {"reason": "package.json missing"}
    try:
        package = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "failed", "not_run", {"reason": "package.json invalid"}
    scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
    if not isinstance(scripts, dict) or not isinstance(scripts.get("build"), str):
        return "failed", "not_run", {"reason": "npm build script missing"}
    install = "npm ci --no-audit --no-fund" if (source_dir / "package-lock.json").is_file() else "npm install --no-audit --no-fund"
    install_result = workspace.execute_command(install, cwd="/workspace", timeout=180)
    if install_result.exit_code != 0 or install_result.timeout_occurred:
        return "failed", "not_run", failed_command("dependency install failed", install_result)
    build_result = workspace.execute_command("npm run build", cwd="/workspace", timeout=180)
    if build_result.exit_code != 0 or build_result.timeout_occurred:
        return "failed", "not_run", failed_command("build command failed", build_result)
    dist_dir = source_dir / "dist"
    if not dist_dir.resolve().is_relative_to(source_dir.resolve()):
        return "failed", "not_run", {"reason": "dist directory escapes the project source"}
    preview = dist_dir / "index.html"
    if not preview.is_file():
        return "failed", "not_run", {"reason": "dist/index.html missing after build"}
    if not preview.resolve().is_relative_to(dist_dir.resolve()):
        return "failed", "not_run", {"reason": "dist/index.html escapes the preview directory"}
    class AssetReferences(HTMLParser):
        def __init__(self):
            super().__init__()
            self.refs: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attributes = dict(attrs)
            if tag == "script" and attributes.get("src"):
                self.refs.append(attributes["src"])
            elif tag == "link" and attributes.get("href"):
                self.refs.append(attributes["href"])

    references = AssetReferences()
    references.feed(preview.read_text(encoding="utf-8"))
    dist_dir = preview.parent.resolve()
    for reference in references.refs:
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            continue
        if url.path.startswith("/"):
            return "failed", "not_run", {"reason": "preview asset uses an absolute URL", "asset": reference[:160]}
        asset = (dist_dir / unquote(url.path)).resolve()
        if not asset.is_relative_to(dist_dir) or not asset.is_file():
            return "failed", "not_run", {"reason": "preview asset is missing", "asset": reference[:160]}
    details["entrypoint"] = "dist/index.html"
    details["checked_assets"] = len(references.refs)
    if not isinstance(scripts.get("test:interaction"), str):
        return "passed", "not_run", {**details, "reason": "test:interaction script missing"}
    test_result = workspace.execute_command("npm run test:interaction", cwd="/workspace", timeout=120)
    if test_result.exit_code != 0 or test_result.timeout_occurred:
        return "passed", "failed", {**details, **failed_command("interaction check failed", test_result)}
    return "passed", "passed", details


def _open_workspace(source_dir: Path, *, project_source_dir: Path | None = None) -> Any:
    """Start an isolated loopback workspace over a native-disk working tree."""
    from openhands.sdk.workspace import RemoteWorkspace
    from openhands.workspace import DockerWorkspace

    project_source_dir = project_source_dir or source_dir
    state_dir = _agent_state_dir(project_source_dir)
    dependencies_dir = state_dir / "node_modules"
    dependencies_dir.mkdir(mode=0o700, exist_ok=True)
    encryption_key = _ensure_secret_key(project_source_dir)
    machine = platform.machine().lower()
    docker_platform = "linux/arm64" if "arm" in machine or "aarch64" in machine else "linux/amd64"
    image = os.getenv("OPENHANDS_SERVER_IMAGE", "ghcr.io/openhands/agent-server:1.49.5-python")
    LoopbackDockerWorkspace = _loopback_docker_workspace_type(DockerWorkspace, RemoteWorkspace)
    with _set_launch_env(encryption_key):
        return LoopbackDockerWorkspace(
            server_image=image,
            platform=docker_platform,
            working_dir="/workspace",
            # npm extraction on exFAT can leave zero-byte dependency files.
            # Keep the large, disposable dependency tree on the native disk too.
            volumes=[
                f"{source_dir}:/workspace",
                f"{state_dir}:/agent-state",
                f"{dependencies_dir}:/workspace/node_modules",
            ],
            forward_env=["OH_PERSISTENCE_DIR", "OH_CONVERSATIONS_PATH", "OH_SECRET_KEY"],
            detach_logs=False,
        )


def _is_iteration_limit_error(exc: BaseException) -> bool:
    """Match the SDK's structured run error, never wording in an error string."""
    try:
        from openhands.sdk.conversation.exceptions import ConversationRunError
    except ImportError:
        return False
    return isinstance(exc, ConversationRunError) and getattr(exc.conversation_error, "code", None) == "MaxIterationsReached"


def _run_failure_result(
    exc: Exception,
    workspace: Any,
    source_dir: Path,
    conversation_id: str,
    counts: dict[str, Any],
    *,
    timed_out: bool,
    max_seconds: int,
    emit: Callable[[str, str, dict[str, Any] | None], None],
) -> dict[str, Any]:
    """Verify a completed artifact only when the Agent exhausted iterations."""
    if timed_out:
        return _result("paused", source_dir, "Agent time limit reached", conversation_id, details={"limit_seconds": max_seconds})
    error_reason = _safe_error(exc)
    if counts["stop_reason"] or not _is_iteration_limit_error(exc):
        return _result("failed", source_dir, error_reason, conversation_id)
    if (source_dir / "decision-request.json").is_file():
        return _result("failed", source_dir, error_reason, conversation_id, details={"reason": "product decision is pending"})
    try:
        build_result, interaction_result, details = _verify_site(workspace, source_dir)
    except Exception as verify_exc:
        return _result("failed", source_dir, error_reason, conversation_id, details={"verification_error": _safe_error(verify_exc)})
    details["agent_stop_reason"] = "iteration_limit"
    details["observed_cost_usd"] = counts["cost_usd"]
    details["cost_limit_is_soft"] = True
    status = "ready" if build_result == "passed" and interaction_result == "passed" else "failed"
    emit("verification", "构建与交互检查完成", {"build_status": build_result, "interaction_status": interaction_result})
    return _result(
        status, source_dir,
        "Verified static prototype after Agent iteration limit" if status == "ready" else error_reason,
        conversation_id,
        build_status_value=build_result,
        interaction_status=interaction_result,
        details=details,
    )


def _verify_saved_build_sync(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Recheck saved source in Docker without constructing an Agent or LLM."""
    source_dir = _source_dir(snapshot)
    if source_dir is None:
        return _result("failed", None, "source_dir is required")
    if not source_dir.is_dir():
        return _result("failed", source_dir, "saved source directory is missing")
    conversation_id = snapshot.get("agent_conversation_id")
    conversation_id = conversation_id if isinstance(conversation_id, str) and conversation_id else None
    prereqs = build_status(require_api_key=False)
    if not prereqs["available"]:
        return _result("failed", source_dir, "Unavailable: " + ", ".join(prereqs["missing"]), conversation_id)
    try:
        workspace = _open_workspace(source_dir, project_source_dir=Path(snapshot.get("project_source_dir") or source_dir))
    except Exception as exc:
        return _result("failed", source_dir, f"Docker workspace unavailable: {_safe_error(exc)}", conversation_id)
    try:
        if (source_dir / "decision-request.json").is_file():
            return _result("failed", source_dir, "Product decision is pending", conversation_id)
        build_result, interaction_result, details = _verify_site(workspace, source_dir)
        details["verification_only"] = True
        status = "ready" if build_result == "passed" and interaction_result == "passed" else "failed"
        reason = "Verified saved static prototype" if status == "ready" else details.get("reason", "Verification incomplete")
        return _result(status, source_dir, reason, conversation_id, build_status_value=build_result, interaction_status=interaction_result, details=details)
    except Exception as exc:
        return _result("failed", source_dir, f"Verification failed: {_safe_error(exc)}", conversation_id)
    finally:
        workspace.cleanup()


def _run_build_sync(snapshot: dict[str, Any], loop: asyncio.AbstractEventLoop, on_event: EventCallback, on_question: QuestionCallback | None) -> dict[str, Any]:
    source_dir = _source_dir(snapshot)
    if source_dir is None:
        return _result("failed", None, "source_dir is required")
    if not snapshot.get("approved_plan") and not snapshot.get("plan_approved"):
        return _result("failed", source_dir, "plan has not been approved")
    prereqs = build_status()
    if not prereqs["available"]:
        return _result("failed", source_dir, "Unavailable: " + ", ".join(prereqs["missing"]))

    from pydantic import SecretStr
    from openhands.sdk import Conversation, LLM, RemoteConversation
    from openhands.tools.preset.default import get_default_agent

    source_dir.mkdir(parents=True, exist_ok=True)
    max_iterations = max(1, min(int(os.getenv("AGENT_MAX_ITERATIONS", "35")), 200))
    max_seconds = max(30, min(int(os.getenv("AGENT_MAX_SECONDS", "900")), 3600))
    max_actions = max(1, min(int(os.getenv("AGENT_MAX_ACTIONS", "80")), 1000))
    max_cost = max(0.01, min(float(os.getenv("AGENT_MAX_COST_USD", "3")), 100.0))
    task_id = str(snapshot.get("task_id") or snapshot.get("project_id") or "single")
    counts: dict[str, Any] = {"events": 0, "actions": 0, "cost_usd": 0.0, "stop_reason": None}
    active_ref: dict[str, Any] = {}

    def emit(kind: str, summary: str, detail: dict[str, Any] | None = None, *, strict: bool = False) -> None:
        future = asyncio.run_coroutine_threadsafe(on_event(kind, summary, detail), loop)
        try:
            future.result(timeout=10)
        except Exception:
            if strict:
                raise
            logger.warning("build event callback failed", exc_info=True)

    def sdk_callback(event: Any) -> None:
        if counts["stop_reason"]:
            return
        counts["events"] += 1
        name = type(event).__name__
        if name == "ActionEvent":
            counts["actions"] += 1
            conversation = active_ref.get("conversation")
            if conversation is not None:
                try:
                    conversation.state.refresh_from_server()
                    metrics = conversation.conversation_stats.get_combined_metrics()
                    counts["cost_usd"] = float(metrics.accumulated_cost)
                except Exception:
                    logger.warning("agent cost metric unavailable", exc_info=True)
            if counts["actions"] >= max_actions or counts["cost_usd"] >= max_cost:
                counts["stop_reason"] = "action_limit" if counts["actions"] >= max_actions else "soft_cost_limit"
                try:
                    if conversation is not None:
                        conversation.pause()
                except Exception:
                    logger.warning("failed to pause after budget limit", exc_info=True)
        summary = _agent_event_summary(event)
        if summary is not None:
            try:
                emit("agent_event", summary, {"event_type": name, "event_count": counts["events"], "action_count": counts["actions"]}, strict=True)
            except Exception as exc:
                counts["stop_reason"] = "spec_changed" if type(exc).__name__ == "BuildPaused" else "event_callback_failed"
                conversation = active_ref.get("conversation")
                if conversation is not None:
                    try:
                        conversation.pause()
                    except Exception:
                        logger.warning("could not pause Agent after event callback failure", exc_info=True)

    try:
        workspace = _open_workspace(source_dir, project_source_dir=Path(snapshot.get("project_source_dir") or source_dir))
    except Exception as exc:
        return _result("failed", source_dir, f"Docker workspace unavailable: {_safe_error(exc)}")
    conversation = None
    try:
        llm = LLM(
            usage_id="agent",
            model=os.getenv("DEEPSEEK_AGENT_MODEL", "deepseek/deepseek-flash"),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            api_key=SecretStr(os.environ["DEEPSEEK_API_KEY"]),
        )
        agent = get_default_agent(llm=llm, cli_mode=True)
        raw_id = snapshot.get("agent_conversation_id")
        conversation_id = uuid.UUID(raw_id) if isinstance(raw_id, str) and raw_id else None
        if conversation_id is not None:
            # Constructor silently creates a new conversation on 404. Strict
            # attach prevents calling lost state a successful recovery.
            try:
                conversation = RemoteConversation.attach(
                    workspace=workspace, conversation_id=conversation_id,
                    callbacks=[sdk_callback], visualizer=None,
                )
            except Exception as exc:
                return _result("failed", source_dir, f"Saved Agent conversation cannot be resumed: {_safe_error(exc)}", str(conversation_id))
        else:
            try:
                conversation = Conversation(
                    agent=agent,
                    workspace=workspace,
                    callbacks=[sdk_callback],
                    max_iteration_per_run=max_iterations,
                    visualizer=None,
                    delete_on_close=False,
                )
            except Exception as exc:
                return _result("failed", source_dir, _conversation_start_error(exc, workspace))
        active_ref["conversation"] = conversation
        with _active_lock:
            _active[task_id] = conversation
        conversation_id_text = str(conversation.id)
        emit("agent_session", "已连接原 Agent 会话" if raw_id else "已创建 Agent 会话", {"conversation_id": conversation_id_text}, strict=True)
        approved_plan = snapshot.get("approved_plan") or snapshot.get("plan") or {}
        plan_text = json.dumps(approved_plan, ensure_ascii=False) if not isinstance(approved_plan, str) else approved_plan
        decision_text = json.dumps(snapshot.get("decision"), ensure_ascii=False) if snapshot.get("decision") else "无"
        change_text = json.dumps(snapshot.get("change_request"), ensure_ascii=False) if snapshot.get("change_request") else "无"
        reference_text = json.dumps(snapshot.get("reference_images") or [], ensure_ascii=False)
        repair_text = json.dumps(snapshot.get("repair_context"), ensure_ascii=False) if snapshot.get("repair_context") else "无"
        prompt = (
            "你正在隔离容器内实现一个已批准的单用户 Web 原型。仅修改 /workspace；不要公开部署、推送仓库、购买服务或读取容器外文件。"
            "请在 /workspace 根目录提供 package.json，包含 build 和 test:interaction 脚本；构建产物必须在 /workspace/dist/index.html。"
            "如果使用 Vite，配置 base:'./'；构建后的 script/link 本地资源必须使用相对路径，保证嵌套预览 URL 可加载。"
            "test:interaction 必须实际检查一个核心用户交互，失败时返回非零状态。"
            "可按需查看 /workspace/references/ 中的参考截图；对应路径见下。"
            "如遇必须由用户决定的产品方向，不要猜测；只写 /workspace/decision-request.json，"
            "格式为 {trigger,question,impact,options:[{id,label,impact},...],recommended_option_id,blocking_scope}，"
            "其中 recommended_option_id 必须匹配选项 id；然后结束本轮。"
            "不要把 API 密钥写到源码、测试、构建产物或日志里。\n"
            f"需求版本：{snapshot.get('spec_version', 1)}\n已批准方案：{plan_text}"
            f"\n用户决定：{decision_text}\n新变更：{change_text}\n参考截图路径：{reference_text}"
            f"\n上轮失败与验收摘要：{repair_text}"
            "\n若有上轮失败记录，先检查相关现有文件和失败输出，定位并修复实际原因；不要仅重复生成整套项目。"
            "完成后重新运行 build 和 test:interaction，确认修复有效。"
        )
        conversation.send_message(prompt)
        started_at = time.monotonic()
        try:
            conversation.run(timeout=float(max_seconds))
        except Exception as exc:
            try:
                conversation.pause()
            except Exception:
                logger.warning("pause after run failure failed", exc_info=True)
            return _run_failure_result(
                exc, workspace, source_dir, conversation_id_text, counts,
                timed_out=time.monotonic() - started_at >= max_seconds,
                max_seconds=max_seconds, emit=emit,
            )

        if counts["stop_reason"]:
            return _result(
                "paused", source_dir, f"Agent stopped at {counts['stop_reason']}", conversation_id_text,
                details={"action_count": counts["actions"], "observed_cost_usd": counts["cost_usd"], "cost_limit_is_soft": True},
            )
        conversation.state.refresh_from_server()
        execution_status = str(conversation.state.execution_status.value)
        if execution_status != "finished":
            if execution_status == "running":
                conversation.pause()
            return _result("paused" if execution_status in {"paused", "waiting_for_confirmation", "running"} else "failed", source_dir, f"Agent ended with {execution_status}", conversation_id_text)

        question = _read_question(source_dir)
        if (source_dir / "decision-request.json").is_file() and question is None:
            return _result("failed", source_dir, "decision-request.json is invalid", conversation_id_text)
        if question is not None:
            if on_question is None:
                return _result("failed", source_dir, "product decision callback is unavailable", conversation_id_text)
            future = asyncio.run_coroutine_threadsafe(on_question(question), loop)
            question_id = future.result(timeout=15)
            (source_dir / "decision-request.json").unlink(missing_ok=True)
            emit("decision_requested", "有一项产品选择待答复", {"question_id": question_id})
            return _result("waiting_decision", source_dir, "Awaiting product decision", conversation_id_text, details={"question_id": question_id})

        build_result, interaction_result, details = _verify_site(workspace, source_dir)
        details["observed_cost_usd"] = counts["cost_usd"]
        details["cost_limit_is_soft"] = True
        status = "ready" if build_result == "passed" and interaction_result == "passed" else "failed"
        reason = "Verified static prototype" if status == "ready" else details.get("reason", "Verification incomplete")
        emit("verification", "构建与交互检查完成", {"build_status": build_result, "interaction_status": interaction_result})
        return _result(status, source_dir, reason, conversation_id_text, build_status_value=build_result, interaction_status=interaction_result, details=details)
    except Exception as exc:
        return _result("failed", source_dir, _safe_error(exc), str(conversation.id) if conversation else None)
    finally:
        with _active_lock:
            if _active.get(task_id) is conversation:
                _active.pop(task_id, None)
        if conversation is not None:
            try:
                conversation.close()
            except Exception:
                logger.warning("OpenHands conversation close failed", exc_info=True)
        workspace.cleanup()


def _staged_snapshot(snapshot: dict[str, Any]) -> tuple[Path, Any, dict[str, Any]]:
    """Copy a selected project onto the native disk before Docker sees it."""
    source_dir = _source_dir(snapshot)
    if source_dir is None:
        raise ValueError("source_dir is required")
    staged = stage_project(source_dir, _agent_state_dir(source_dir))
    staged_snapshot = dict(snapshot)
    staged_snapshot.update({
        "source_dir": str(staged.workspace_dir),
        "workspace_dir": str(staged.workspace_dir),
        "workspace_root": str(staged.workspace_dir),
        "project_source_dir": str(source_dir),
    })
    return source_dir, staged, staged_snapshot


def _sync_staged_result(source_dir: Path, staged: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Return project paths only after the staged files reach the chosen folder."""
    try:
        staged.sync_back()
    except Exception as exc:
        verification = result.get("verification") or {}
        raw_details = verification.get("details") if isinstance(verification, dict) else None
        details = dict(raw_details) if isinstance(raw_details, dict) else {}
        details["recovery_workspace"] = str(staged.stage_dir)
        return _result(
            "failed", source_dir,
            f"Project sync failed; staged work retained at {staged.stage_dir}: {_safe_error(exc)}",
            result.get("conversation_id"),
            build_status_value=str(verification.get("build_status") or "not_run") if isinstance(verification, dict) else "not_run",
            interaction_status=str(verification.get("interaction_status") or "not_run") if isinstance(verification, dict) else "not_run",
            details=details,
        )
    mapped = dict(result)
    mapped["source_dir"] = str(source_dir)
    mapped["preview_dir"] = str(source_dir / "dist") if result.get("status") == "ready" and (source_dir / "dist" / "index.html").is_file() else None
    if result.get("status") == "ready" and mapped["preview_dir"] is None:
        mapped["status"] = "failed"
        mapped["reason"] = "Verified preview was not present in the selected project after sync"
    try:
        shutil.rmtree(staged.stage_dir)
    except OSError:
        logger.warning("could not remove a synced Agent working copy", exc_info=True)
    return mapped


def _staged_run_build_sync(snapshot: dict[str, Any], loop: asyncio.AbstractEventLoop, on_event: EventCallback, on_question: QuestionCallback | None) -> dict[str, Any]:
    source_dir = _source_dir(snapshot)
    try:
        source_dir, staged, staged_snapshot = _staged_snapshot(snapshot)
    except Exception as exc:
        return _result("failed", source_dir, f"Project staging unavailable: {_safe_error(exc)}")
    result = _run_build_sync(staged_snapshot, loop, on_event, on_question)
    return _sync_staged_result(source_dir, staged, result)


def _staged_verify_saved_build_sync(snapshot: dict[str, Any]) -> dict[str, Any]:
    source_dir = _source_dir(snapshot)
    try:
        source_dir, staged, staged_snapshot = _staged_snapshot(snapshot)
    except Exception as exc:
        return _result("failed", source_dir, f"Project staging unavailable: {_safe_error(exc)}")
    result = _verify_saved_build_sync(staged_snapshot)
    return _sync_staged_result(source_dir, staged, result)


async def run_build(snapshot: dict[str, Any], on_event: EventCallback, on_question: QuestionCallback | None = None) -> dict[str, Any]:
    """Run the approved plan and verify the artifact in Docker.

    For this SDK version, the product decision contract is a file emitted by the
    agent at a safe end-of-run boundary. It is not an OpenHands SDK question API.
    """
    return await asyncio.to_thread(_staged_run_build_sync, snapshot, asyncio.get_running_loop(), on_event, on_question)


async def verify_saved_build(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Reverify persisted source in an isolated Docker workspace without an LLM."""
    return await asyncio.to_thread(_staged_verify_saved_build_sync, snapshot)


async def ask_progress(snapshot: dict[str, Any], question: str) -> str:
    """Use ask_agent only for an active conversation; anchor reply in core events."""
    task_id = str(snapshot.get("task_id") or snapshot.get("project_id") or "single")
    phase = str(snapshot.get("phase") or "unknown")
    recent = snapshot.get("recent_event") or snapshot.get("last_event")
    anchor = f"当前阶段：{phase}。最近记录：{recent if recent else '暂无可核对事件'}。"
    with _active_lock:
        conversation = _active.get(task_id)
    if conversation is None:
        return anchor + " 当前没有运行中的 Agent 会话可供进一步查询。"
    try:
        answer = await asyncio.to_thread(conversation.ask_agent, question[:500])
    except Exception:
        return anchor + " 当前 Agent 查询暂不可用。"
    return anchor + str(answer)[:1000]
