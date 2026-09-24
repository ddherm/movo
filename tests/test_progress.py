from backend.core.progress import build_progress


def test_agent_activity_does_not_invent_build_completion():
    snapshot = {
        "phase": "BUILD",
        "events": [
            {"kind": "agent_event", "summary": "Agent 状态已更新"},
            {"kind": "agent_event", "summary": "Agent 正在执行开发操作"},
            {"kind": "agent_session", "summary": "已创建 Agent 会话"},
        ],
    }
    before = build_progress(snapshot)
    snapshot["events"] = [
        {"kind": "agent_event", "summary": "Agent 状态已更新"} for _ in range(100)
    ] + snapshot["events"]
    after = build_progress(snapshot)

    assert before == after
    assert before["stage_index"] == 4
    assert before["stage_count"] == 7
    assert before["step"] == "Agent 正在执行开发操作"
    assert before["indeterminate"] is True
    assert before["percent"] < 100


def test_waiting_decision_and_failure_never_look_complete():
    waiting = build_progress({
        "phase": "WAITING_DECISION",
        "pending_question": {"question": "要保留登录吗？"},
        "events": [{"kind": "agent_event", "summary": "开发工具已返回结果"}],
    })
    failed = build_progress({
        "phase": "FAILED",
        "events": [{"kind": "build_failed", "summary": "构建与交互检查没有通过。"}],
    })
    ready = build_progress({
        "phase": "READY",
        "events": [{"kind": "phase_changed", "summary": "原型与验收说明已准备好。"}],
    })

    assert waiting["step"] == "等待你决定：要保留登录吗？"
    assert waiting["indeterminate"] is False
    assert waiting["stage_index"] == failed["stage_index"] == 4
    assert failed["percent"] < ready["percent"] == 100
    assert ready["stage_index"] == ready["stage_count"]


def test_changed_plan_resets_progress_without_reusing_old_agent_events():
    paused = build_progress({
        "phase": "PAUSED",
        "plan": None,
        "events": [
            {"kind": "spec_changed", "summary": "需求已更新，等待安全边界调整。"},
            {"kind": "phase_changed", "summary": "原型与验收说明已准备好。"},
        ],
    })
    assert paused["stage_index"] == 1
    assert paused["indeterminate"] is False
    assert paused["step"] == "需求已更新，等待安全边界调整。"

    paused_with_approved_plan = build_progress({
        "phase": "PAUSED",
        "plan": {"status": "approved"},
        "events": [{"kind": "phase_changed", "summary": "Agent 已暂停；可以继续同一项目。"}],
    })
    assert paused_with_approved_plan["stage_index"] == 4
