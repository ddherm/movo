"""A truthful, display-ready view of project workflow progress.

OpenHands reports actions and observations, but neither predicts how much
development work remains.  The percentage here counts completed workflow
milestones only; ``indeterminate`` marks phases with work still in progress.
"""

from __future__ import annotations

from typing import Any


STAGE_COUNT = 7

# The index is the number of completed workflow milestones, not an estimate
# of effort or elapsed time.  READY completes both verification checks and
# makes the preview available, so the last two milestones arrive together.
_PHASES: dict[str, tuple[int, str, str, bool]] = {
    "IDEA": (0, "记录想法", "等待输入项目想法", False),
    "CLARIFY": (1, "澄清需求", "正在确认项目要解决的问题", False),
    "RESEARCH": (2, "准备方案", "正在调研并整理实现方案", False),
    "PLAN_REVIEW": (3, "确认方案", "方案已准备好，等待你确认", False),
    "BUILD": (4, "Agent 构建", "正在准备隔离工作区", True),
    "WAITING_DECISION": (4, "等待决定", "构建暂停，等待你的产品决定", False),
    "VERIFY": (5, "验证原型", "正在检查构建产物与核心交互", True),
    "READY": (7, "可预览", "原型已通过构建与交互检查", False),
    "FAILED": (4, "需要处理", "构建遇到问题，请查看记录", False),
    "PAUSED": (1, "已暂停", "任务已暂停，等待继续", False),
}

_NOISE_SUMMARIES = {"Agent 状态已更新"}
_NOISE_KINDS = {"notification_delivery_failed", "stale_build_result", "stale_planner_result"}


def _latest_step(events: Any) -> str | None:
    """Use the newest meaningful, redacted event from Store.snapshot()."""
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        summary = event.get("summary")
        if kind in _NOISE_KINDS or not isinstance(summary, str):
            continue
        summary = summary.strip()
        if summary and summary not in _NOISE_SUMMARIES:
            return summary
    return None


def build_progress(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Return completed workflow milestones and the latest actual step.

    ``stage_index`` is a completed milestone count from 0 to 7.  A BUILD
    response of 4/7 means the plan was approved, not that code is 57% done.
    No Agent event count, time estimate, or token count changes this value.
    """
    if snapshot is None:
        index, label, default_step, indeterminate = _PHASES["IDEA"]
        step = default_step
    else:
        phase = str(snapshot.get("phase") or "IDEA")
        index, label, default_step, indeterminate = _PHASES.get(phase, _PHASES["IDEA"])
        if phase == "PAUSED" and (snapshot.get("plan") or {}).get("status") == "approved":
            index = 4
        if phase == "WAITING_DECISION":
            question = (snapshot.get("pending_question") or {}).get("question")
            step = f"等待你决定：{question}" if isinstance(question, str) and question.strip() else default_step
        else:
            step = _latest_step(snapshot.get("events")) or default_step

    return {
        "stage_index": index,
        "stage_count": STAGE_COUNT,
        "stage_label": label,
        "step": step,
        "percent": round(index / STAGE_COUNT * 100),
        "indeterminate": indeterminate,
    }
