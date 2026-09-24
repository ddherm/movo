"""Evidence-based diagnosis for project build failures.

This module makes no project changes. The coordinator is responsible for
authorizing and executing any proposed recovery action.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

import httpx

from .privacy import redact


_ACTIONS = {"none", "retry_build", "verify_saved", "repair_build"}
_LOCAL_PATH = re.compile(r"(?:/Volumes|/Users|/private|/workspace|/tmp|/var|/home)(?:/[^\s\"'<>]*)?", re.I)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_MAX_EVENTS = 12


def _safe_text(value: Any, limit: int = 500) -> str:
    """Keep useful error wording while omitting URLs, local paths and tokens."""
    text = redact(str(value or ""))
    text = _URL.sub("[地址已隐藏]", text)
    text = _LOCAL_PATH.sub("[本地路径]", text)
    text = _CONTROL.sub(" ", text)
    return text[:limit]


def _safe_data(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "[内容已省略]"
    if isinstance(value, str):
        return _safe_text(value, 1000)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_data(item, depth=depth + 1) for item in value[:10]]
    if isinstance(value, dict):
        return {_safe_text(key, 40): _safe_data(item, depth=depth + 1) for key, item in list(value.items())[:12]}
    return _safe_text(value, 200)


def _artifact_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Inspect bounded file metadata, never source content or SDK logs."""
    missing = {"source_directory_exists": False, "package_json_exists": False, "dist_entry_exists": False, "decision_request_exists": False, "empty_source_files": []}
    raw = snapshot.get("source_dir")
    if not isinstance(raw, str) or not raw:
        return missing
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir():
        return missing
    try:
        empty_files: list[str] = []
        source_tree = path / "src"
        if source_tree.is_dir():
            for candidate in source_tree.rglob("*"):
                if len(empty_files) >= 12:
                    break
                if candidate.is_symlink() or candidate.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".css"}:
                    continue
                if candidate.is_file() and candidate.stat().st_size == 0:
                    empty_files.append(candidate.relative_to(path).as_posix())
        return {
            "source_directory_exists": True,
            "package_json_exists": (path / "package.json").is_file(),
            "dist_entry_exists": (path / "dist" / "index.html").is_file(),
            "decision_request_exists": (path / "decision-request.json").is_file(),
            "empty_source_files": empty_files,
        }
    except OSError:
        return {**missing, "source_directory_exists": True}


def _case(snapshot: dict[str, Any], question: str, evidence: dict[str, Any] | None) -> dict[str, Any]:
    verification = snapshot.get("verification") or {}
    if not isinstance(verification, dict):
        verification = {}
    plan = snapshot.get("plan") or {}
    if not isinstance(plan, dict):
        plan = {}
    events = snapshot.get("events") or []
    if not isinstance(events, list):
        events = []
    clean_events = [
        {"kind": _safe_text(item.get("kind"), 50), "summary": _safe_text(item.get("summary"), 250)}
        for item in events[:_MAX_EVENTS]
        if isinstance(item, dict)
    ]
    raw_error = str(snapshot.get("last_error") or "")
    server_error_detail = _safe_text(raw_error.split("服务端异常：", 1)[1], 350) if "服务端异常：" in raw_error else ""
    return {
        "question": _safe_text(question, 500),
        "phase": _safe_text(snapshot.get("phase"), 40),
        "project_context": {
            "idea": _safe_text(snapshot.get("idea"), 500),
            "spec_version": snapshot.get("spec_version") if isinstance(snapshot.get("spec_version"), int) else None,
            "plan_status": _safe_text(plan.get("status"), 40),
            "plan": {
                key: _safe_text(plan.get(key), 500)
                for key in ("goal", "audience", "flow", "scope", "acceptance")
                if plan.get(key)
            },
        },
        "last_error": _safe_text(snapshot.get("last_error"), 500),
        "server_error_detail": server_error_detail,
        "failure_location": "agent_server_conversation_create" if snapshot.get("phase") == "FAILED" and _is_agent_server_500(raw_error) else "unknown",
        "verification": {
            "build_status": _safe_text(verification.get("build_status"), 40),
            "interaction_status": _safe_text(verification.get("interaction_status"), 40),
            "details": _safe_text(verification.get("details"), 1000),
        },
        "recent_events": clean_events,
        "diagnostic_artifacts": _artifact_metadata(snapshot),
        "extra_evidence": _safe_data(evidence or {}),
    }


