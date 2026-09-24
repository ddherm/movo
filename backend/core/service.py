"""One project coordinator used by both text and voice entry points."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .folders import ProjectFolders
from .planner import Planner, PlannerUnavailable
from .progress import build_progress
from .store import StateError, Store


PROGRESS_RE = re.compile(r"(进度|进展|做到哪|到哪一步|怎么样了|卡住|卡点|状态|status|完成了吗|目前做了|现在做了)", re.I)
CHANGE_RE = re.compile(r"(删掉|去掉|不要|改成|修改|增加|加入|先不做|取消|改需求)")
APPROVE_RE = re.compile(r"^(确认|同意|批准|按这个方案|开始做|就这样做)[。！!\s]*$")
RESUME_RE = re.compile(r"^(继续|继续任务|重试|重试任务|恢复|恢复任务)[。！!\s]*$")
DIAGNOSTIC_RE = re.compile(
    r"(为什么|为何|原因|怎么回事|怎么修|如何修|怎么解决|出了?什么问题|哪里出错|报错|故障|异常|失败|卡住|诊断|查\s*bug|debug|error|bug)",
    re.I,
)
DIAGNOSTIC_QUESTION_RE = re.compile(r"(为什么|为何|原因|怎么回事|怎么修|如何修|怎么解决|出了?什么问题|哪里出错|诊断|查\s*bug|debug)", re.I)
SECRET_RE = re.compile(r"(api[ -]?key|密钥|秘钥|access[ -]?token|bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|\b[A-Za-z0-9_-]{32,}\b)", re.I)


def contains_secret(text: str) -> bool:
    if SECRET_RE.search(text):
        return True
    return any(
        value and len(value) >= 6 and value in text
        for value in (os.getenv(name, "").strip() for name in (
            "DEEPSEEK_API_KEY", "MINIMAX_API_KEY", "MINIMAX_GROUP_ID", "TAVILY_API_KEY", "NTFY_TOKEN"
        ))
    )


class BuildPaused(RuntimeError):
    pass


class Coordinator:
    def __init__(self, data_dir: Path, assistant_root: Path | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "state.sqlite3")
        self.folders = ProjectFolders(assistant_root or self.data_dir / "assistant", os.getenv("PROJECT_STORAGE_ROOTS", ""))
        self._build_task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._build_requested = False
        self._build_lock = asyncio.Lock()
        self._plan_lock = asyncio.Lock()
        self._diagnostic_task: asyncio.Task[None] | None = None

        try:
            from backend.integrations import search
        except ImportError:
            search = None
        self.planner = Planner(self.data_dir, search=search, asset_lookup=self.store.asset_path)

    def snapshot(self) -> dict[str, Any] | None:
        snapshot = self.store.snapshot()
        if snapshot:
            snapshot["source_dir"] = snapshot.get("source_dir") or str(self.data_dir / "projects" / snapshot["id"])
            snapshot["progress"] = build_progress(snapshot)
        return snapshot

    async def start(self) -> None:
        snapshot = self.snapshot()
        if snapshot and snapshot["phase"] in {"BUILD", "VERIFY"}:
            self.store.event("service_restart", "服务已重启，正在尝试恢复原任务。")
            self.schedule_build()
        if self._notification_task is None or self._notification_task.done():
            self._notification_task = asyncio.create_task(self._deliver_notifications())

    async def stop(self) -> None:
        if self._notification_task:
            self._notification_task.cancel()
            try:
                await self._notification_task
            except asyncio.CancelledError:
                pass

    async def _deliver_notifications(self) -> None:
        while True:
            for item in self.store.pending_notification_deliveries():
                await self._notify(item["kind"], item["dedupe_key"])
            await asyncio.sleep(60)

    def _ensure_switchable(self) -> None:
        if self._plan_lock.locked() or self._build_lock.locked() or (self._build_task and not self._build_task.done()):
            raise StateError("当前项目正有任务运行，请等待本轮完成或暂停后再切换项目。")

    def list_projects(self) -> list[dict[str, Any]]:
        projects = self.store.list_projects()
        for project in projects:
            source = project.get("source_dir")
            project["folder_name"] = Path(source).name if source else None
            project["source_dir"] = source or str(self.data_dir / "projects" / project["id"])
        return projects

    async def select_project(self, project_id: str) -> dict[str, Any]:
        self._ensure_switchable()
        self.store.select_project(project_id)
        return self.snapshot()  # type: ignore[return-value]

    async def create_project(self, idea: str, parent_dir: str | None = None, folder_name: str | None = None) -> dict[str, Any]:
        self._ensure_switchable()
        target = self.folders.create(parent_dir, folder_name)
        try:
            self.store.create_project(idea, source_dir=str(target))
        except Exception:
            try:
                target.rmdir()  # Only remove the empty folder created by this attempt.
            except OSError:
                pass
            raise
        return self.snapshot()  # type: ignore[return-value]

    async def message(self, text: str, channel: str = "text") -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise StateError("消息不能为空。")
        if channel not in {"text", "voice"}:
            raise StateError("消息渠道无效。")
        snapshot = self.snapshot()
        if not snapshot:
            raise StateError("请先创建项目。")
        if contains_secret(text):
            # Do not persist accidental credentials in chat or voice transcripts.
            self.store.add_message("assistant", "密钥请只在 Mac 服务端的 .env 文件填写，不要在聊天或通话中提供。", "system")
            return self.snapshot()  # type: ignore[return-value]
        self.store.add_message("user", text, channel)
        phase = snapshot["phase"]

        if phase == "WAITING_DECISION" and snapshot["pending_question"]:
            question = snapshot["pending_question"]
            matches = [
                option for option in question["options"]
                if text == option["id"] or text == option["label"] or option["label"] in text
            ]
            if len(matches) == 1:
                result = self.store.answer_question(question["id"], matches[0]["id"], text, channel)
                self.store.add_message("assistant", result["message"], "system")
                if result["accepted"]:
                    self.schedule_build()
            elif PROGRESS_RE.search(text) and not CHANGE_RE.search(text):
                self.store.add_message("assistant", self.progress_text(), "system")
            else:
                self.store.add_message("assistant", "这个问题需要明确选择，请说出或点选卡片中的一个选项。", "system")
            return self.snapshot()  # type: ignore[return-value]

        if phase == "PLAN_REVIEW" and APPROVE_RE.search(text):
            await self.approve_plan()
            return self.snapshot()  # type: ignore[return-value]

        if phase in {"FAILED", "PAUSED"} and RESUME_RE.search(text):
            await self.resume()
            return self.snapshot()  # type: ignore[return-value]

        # A question about a failure is different from a progress request, even
        # when it contains words such as "卡住". Keep diagnostics ahead of both
        # the progress shortcut and the change-request branch.
        diagnostic_intent = bool(DIAGNOSTIC_RE.search(text)) and (
            not CHANGE_RE.search(text) or bool(DIAGNOSTIC_QUESTION_RE.search(text))
        )
        if diagnostic_intent and phase in {"PLAN_REVIEW", "BUILD", "VERIFY", "PAUSED", "READY", "FAILED"}:
            await self._answer_diagnostic(text, snapshot)
            return self.snapshot()  # type: ignore[return-value]

        if PROGRESS_RE.search(text) and not CHANGE_RE.search(text):
            self.store.add_message("assistant", self.progress_text(), "system")
            return self.snapshot()  # type: ignore[return-value]

        if phase in {"BUILD", "VERIFY", "PAUSED", "READY", "FAILED"}:
            if CHANGE_RE.search(text):
                await self.change(text, "在会话中提出", trigger_plan=True)
            else:
                await self._answer_diagnostic(text, snapshot)
            return self.snapshot()  # type: ignore[return-value]

        if phase == "PLAN_REVIEW":
            if CHANGE_RE.search(text):
                self.store.change_spec(text, "用户修订待确认方案")
                self.store.add_message("assistant", "已记录这次修改，我会按新需求更新方案。", "system")
                await self._plan_turn()
            else:
                await self._answer_diagnostic(text, snapshot)
            return self.snapshot()  # type: ignore[return-value]

        await self._plan_turn()
        return self.snapshot()  # type: ignore[return-value]

    async def _answer_diagnostic(self, text: str, snapshot: dict[str, Any]) -> None:
        reply = await self.diagnose_issue(text, snapshot)
        current = self.snapshot()
        if not current or current["id"] != snapshot["id"] or current["spec_version"] != snapshot["spec_version"]:
            raise StateError("排查期间项目或需求已切换；旧结果已丢弃。")
        self.store.add_message("assistant", reply, "system")

    async def diagnose_issue(self, text: str, snapshot: dict[str, Any]) -> str:
        """Answer from current evidence and execute only a guarded recovery."""
        from .diagnostics import diagnose

        result = await diagnose(snapshot, text)
        reply = str(result["reply"])
        action = result["action"]
        current = self.snapshot()
        if not current or current["id"] != snapshot["id"] or current["spec_version"] != snapshot["spec_version"]:
            return reply + " 项目状态在排查期间已变化，请以最新页面为准。"
        if action == "verify_saved":
            if current["phase"] != "FAILED" or "MaxIterationsReached" not in (current["last_error"] or ""):
                return reply
            if self._diagnostic_task and not self._diagnostic_task.done():
                return reply + " 保存结果的验收已经在进行。"
            self._diagnostic_task = asyncio.create_task(self._verify_diagnostic_recovery(current["id"], current["spec_version"]))
            return reply + " 已开始重新验收保存的页面。"
        if action in {"repair_build", "retry_build"}:
            if current["phase"] != "FAILED" or not current.get("plan") or current["plan"].get("status") != "approved":
                return reply
            if action == "repair_build":
                source = Path(current["source_dir"])
                if not (source / "package.json").is_file() or "/api/conversations" in (current["last_error"] or ""):
                    return reply + " 当前没有可供代码修复的构建产物，我会先排查会话服务。"
            else:
                # A repeated failed run needs new evidence, not another blind
                # container start and model call.
                failures = 0
                for event in current.get("events", []):
                    if event.get("kind") in {"plan_approved", "spec_changed"}:
                        break
                    failures += event.get("kind") == "build_failed"
                if failures > 1 or "/api/conversations" in (current["last_error"] or ""):
                    return reply + " 同一版本已重复失败，先查看服务端错误再重试。"
            await self.resume()
            return reply + (" 已让构建 Agent 检查现有代码并修复。" if action == "repair_build" else " 已开始一次恢复尝试。")
        return reply

    async def _verify_diagnostic_recovery(self, project_id: str, version: int) -> None:
        try:
            current = self.snapshot()
            if not current or current["id"] != project_id or current["spec_version"] != version:
                return
            await self.reverify_saved_build()
        except Exception as exc:
            current = self.snapshot()
            if current and current["id"] == project_id and current["spec_version"] == version:
                self.store.add_message("assistant", f"保存的页面仍未通过验收：{type(exc).__name__}。请查看验收卡中的构建与交互结果。", "system")

    async def plan_current_spec(self) -> None:
        await self._plan_turn()

    async def _plan_turn(self) -> None:
        async with self._plan_lock:
            snapshot = self.snapshot()
            if not snapshot or snapshot["phase"] not in {"CLARIFY", "RESEARCH", "PAUSED"}:
                return
            try:
                result = await self.planner.next_turn(snapshot)
            except PlannerUnavailable as exc:
                current = self.snapshot()
                if current and current["id"] == snapshot["id"] and current["spec_version"] == snapshot["spec_version"]:
                    self.store.add_message("assistant", str(exc), "system")
                    self.store.event("planner_unavailable", "需求讨论暂时不可用，用户消息已保存。")
                return
            try:
                plan_id = self.store.commit_planner_turn(snapshot["spec_version"], result["reply"], result["plan"], expected_project_id=snapshot["id"])
            except StateError:
                self.store.event("stale_planner_result", "需求在讨论期间更新，旧方案结果已丢弃。")
                return
            if plan_id:
                await self._notify("plan_review", f"plan:{plan_id}")

    async def approve_plan(self) -> dict[str, Any]:
        self.store.approve_plan()
        self.store.add_message("assistant", "方案已确认。我会在隔离工作区开始制作，并在关键问题出现时回来问你。", "system")
        self.schedule_build()
        return self.snapshot()  # type: ignore[return-value]

    async def answer(self, question_id: str, option_id: str, reason: str, channel: str) -> dict[str, Any]:
        result = self.store.answer_question(question_id, option_id, reason, channel)
        if result["accepted"]:
            self.store.add_message("assistant", result["message"], "system")
            self.schedule_build()
        return result

    async def change(self, text: str, reason: str, trigger_plan: bool = False) -> dict[str, Any]:
        version = self.store.change_spec(text, reason)
        self.store.add_message("assistant", f"已记录第 {version} 版需求。我会在安全边界调整方案，原方案仍可追溯。", "system")
        if trigger_plan:
            await self._plan_turn()
        return self.snapshot()  # type: ignore[return-value]

    def progress_text(self) -> str:
        snapshot = self.snapshot()
        if not snapshot:
            return "项目尚未创建。"
        latest = snapshot["events"][:3]
        facts = "；".join(event["summary"] for event in latest) if latest else "还没有执行事件"
        phase = snapshot["phase"]
        if phase == "WAITING_DECISION" and snapshot["pending_question"]:
            return f"目前在等待你决定：{snapshot['pending_question']['question']} 最近记录：{facts}"
        if phase == "FAILED":
            return f"任务遇到问题：{snapshot['last_error'] or '原因尚未记录'}。最近记录：{facts}"
        if phase == "READY":
            return f"原型已完成，预览入口在项目页。构建和交互检查结果也在验收卡中。最近记录：{facts}"
        if phase == "PAUSED":
            return f"任务已暂停，正在按新需求调整。最近记录：{facts}"
        if phase == "PLAN_REVIEW":
            return f"方案已准备好，正在等你确认。最近真实记录：{facts}。没有可靠的完成百分比。"
        if phase in {"CLARIFY", "RESEARCH"}:
            return f"正在澄清想法和准备方案。最近真实记录：{facts}。没有可靠的完成百分比。"
        return f"当前阶段：{phase}。最近真实记录：{facts}。没有可靠的完成百分比。"

    def schedule_build(self) -> bool:
        if self._build_task and not self._build_task.done():
            self._build_requested = True
            return False
        self._build_requested = False
        self._build_task = asyncio.create_task(self._run_build())
        self._build_task.add_done_callback(self._after_build)
        return True

    def _after_build(self, _task: asyncio.Task[None]) -> None:
        if self._build_requested and (self.snapshot() or {}).get("phase") == "BUILD":
            self._build_requested = False
            asyncio.get_running_loop().call_soon(self.schedule_build)

    async def _run_build(self) -> None:
        async with self._build_lock:
            snapshot = self.snapshot()
            if not snapshot or snapshot["phase"] != "BUILD":
                return
            workspace_root = Path(snapshot["source_dir"])
            workspace_root.mkdir(parents=True, exist_ok=True)
            reference_dir = workspace_root / "references"
            reference_dir.mkdir(exist_ok=True)
            reference_images: list[str] = []
            for item in snapshot["screenshots"]:
                relative = self.store.asset_path(item["id"])
                if not relative:
                    continue
                source = (self.data_dir / relative).resolve()
                if not source.is_relative_to(self.data_dir) or not source.is_file():
                    continue
                target = reference_dir / f"{item['id']}{source.suffix.lower()}"
                shutil.copy2(source, target)
                reference_images.append(f"/workspace/references/{target.name}")
            snapshot["workspace_root"] = str(workspace_root)
            snapshot["workspace_dir"] = str(workspace_root)
            snapshot["source_dir"] = str(workspace_root)
            snapshot["approved_plan"] = snapshot["plan"] if snapshot["plan"] and snapshot["plan"]["status"] == "approved" else None
            snapshot["decision"] = snapshot["decisions"][-1] if snapshot["decisions"] else None
            snapshot["change_request"] = snapshot["spec_versions"][-1] if snapshot["spec_version"] > 1 else None
            snapshot["reference_images"] = reference_images
            if snapshot.get("last_error"):
                from .diagnostics import _safe_text

                verification = snapshot.get("verification") or {}
                # A failed interaction can come from a source file that was
                # truncated while the external drive was disconnected. Give
                # the Agent this bounded file metadata so it inspects the
                # right place before attempting another full generation.
                empty_sources = []
                source_tree = workspace_root / "src"
                if source_tree.is_dir():
                    for candidate in source_tree.rglob("*"):
                        if len(empty_sources) >= 20:
                            break
                        if candidate.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".css"}:
                            continue
                        try:
                            if candidate.is_file() and candidate.stat().st_size == 0:
                                empty_sources.append(str(candidate.relative_to(workspace_root)))
                        except OSError:
                            continue
                snapshot["repair_context"] = {
                    "last_error": _safe_text(snapshot["last_error"], 500),
                    "verification": _safe_text(verification.get("details"), 1000) if isinstance(verification, dict) else "",
                    "empty_source_files": empty_sources,
                }
            run_version = snapshot["spec_version"]
            try:
                from backend.integrations import run_build
            except ImportError:
                if self.store.fail_build(run_version, "OpenHands 集成尚未安装，无法开始隔离构建。", "OpenHands 集成不可用。"):
                    await self._notify("failed", f"phase:FAILED:{run_version}")
                return

            async def on_event(kind: str, summary: str, detail: dict[str, Any] | None = None) -> None:
                current = self.snapshot()
                if not current or current["id"] != snapshot["id"] or current["spec_version"] != run_version or current["phase"] == "PAUSED":
                    raise BuildPaused("需求已变化，等待安全边界调整。")
                if kind == "agent_session" and detail and detail.get("conversation_id"):
                    self.store.set_conversation_id(str(detail["conversation_id"]))
                self.store.event(kind, summary, detail)

            async def on_question(body: dict[str, Any]) -> str:
                current = self.snapshot()
                if not current or current["id"] != snapshot["id"] or current["spec_version"] != run_version:
                    raise BuildPaused("需求已变化，等待安全边界调整。")
                question_id = self.store.open_question(body, expected_version=run_version)
                await self._notify("decision", f"question:{question_id}")
                return question_id

            try:
                result = await run_build(snapshot, on_event, on_question)
            except BuildPaused:
                self.store.event("build_paused", "构建已在安全边界暂停，等待新方案。")
                return
            except Exception as exc:
                # Never expose credentials or raw provider payloads in the UI.
                if self.store.fail_build(run_version, f"隔离构建异常：{type(exc).__name__}", "隔离构建失败，请查看验收卡。"):
                    await self._notify("failed", f"phase:FAILED:{run_version}")
                return

            current = self.snapshot()
            if not current or current["id"] != snapshot["id"] or current["spec_version"] != run_version or current["phase"] == "PAUSED":
                self.store.event("stale_build_result", "旧需求版本的构建结果已保留，但不会标记为完成。")
                return
            if result.get("conversation_id"):
                self.store.set_conversation_id(str(result["conversation_id"]))
            status = result.get("status")
            if status == "waiting_decision":
                if current["phase"] == "BUILD" and self._build_requested and current["decisions"]:
                    # The user answered while the previous SDK turn was still
                    # unwinding. Its done callback starts the same session again.
                    return
                if current["phase"] != "WAITING_DECISION":
                    if self.store.fail_build(run_version, "Agent 等待决定，但没有生成有效问题卡。", "缺少待回答的决策卡。"):
                        await self._notify("failed", f"phase:FAILED:{run_version}")
                return
            if status == "paused":
                self.store.set_phase("PAUSED", "Agent 已暂停；可以继续同一项目。")
                return
            if status != "ready":
                raw_verification = result.get("verification") or {}
                failed_verification = None
                if isinstance(raw_verification, dict) and (
                    raw_verification.get("build_status") not in {None, "not_run"}
                    or raw_verification.get("interaction_status") not in {None, "not_run"}
                ):
                    failed_verification = {
                        "build_status": str(raw_verification.get("build_status") or "not_run"),
                        "interaction_status": str(raw_verification.get("interaction_status") or "not_run"),
                        "details": json.dumps(raw_verification.get("details") or {}, ensure_ascii=False)[:1500],
                    }
                if self.store.fail_build(run_version, str(result.get("reason") or "构建未完成。")[:300], "构建未完成，请查看失败原因。", failed_verification):
                    await self._notify("failed", f"phase:FAILED:{run_version}")
                return

            verification = result.get("verification") or {}
            preview_dir = Path(str(result.get("preview_dir") or ""))
            try:
                valid_preview = preview_dir.resolve().is_relative_to(workspace_root.resolve()) and (preview_dir / "index.html").is_file()
            except (OSError, ValueError):
                valid_preview = False
            if not valid_preview or verification.get("build_status") != "passed" or verification.get("interaction_status") != "passed":
                invalid = {
                    "build_status": str(verification.get("build_status") or "missing"),
                    "interaction_status": str(verification.get("interaction_status") or "missing"),
                    "details": str(verification.get("details") or "缺少可验证的静态预览。"),
                }
                if self.store.fail_build(run_version, "构建产物或核心交互检查未通过。", "验证失败，请查看构建记录。", invalid):
                    await self._notify("failed", f"phase:FAILED:{run_version}")
                return
            if self.store.finish_build(run_version, str(verification.get("details") or "构建与核心交互检查通过。")):
                await self._notify("ready", f"phase:READY:{run_version}")

    async def resume(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        if not snapshot:
            raise StateError("项目尚未创建。")
        if snapshot["phase"] == "FAILED" and snapshot["plan"] and snapshot["plan"]["status"] == "approved":
            self.store.set_phase("BUILD", "用户请求恢复已确认的构建任务。")
            self.schedule_build()
        elif snapshot["phase"] == "PAUSED":
            if snapshot["plan"] and snapshot["plan"]["status"] == "approved":
                self.store.set_phase("BUILD", "继续已确认方案的原 Agent 任务。")
                self.schedule_build()
            else:
                self.store.set_phase("CLARIFY", "继续讨论更新后的需求。")
                await self._plan_turn()
        else:
            raise StateError("当前阶段无需恢复。")
        return self.snapshot()  # type: ignore[return-value]

    async def reverify_saved_build(self) -> dict[str, Any]:
        """Recover an iteration-limited build by checking its saved files without an LLM call."""
        async with self._build_lock:
            snapshot = self.snapshot()
            if not snapshot or snapshot["phase"] != "FAILED" or "MaxIterationsReached" not in (snapshot["last_error"] or ""):
                raise StateError("当前没有可重新验收的迭代上限构建。")
            version = snapshot["spec_version"]
            workspace_root = Path(snapshot["source_dir"])
            snapshot["source_dir"] = str(workspace_root)
            from backend.integrations import verify_saved_build

            result = await verify_saved_build(snapshot)
            current = self.snapshot()
            if not current or current["spec_version"] != version or current["phase"] != "FAILED":
                raise StateError("需求状态已变化，旧构建结果不能用于当前版本。")
            verification = result.get("verification") or {}
            preview_dir = Path(str(result.get("preview_dir") or ""))
            try:
                valid_preview = preview_dir.resolve().is_relative_to(workspace_root.resolve()) and (preview_dir / "index.html").is_file()
            except (OSError, ValueError):
                valid_preview = False
            if result.get("status") != "ready" or not valid_preview or verification.get("build_status") != "passed" or verification.get("interaction_status") != "passed":
                self.store.set_verification(
                    str(verification.get("build_status") or "not_run"),
                    str(verification.get("interaction_status") or "not_run"),
                    json.dumps(verification.get("details") or {}, ensure_ascii=False)[:1500],
                )
                raise StateError("已保存的页面未通过构建与核心交互检查：" + str(result.get("reason") or "验证未完成。")[:180])
            if not self.store.finish_build(version, str(verification.get("details") or "已保存的构建与核心交互检查通过。"), from_iteration_limit=True):
                raise StateError("需求状态已变化，旧构建结果不能用于当前版本。")
            await self._notify("ready", f"phase:READY:{version}")
            return self.snapshot()  # type: ignore[return-value]

    async def _notify(self, kind: str, key: str) -> None:
        # Notification rows are committed with the corresponding state change.
        # External delivery only contains a neutral phrase and the private URL.
        snapshot = self.snapshot()
        if not snapshot:
            return
        current = {
            "plan_review": snapshot["phase"] == "PLAN_REVIEW" and bool(snapshot["plan"]) and key == f"plan:{snapshot['plan']['id']}",
            "decision": snapshot["phase"] == "WAITING_DECISION" and bool(snapshot["pending_question"]) and key == f"question:{snapshot['pending_question']['id']}",
            "ready": snapshot["phase"] == "READY" and key == f"phase:READY:{snapshot['spec_version']}",
            "failed": snapshot["phase"] == "FAILED" and key == f"phase:FAILED:{snapshot['spec_version']}",
        }.get(kind, False)
        if not current:
            return
        try:
            from backend.integrations import send_notification
        except ImportError:
            return
        url = os.getenv("PUBLIC_PROJECT_URL", "").strip()
        if not url or not os.getenv("NTFY_TOPIC", "").strip():
            return
        if not self.store.claim_notification_delivery(key):
            return
        try:
            sent = await send_notification(kind, url)
            if sent:
                self.store.mark_notification_delivered(key)
            else:
                self.store.event("notification_delivery_failed", "手机提醒暂未送达；站内卡片仍可查看，稍后会重试。")
        except Exception:
            self.store.event("notification_delivery_failed", "手机提醒暂未送达；站内卡片仍可查看，稍后会重试。")
