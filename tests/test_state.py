from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from backend.core.store import StateError, Store


def proposal():
    return {
        "goal": "学生在手机上试用学习计划页面",
        "audience": "学生",
        "flow": "填写目标，生成本地示例计划",
        "scope": "单页、表单、状态切换、本地演示数据",
        "exclusions": "账号和支付",
        "route": "静态响应式网页",
        "reasons": "足以验证核心操作",
        "acceptance": "在手机填写目标并看到计划状态变化",
        "source_scope": "未联网检索",
        "sources": [],
    }


def question():
    return {
        "trigger": "需要决定是否保留登录",
        "question": "首版只给你自己试，还是需要多人登录？",
        "impact": "多人登录需要额外的账号和云端存储。",
        "options": [
            {"id": "solo", "label": "先做单人版本", "impact": "更快验证核心体验"},
            {"id": "multi", "label": "需要多人登录", "impact": "增加开发时间和存储"},
        ],
        "recommended_option_id": "solo",
        "blocking_scope": "登录与数据保存",
    }


def test_plan_gate_and_restart_persistence(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = Store(path)
    store.create_project("给学生做学习计划")
    store.add_asset("reference.png", "image/png", "uploads/example.png")
    plan_id = store.add_plan(proposal())
    snapshot = store.snapshot()
    assert snapshot["phase"] == "PLAN_REVIEW"
    assert snapshot["plan"]["id"] == plan_id
    assert snapshot["screenshots"][0]["name"] == "reference.png"
    assert snapshot["agent_conversation_id"] is None

    restarted = Store(path)
    assert restarted.snapshot()["plan"]["status"] == "pending"
    restarted.approve_plan()
    assert restarted.snapshot()["phase"] == "BUILD"


def test_first_valid_decision_wins_across_concurrent_channels(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.create_project("做一个表单")
    store.add_plan(proposal())
    store.approve_plan()
    question_id = store.open_question(question())

    def answer(item):
        option, channel = item
        return store.answer_question(question_id, option, "我的选择", channel)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, [("solo", "voice"), ("multi", "text")]))

    assert sum(result["accepted"] for result in results) == 1
    state = Store(store.db_path).snapshot()
    assert state["phase"] == "BUILD"
    assert state["pending_question"] is None
    assert len(state["decisions"]) == 1
    assert state["decisions"][0]["spec_version"] == 1