def _is_agent_server_500(error: str) -> bool:
    return bool(re.search(r"\b500\b|internal server error", error, re.I)) and bool(
        re.search(r"/api/conversations|Agent Server[^.]*create|Agent Server[^.]*创建会话", error, re.I)
    )


def _has_server_error_detail(case: dict[str, Any], evidence: dict[str, Any] | None) -> bool:
    """A status code and ordinary events do not establish a 500 root cause."""
    generic = {"500", "internal server error", "http 500", "server error"}
    if case["server_error_detail"] and case["server_error_detail"].strip().lower() not in generic:
        return True
    if not isinstance(evidence, dict):
        return False
    return any(
        isinstance(evidence.get(key), str) and bool(evidence[key].strip()) and evidence[key].strip().lower() not in generic
        for key in ("server_log_excerpt", "agent_server_traceback", "server_error_detail")
    )


def _fallback(case: dict[str, Any]) -> dict[str, Any]:
    phase, error = case["phase"], case["last_error"]
    artifacts = case["diagnostic_artifacts"]
    verification = case["verification"]
    if case["failure_location"] == "agent_server_conversation_create":
        finding = next((item["summary"] for item in case["recent_events"] if item["kind"] == "diagnostic_finding"), "")
        if finding:
            return {
                "reply": f"排查记录：{finding.rstrip('。！？')}。会话服务的存储位置已调整；当前失败的构建需要重新启动，完成后仍会检查构建与核心交互。",
                "action": "none",
                "rationale": "受控复现已定位会话存储问题，诊断回复不自行重复启动构建。",
                "confidence": 0.9,
            }
        artifact_note = "当前没有检测到 package.json 或预览产物。" if not artifacts["package_json_exists"] and not artifacts["dist_entry_exists"] else ""
        detail_note = f"服务端留下的异常提示是：{case['server_error_detail']}。" if case["server_error_detail"] else ""
        return {
            "reply": "当前记录显示：Agent Server 创建会话时返回 HTTP 500。" + artifact_note + detail_note + "仅凭 HTTP 状态码无法判定是模型接口、容器还是项目源码导致。需要结合服务端异常和 Docker 状态继续核查；在找到确切原因前不应把它归因于你的需求。",
            "action": "none",
            "rationale": "缺少服务端异常堆栈，重试或改源码都没有足够依据。",
            "confidence": 0.35,
        }
    approved = case["project_context"]["plan_status"] == "approved"
    if phase == "FAILED" and approved and "MaxIterationsReached" in error and artifacts["dist_entry_exists"] and (
        verification["build_status"] != "failed" and verification["interaction_status"] != "failed"
    ):
        return {
            "reply": "构建 Agent 达到迭代上限，但项目目录已有预览入口。我会先对保存的页面运行构建和核心交互检查；两项通过才能标记为完成。",
            "action": "verify_saved",
            "rationale": "迭代上限不等于产物失败，且已有可检查的构建产物。",
            "confidence": 0.85,
        }
    if phase == "FAILED" and approved and (
        verification["build_status"] == "failed" or verification["interaction_status"] == "failed"
    ) and artifacts["package_json_exists"]:
        check_name = "核心交互" if verification["interaction_status"] == "failed" else "构建"
        empty_files = artifacts.get("empty_source_files") or []
        empty_note = "检测到空的源码文件：" + "、".join(empty_files[:4]) + "。" if empty_files else ""
        return {
            "reply": f"记录显示项目{check_name}检查失败，且已有项目文件。{empty_note}我会让构建 Agent 查看失败输出和现有代码，修复后重新运行构建与交互检查。",
            "action": "repair_build",
            "rationale": "已有源码且构建检查失败，适合在原项目中定位并修复。",
            "confidence": 0.7,
        }
    if phase == "FAILED":
        detail = error or "失败原因尚未记录"
        return {
            "reply": f"当前项目处于失败状态，记录的错误是：{detail}。现有记录还不足以确定根因；需要结合最近的 Agent 事件、服务日志和构建检查结果继续排查。",
            "action": "none",
            "rationale": "没有足够证据判断应重试、验收还是修改源码。",
            "confidence": 0.3,
        }
    recent = case["recent_events"]
    latest = recent[0]["summary"] if recent else "暂无执行事件"
    return {
        "reply": f"当前阶段是 {phase or '未知'}，最近记录：{latest}。如果刚才出现了短暂报错，需要保留发生时间和完整错误提示，才能对应服务日志定位。",
        "action": "none",
        "rationale": "当前项目状态没有记录持续的失败。",
        "confidence": 0.4,
    }


