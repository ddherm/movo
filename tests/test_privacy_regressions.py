"""Credentials supplied through product inputs must never reach persistent state."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from backend.core.service import Coordinator
from backend.core.store import StateError


def _assert_token_absent_from_database(coordinator: Coordinator, token: str) -> None:
    database = coordinator.store.db_path
    with sqlite3.connect(database) as connection:
        logical_contents = "\n".join(connection.iterdump())
    assert token not in logical_contents

    # A deleted value can remain in SQLite pages or the write-ahead log.
    for path in (database, database.with_name(database.name + "-wal")):
        if path.exists():
            assert token.encode() not in path.read_bytes()


async def _allow_rejection(operation) -> None:
    try:
        await operation
    except StateError:
        pass


def _proposal() -> dict[str, str]:
    return {
        "goal": "做一个学习计划页",
        "audience": "学生",
        "flow": "输入目标并查看计划",
        "scope": "单页演示",
        "exclusions": "账号与支付",
        "route": "静态网页",
        "reasons": "足以验证核心交互",
        "acceptance": "手机上能完成一次输入",
        "source_scope": "未联网检索",
    }


def test_secret_in_initial_idea_is_not_persisted(tmp_path) -> None:
    coordinator = Coordinator(tmp_path)
    token = "sk-FAKE-IDEA-PRIVACY-TOKEN-2026"

    asyncio.run(_allow_rejection(coordinator.create_project(f"做学习计划页 {token}")))

    _assert_token_absent_from_database(coordinator, token)


@pytest.mark.parametrize("label", ["密钥", "秘钥", "令牌", "凭证"])
def test_chinese_labeled_secret_in_initial_idea_is_not_persisted(tmp_path, label: str) -> None:
    coordinator = Coordinator(tmp_path)
    token = "FAKE_TEST_VALUE_1234"

    asyncio.run(_allow_rejection(coordinator.create_project(f"做学习计划页，{label}：{token}")))

    _assert_token_absent_from_database(coordinator, token)


@pytest.mark.parametrize("field", ["text", "reason"])
def test_secret_in_change_request_is_not_persisted(tmp_path, field: str) -> None:
    coordinator = Coordinator(tmp_path)
    asyncio.run(coordinator.create_project("做学习计划页"))
    token = f"sk-FAKE-CHANGE-{field.upper()}-TOKEN-2026"
    text = f"增加日历 {token}" if field == "text" else "增加日历"
    reason = f"用户反馈 {token}" if field == "reason" else "用户反馈"

    asyncio.run(_allow_rejection(coordinator.change(text, reason)))

    _assert_token_absent_from_database(coordinator, token)


@pytest.mark.parametrize("label", ["密钥", "秘钥", "令牌", "凭证"])
@pytest.mark.parametrize("field", ["text", "reason"])
def test_chinese_labeled_secret_in_change_request_is_not_persisted(tmp_path, label: str, field: str) -> None:
    coordinator = Coordinator(tmp_path)
    asyncio.run(coordinator.create_project("做学习计划页"))
    token = "FAKE_TEST_VALUE_1234"
    text = f"增加日历，{label}：{token}" if field == "text" else "增加日历"
    reason = f"用户反馈，{label}：{token}" if field == "reason" else "用户反馈"

    asyncio.run(_allow_rejection(coordinator.change(text, reason)))

    _assert_token_absent_from_database(coordinator, token)


def test_secret_in_decision_reason_is_not_persisted(tmp_path, monkeypatch) -> None:
    coordinator = Coordinator(tmp_path)
    asyncio.run(coordinator.create_project("做学习计划页"))
    coordinator.store.add_plan(_proposal())
    coordinator.store.approve_plan()
    question_id = coordinator.store.open_question({
        "trigger": "需要确定账号范围",
        "question": "首版只供自己使用吗？",
        "impact": "多人使用需要账号和存储。",
        "options": [
            {"id": "solo", "label": "单人", "impact": "实现较快"},
            {"id": "multi", "label": "多人", "impact": "增加账号系统"},
        ],
        "recommended_option_id": "solo",
        "blocking_scope": "账号与存储",
    })
    monkeypatch.setattr(coordinator, "schedule_build", lambda: False)
    token = "sk-FAKE-DECISION-REASON-TOKEN-2026"

    asyncio.run(_allow_rejection(coordinator.answer(question_id, "solo", f"我选单人 {token}", "text")))

    _assert_token_absent_from_database(coordinator, token)


@pytest.mark.parametrize("label", ["密钥", "秘钥", "令牌", "凭证"])
def test_chinese_labeled_secret_in_decision_reason_is_not_persisted(tmp_path, monkeypatch, label: str) -> None:
    coordinator = Coordinator(tmp_path)
    asyncio.run(coordinator.create_project("做学习计划页"))
    coordinator.store.add_plan(_proposal())
    coordinator.store.approve_plan()
    question_id = coordinator.store.open_question({
        "trigger": "需要确定账号范围",
        "question": "首版只供自己使用吗？",
        "impact": "多人使用需要账号和存储。",
        "options": [
            {"id": "solo", "label": "单人", "impact": "实现较快"},
            {"id": "multi", "label": "多人", "impact": "增加账号系统"},
        ],
        "recommended_option_id": "solo",
        "blocking_scope": "账号与存储",
    })
    monkeypatch.setattr(coordinator, "schedule_build", lambda: False)
    token = "FAKE_TEST_VALUE_1234"

    asyncio.run(_allow_rejection(coordinator.answer(question_id, "solo", f"我选单人，{label}：{token}", "text")))

    _assert_token_absent_from_database(coordinator, token)