def test_change_versions_and_notification_deduplication(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.create_project("做活动页")
    store.add_plan(proposal())
    store.approve_plan()
    assert store.change_spec("删掉支付", "首版不需要") == 2
    state = store.snapshot()
    assert state["phase"] == "PAUSED"
    assert state["spec_versions"][-1]["text"] == "删掉支付"
    assert state["plan"] is None
    assert state["plan_history"][0]["spec_version"] == 1
    assert store.finish_build(1, "旧构建通过") is False
    assert store.snapshot()["phase"] == "PAUSED"
    assert store.notify_once("same-key", "decision", "有一个问题待决定")
    assert store.notify_once("same-key", "decision", "有一个问题待决定") is None
    assert store.claim_notification_delivery("same-key") is True
    assert store.claim_notification_delivery("same-key") is False


def test_invalid_decision_does_not_close_question(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.create_project("做活动页")
    store.add_plan(proposal())
    store.approve_plan()
    question_id = store.open_question(question())
    with pytest.raises(StateError):
        store.answer_question(question_id, "unknown", "", "voice")
    assert store.snapshot()["pending_question"]["id"] == question_id


def test_failed_build_cannot_be_published_without_iteration_limit_reason(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.create_project("做一个活动页")
    store.add_plan(proposal())
    store.approve_plan()
    assert store.fail_build(1, "构建命令失败", "构建未完成") is True
    assert store.finish_build(1, "没有重新验证", from_iteration_limit=True) is False
    assert store.snapshot()["phase"] == "FAILED"


def test_failed_notification_can_retry_then_stops_after_delivery(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    store.create_project("做一个活动页")
    store.notify_once("review:1", "plan_review", "方案待确认")
    assert store.claim_notification_delivery("review:1") is True
    assert store.claim_notification_delivery("review:1") is False
    assert store.pending_notification_deliveries() == [{"dedupe_key": "review:1", "kind": "plan_review"}]
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE notifications SET delivery_attempted_at='2000-01-01T00:00:00+00:00' WHERE dedupe_key='review:1'")
    assert store.claim_notification_delivery("review:1") is True
    store.mark_notification_delivered("review:1")
    assert store.pending_notification_deliveries() == []


def test_multiple_projects_keep_state_and_notifications_separate(tmp_path):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    store = Store(tmp_path / "state.sqlite3")
    first_id = store.create_project("第一个页面", str(first_dir))
    store.add_message("user", "第一条消息")
    store.notify_once("phase:READY:1", "ready", "第一个项目完成")

    second_id = store.create_project("第二个页面", str(second_dir))
    assert store.snapshot()["id"] == second_id
    assert store.snapshot()["messages"] == []
    assert store.pending_notification_deliveries() == []
    assert store.notify_once("phase:READY:1", "ready", "第二个项目完成")
    assert len(store.pending_notification_deliveries()) == 1
    assert {item["id"] for item in store.list_projects()} == {first_id, second_id}
    with pytest.raises(StateError, match="文件夹"):
        store.create_project("重复路径", str(first_dir))

    store.select_project(first_id)
    assert store.snapshot()["messages"][0]["text"] == "第一条消息"
    assert store.snapshot()["source_dir"] == str(first_dir)
    assert len(store.pending_notification_deliveries()) == 1
    store.mark_notification_delivered("phase:READY:1")
    restarted = Store(store.db_path)
    assert restarted.active_project_id() == first_id
    assert restarted.pending_notification_deliveries() == []
    restarted.select_project(second_id)
    assert len(restarted.pending_notification_deliveries()) == 1


def test_switch_is_blocked_during_active_build(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    first_id = store.create_project("第一个")
    second_id = store.create_project("第二个")
    store.add_plan(proposal())
    store.approve_plan()
    with pytest.raises(StateError, match="构建"):
        store.select_project(first_id)
    with pytest.raises(StateError, match="构建"):
        store.create_project("第三个")
    assert store.active_project_id() == second_id


def test_single_project_database_migrates_without_losing_state(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """CREATE TABLE project (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, idea TEXT NOT NULL,
                phase TEXT NOT NULL, spec_version INTEGER NOT NULL DEFAULT 1,
                agent_conversation_id TEXT, current_plan_id TEXT,
                pending_question_id TEXT, preview_url TEXT, last_error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE notifications (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
                text TEXT NOT NULL, created_at TEXT NOT NULL,
                delivery_attempted_at TEXT, delivered_at TEXT,
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(project_id) REFERENCES project(id)
            );"""
        )
        conn.execute(
            "INSERT INTO project (id,task_id,idea,phase,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("old-id", "old-task", "旧项目", "READY", "2026-01-01", "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO notifications VALUES (?,?,?,?,?,?,?,?,?)",
            ("n1", "old-id", "phase:READY:1", "ready", "旧通知", "2026-01-01", None, None, 0),
        )
    store = Store(path)
    assert store.active_project_id() == "old-id"
    assert store.snapshot()["idea"] == "旧项目"
    assert store.snapshot()["source_dir"] is None
    assert store.pending_notification_deliveries() == [{"dedupe_key": "phase:READY:1", "kind": "ready"}]
    store.create_project("新项目")
    assert store.notify_once("phase:READY:1", "ready", "新通知")
    store.select_project("old-id")
    assert store.pending_notification_deliveries() == [{"dedupe_key": "phase:READY:1", "kind": "ready"}]