async def _ask_model(case: dict[str, Any]) -> dict[str, Any] | None:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        return None
    system = (
        "你是单人创作者的编程搭档，可以回答当前项目方案、构建状态与故障问题。"
        "只依据输入的目标、已确认方案、阶段、最近事件、验收结果和安全摘要回答；"
        "遇到故障，把事实、推测与缺失证据分开。HTTP 500 不能单独证明源码或模型故障。"
        "如果可安全自动处理，只能从输入的 permitted_actions 中选择一个动作；否则选 none 并说明下一项具体排查。"
        "若已批准方案、有现成源码且构建或交互检查失败，选择 repair_build，让构建 Agent 先检查现有代码和失败输出；"
        "不需要预先知道具体是哪一行错了。若需要新的产品选择，构建 Agent 会另行向用户提问。"
        "不要编造日志、堆栈、文件内容或已执行的修复，不要索取用户密钥。"
        "只输出 JSON 对象，含 reply（简洁中文）、action（none/retry_build/verify_saved/repair_build）、"
        "rationale（动作依据）、confidence（0 到 1 的数字）。"
    )
    payload = {
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(case, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0.1,
        "max_tokens": 800,
    }
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        return result if isinstance(result, dict) else None
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None


def _allowed_action(action: str, snapshot: dict[str, Any], case: dict[str, Any]) -> bool:
    if action == "none":
        return True
    if snapshot.get("phase") != "FAILED" or snapshot.get("pending_question"):
        return False
    plan = snapshot.get("plan") or {}
    if not isinstance(plan, dict) or plan.get("status") != "approved":
        return False
    artifacts = case["diagnostic_artifacts"]
    verification = case["verification"]
    check_failed = verification["build_status"] == "failed" or verification["interaction_status"] == "failed"
    if action == "verify_saved":
        return "MaxIterationsReached" in str(snapshot.get("last_error") or "") and artifacts["dist_entry_exists"] and not check_failed
    if action == "repair_build":
        return artifacts["package_json_exists"] and not _is_agent_server_500(str(snapshot.get("last_error") or ""))
    if action == "retry_build":
        return not check_failed and not _is_agent_server_500(str(snapshot.get("last_error") or ""))
    return False


async def diagnose(snapshot: dict[str, Any], question: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Diagnose a project from bounded evidence and propose, but never run, a fix.

    The model is advisory. Its action must pass deterministic phase/artifact
    checks; callers must still perform their own guards before any mutation.
    """
    case = _case(snapshot, question, evidence)
    case["permitted_actions"] = [
        action for action in ("none", "repair_build", "verify_saved", "retry_build")
        if _allowed_action(action, snapshot, case)
    ]
    fallback = _fallback(case)
    # The current 500 reports only an HTTP status. Without a server-side error
    # detail, a model cannot establish which component failed.
    if case["failure_location"] == "agent_server_conversation_create" and not _has_server_error_detail(case, evidence):
        result = fallback
    else:
        result = await _ask_model(case)
    if not isinstance(result, dict) or not isinstance(result.get("reply"), str) or not result["reply"].strip():
        result = fallback
    action = str(result.get("action") or "none")
    if action not in _ACTIONS or not _allowed_action(action, snapshot, case):
        result = fallback
        action = fallback["action"]
    elif action == "none" and fallback["action"] in {"repair_build", "verify_saved"}:
        # A verified failed check and an approved plan are enough to inspect
        # saved files. The model may be overcautious about unknown source lines;
        # that uncertainty should guide the repair, not suppress the attempt.
        result = fallback
        action = fallback["action"]
    try:
        confidence = max(0.0, min(float(result.get("confidence", 0)), 1.0))
    except (ValueError, TypeError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    reply = _safe_text(result.get("reply"), 1200).strip() or fallback["reply"]
    rationale = _safe_text(result.get("rationale"), 400).strip() or fallback["rationale"]
    return {
        "reply": reply,
        "action": action,
        "rationale": rationale,
        "confidence": confidence,
        "diagnostic_artifacts": case["diagnostic_artifacts"],
    }
