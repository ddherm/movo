"""Regressions for task handoff and versioned artifacts.

The fake planner/build callbacks control timing without contacting models,
Docker, or voice providers.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.core.service import Coordinator
from backend.core.store import StateError


def proposal(goal: str = "给学生做学习计划") -> dict:
    return {
        "goal": goal,
        "audience": "学生",
        "flow": "填写目标并看到本地计划",
        "scope": "响应式页面与本地演示数据",
        "exclusions": "账号和支付",
        "route": "静态网页",
        "reasons": "先验证核心操作",
        "acceptance": "在手机填写目标并看到状态变化",
        "source_scope": "未联网检索",
        "sources": [],
    }


def decision_question() -> dict:
    return {
        "trigger": "登录范围需要明确",
        "question": "首版需要多人登录吗？",
        "impact": "多人登录会增加开发范围。",
        "options": [
            {"id": "solo", "label": "先做单人版", "impact": "更快试用"},
            {"id": "multi", "label": "需要多人版", "impact": "增加账号系统"},
        ],
        "recommended_option_id": "solo",
        "blocking_scope": "登录与保存",
    }


def test_answer_before_previous_build_unwinds_still_resumes_same_task(tmp_path, monkeypatch):
    """The old waiting_decision result must not mark an accepted answer FAILED."""
    from backend import integrations

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("给学生做一个页面")
        coordinator.store.add_plan(proposal())
        question_opened = asyncio.Event()
        allow_old_run_to_end = asyncio.Event()
        resumed = asyncio.Event()
        calls = []
        conversation_id = str(uuid.uuid4())

        async def fake_build(snapshot, _on_event, on_question):
            calls.append(snapshot)
            if len(calls) == 1:
                await on_question(decision_question())
                question_opened.set()
                await allow_old_run_to_end.wait()
                return {"status": "waiting_decision", "conversation_id": conversation_id}
            resumed.set()
            return {"status": "paused", "conversation_id": conversation_id}

        monkeypatch.setattr(integrations, "run_build", fake_build)
        await coordinator.approve_plan()
        await asyncio.wait_for(question_opened.wait(), 2)
        question_id = coordinator.snapshot()["pending_question"]["id"]
        answer = await coordinator.answer(question_id, "solo", "先验证核心流程", "text")
        assert answer["accepted"] is True
        assert coordinator.snapshot()["phase"] == "BUILD"

        allow_old_run_to_end.set()
        await asyncio.wait_for(resumed.wait(), 2)
        await asyncio.wait_for(coordinator._build_task, 2)
        state = coordinator.snapshot()
        assert len(calls) == 2
        assert calls[1]["agent_conversation_id"] == conversation_id
        assert calls[1]["decision"]["option_id"] == "solo"
        assert state["phase"] == "PAUSED"
        assert state["last_error"] is None
        assert len(state["decisions"]) == 1

    asyncio.run(scenario())


def test_old_build_question_cannot_block_new_approved_spec(tmp_path, monkeypatch):
    """A delayed v1 question must not become the pending decision for v2."""
    from backend import integrations

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("给学生做一个页面")
        coordinator.store.add_plan(proposal("第一版目标"))
        old_run_started = asyncio.Event()
        allow_old_question = asyncio.Event()
        new_run_started = asyncio.Event()
        calls = []

        async def fake_build(snapshot, _on_event, on_question):
            calls.append(snapshot["spec_version"])
            if snapshot["spec_version"] == 1:
                old_run_started.set()
                await allow_old_question.wait()
                await on_question(decision_question())
                return {"status": "waiting_decision"}
            new_run_started.set()
            return {"status": "paused"}

        monkeypatch.setattr(integrations, "run_build", fake_build)
        await coordinator.approve_plan()
        await asyncio.wait_for(old_run_started.wait(), 2)
        old_task = coordinator._build_task

        await coordinator.change("新版需求：删掉支付", "先缩小范围", trigger_plan=False)
        coordinator.store.add_plan(proposal("第二版目标"))
        await coordinator.approve_plan()
        assert coordinator.snapshot()["phase"] == "BUILD"
        assert coordinator.snapshot()["spec_version"] == 2

        allow_old_question.set()
        await asyncio.wait_for(old_task, 2)
        assert coordinator.snapshot()["pending_question"] is None
        await asyncio.wait_for(new_run_started.wait(), 2)
        await asyncio.wait_for(coordinator._build_task, 2)

        state = coordinator.snapshot()
        assert calls == [1, 2]
        assert state["spec_version"] == 2
        assert state["phase"] == "PAUSED"
        assert state["pending_question"] is None
        assert state["last_error"] is None

    asyncio.run(scenario())


def test_stale_async_plan_cannot_become_new_spec_plan(tmp_path):
    """A planner result based on v1 is discarded if the user moves to v2."""

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("旧想法")
        planner_started = asyncio.Event()
        release_old_plan = asyncio.Event()
        seen_versions = []

        async def fake_next_turn(snapshot):
            seen_versions.append(snapshot["spec_version"])
            planner_started.set()
            await release_old_plan.wait()
            return {"reply": "旧版方案已准备好", "plan": proposal("旧版目标")}

        coordinator.planner.next_turn = fake_next_turn
        planning = asyncio.create_task(coordinator.plan_current_spec())
        await asyncio.wait_for(planner_started.wait(), 2)
        await coordinator.change("新版需求：删掉支付", "先缩小范围", trigger_plan=False)
        release_old_plan.set()
        await asyncio.wait_for(planning, 2)

        state = coordinator.snapshot()
        assert seen_versions == [1]
        assert state["spec_version"] == 2
        assert state["phase"] == "CLARIFY"
        assert state["plan"] is None
        assert state["plan_history"] == []
        assert not any(message["text"] == "旧版方案已准备好" for message in state["messages"])

    asyncio.run(scenario())


@pytest.fixture
def isolated_api(tmp_path, monkeypatch):
    # backend.api creates its default Coordinator at import time, so point it
    # at the test directory before importing the module.
    monkeypatch.setenv("VOICE_COMPANION_DATA_DIR", str(tmp_path))
    from backend import api

    coordinator = Coordinator(tmp_path)
    monkeypatch.setattr(api, "coordinator", coordinator)
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    with TestClient(api.app) as client:
        yield client, coordinator, tmp_path


def test_upload_during_pending_decision_does_not_save_file_or_asset(isolated_api):
    client, coordinator, data_dir = isolated_api
    client.post("/api/project", json={"idea": "做一个表单"}).raise_for_status()
    coordinator.store.add_plan(proposal())
    coordinator.store.approve_plan()
    coordinator.store.open_question(decision_question())

    image = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    response = client.post("/api/assets", files={"file": ("new.png", image.getvalue(), "image/png")})

    state = client.get("/api/project").json()
    assert response.status_code == 409
    assert state["phase"] == "WAITING_DECISION"
    assert state["spec_version"] == 1
    assert state["screenshots"] == []
    assert list((data_dir / "uploads").glob("*")) == []


def test_old_preview_and_verification_are_hidden_after_spec_change(isolated_api):
    client, coordinator, data_dir = isolated_api
    project = client.post("/api/project", json={"idea": "做一个活动页"}).json()
    coordinator.store.add_plan(proposal())
    coordinator.store.approve_plan()
    preview = Path(project["source_dir"]) / "dist" / "index.html"
    preview.parent.mkdir(parents=True)
    preview.write_text("<h1>旧版作品</h1>", encoding="utf-8")
    assert coordinator.store.finish_build(1, "构建与交互通过") is True
    assert client.get("/preview/").status_code == 200

    coordinator.store.change_spec("删掉支付", "先验证核心流程")
    state = client.get("/api/project").json()
    assert state["spec_version"] == 2
    assert state["preview_url"] is None
    assert state["verification"] is None
    assert client.get("/preview/").status_code == 404
    assert preview.is_file()  # Old artifact remains traceable on disk.


def test_new_project_uses_chosen_folder_and_switch_keeps_previews_isolated(isolated_api):
    client, coordinator, data_dir = isolated_api
    folder = client.get("/api/folders").json()
    assert folder["default_path"] == str(data_dir)

    first = client.post("/api/project", json={
        "idea": "第一个作品", "parent_dir": str(data_dir), "folder_name": "作品甲",
    })
    assert first.status_code == 201
    first_id = first.json()["id"]
    first_source = data_dir / "作品甲"
    assert first.json()["source_dir"] == str(first_source)
    coordinator.store.add_plan(proposal())
    coordinator.store.approve_plan()
    first_preview = first_source / "dist" / "index.html"
    first_preview.parent.mkdir()
    first_preview.write_text("<h1>作品甲</h1>", encoding="utf-8")
    assert coordinator.store.finish_build(1, "构建与交互通过") is True
    assert "作品甲" in client.get("/preview/").text

    second = client.post("/api/project", json={
        "idea": "第二个作品", "parent_dir": str(data_dir), "folder_name": "作品乙",
    })
    assert second.status_code == 201
    assert second.json()["source_dir"] == str(data_dir / "作品乙")
    assert client.get("/preview/").status_code == 404
    listing = client.get("/api/projects").json()
    assert len(listing["projects"]) == 2
    assert listing["active_project_id"] == second.json()["id"]

    selected = client.post("/api/projects/select", json={"project_id": first_id})
    assert selected.status_code == 200
    assert selected.json()["id"] == first_id
    assert "作品甲" in client.get("/preview/").text
    duplicate = client.post("/api/project", json={
        "idea": "重复目录", "parent_dir": str(data_dir), "folder_name": "作品甲",
    })
    assert duplicate.status_code == 409


def test_iteration_limited_build_can_publish_only_after_saved_artifact_passes(isolated_api, monkeypatch):
    from backend import integrations

    client, coordinator, data_dir = isolated_api
    project = client.post("/api/project", json={"idea": "做一个活动页"}).json()
    coordinator.store.add_plan(proposal())
    coordinator.store.approve_plan()
    coordinator.store.fail_build(1, "ConversationRunError: MaxIterationsReached: Agent reached limit", "Agent 已停止。")
    preview = Path(project["source_dir"]) / "dist" / "index.html"
    preview.parent.mkdir(parents=True)
    preview.write_text("<h1>已构建</h1>", encoding="utf-8")
    calls = []

    async def fake_verify(snapshot):
        calls.append(snapshot["spec_version"])
        return {
            "status": "ready", "preview_dir": str(preview.parent),
            "verification": {"build_status": "passed", "interaction_status": "passed", "details": {"checked_assets": 0}},
        }

    monkeypatch.setattr(integrations, "verify_saved_build", fake_verify)
    response = client.post("/api/build/reverify")
    assert response.status_code == 200
    assert calls == [1]
    assert response.json()["phase"] == "READY"
    assert client.get("/preview/").status_code == 200


def test_saved_artifact_cannot_publish_after_spec_changes_during_verification(isolated_api, monkeypatch):
    from backend import integrations

    client, coordinator, data_dir = isolated_api
    project = client.post("/api/project", json={"idea": "做一个活动页"}).json()
    coordinator.store.add_plan(proposal())
    coordinator.store.approve_plan()
    coordinator.store.fail_build(1, "ConversationRunError: MaxIterationsReached: Agent reached limit", "Agent 已停止。")
    preview = Path(project["source_dir"]) / "dist" / "index.html"
    preview.parent.mkdir(parents=True)
    preview.write_text("<h1>旧版</h1>", encoding="utf-8")

    async def fake_verify(_snapshot):
        coordinator.store.change_spec("删掉登录", "需求变化")
        return {
            "status": "ready", "preview_dir": str(preview.parent),
            "verification": {"build_status": "passed", "interaction_status": "passed", "details": {}},
        }

    monkeypatch.setattr(integrations, "verify_saved_build", fake_verify)
    response = client.post("/api/build/reverify")
    assert response.status_code == 409
    assert coordinator.snapshot()["spec_version"] == 2
    assert coordinator.snapshot()["phase"] == "CLARIFY"
    assert client.get("/preview/").status_code == 404


def test_voice_progress_does_not_rewrite_pending_plan(tmp_path):
    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("做一个工具页")
        coordinator.store.add_plan(proposal())
        plan_id = coordinator.snapshot()["plan"]["id"]

        for utterance in ("你好", "现在项目进展到哪里了？"):
            state = await coordinator.message(utterance, "voice")
            assert state["phase"] == "PLAN_REVIEW"
            assert state["spec_version"] == 1
            assert state["plan"]["id"] == plan_id
        assert "等你确认" in state["messages"][-1]["text"]

        coordinator.store.approve_plan()
        state = await coordinator.message("现在项目进展到哪里了？", "voice")
        assert state["phase"] == "BUILD"
        assert state["spec_version"] == 1
        assert "最近真实记录" in state["messages"][-1]["text"]

    asyncio.run(scenario())


def test_explicit_progress_bar_change_still_revises_plan(tmp_path):
    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("做一个工具页")
        coordinator.store.add_plan(proposal())

        async def no_model_call():
            pass

        coordinator._plan_turn = no_model_call
        state = await coordinator.message("增加进度条", "voice")
        assert state["spec_version"] == 2
        assert state["phase"] == "CLARIFY"
        assert state["plan"] is None

    asyncio.run(scenario())


def test_failed_build_error_question_uses_project_diagnosis(isolated_api, monkeypatch):
    """The error card's follow-up must use the failed run, not a canned edit prompt."""
    client, coordinator, _ = isolated_api
    client.post("/api/project", json={"idea": "做一个预约页面"}).raise_for_status()
    coordinator.store.add_plan(proposal())
    coordinator.store.approve_plan()
    error = "HTTPStatusError: Server error '500 Internal Server Error' for url 'http://127.0.0.1:33941/api/conversations'"
    assert coordinator.store.fail_build(1, error, "OpenHands 会话服务请求失败")
    seen = []

    async def fake_diagnose(text, snapshot):
        seen.append((text, snapshot))
        return "OpenHands 会话服务返回 500。我会先检查服务日志和保存的项目文件，再决定能否自动恢复。"

    monkeypatch.setattr(coordinator, "diagnose_issue", fake_diagnose, raising=False)
    response = client.post("/api/messages", json={"text": "为什么出错了", "channel": "text"})

    assert response.status_code == 200
    state = response.json()
    assert len(seen) == 1
    assert seen[0][0] == "为什么出错了"
    assert seen[0][1]["last_error"] == error
    assert seen[0][1]["phase"] == "FAILED"
    assert state["phase"] == "FAILED"
    assert state["spec_version"] == 1
    assert "会话服务返回 500" in state["messages"][-1]["text"]
    assert "若要改变作品" not in state["messages"][-1]["text"]


def test_build_error_question_precedes_generic_progress_reply(tmp_path, monkeypatch):
    """'卡住' matches progress too, but a why-question asks for diagnosis."""

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("做一个预约页面")
        coordinator.store.add_plan(proposal())
        coordinator.store.approve_plan()
        coordinator.store.event("agent_http_error", "OpenHands 会话接口返回 500")
        original_plan_id = coordinator.snapshot()["plan"]["id"]
        seen = []

        async def fake_diagnose(text, snapshot):
            seen.append((text, snapshot))
            return "会话接口出现 500，正在检查 Agent 日志。"

        monkeypatch.setattr(coordinator, "diagnose_issue", fake_diagnose, raising=False)
        state = await coordinator.message("为什么卡住了？", "voice")

        assert len(seen) == 1
        assert seen[0][1]["phase"] == "BUILD"
        assert any(event["kind"] == "agent_http_error" for event in seen[0][1]["events"])
        assert state["messages"][-1]["text"] == "会话接口出现 500，正在检查 Agent 日志。"
        assert state["phase"] == "BUILD"
        assert state["spec_version"] == 1
        assert state["plan"]["id"] == original_plan_id

    asyncio.run(scenario())


def test_failed_build_keeps_verification_evidence_for_diagnosis(tmp_path, monkeypatch):
    from backend import integrations

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("做一个页面")
        coordinator.store.add_plan(proposal())

        async def fake_build(_snapshot, _on_event, _on_question):
            return {
                "status": "failed",
                "reason": "ConversationRunError: MaxIterationsReached",
                "verification": {
                    "build_status": "passed",
                    "interaction_status": "failed",
                    "details": {"reason": "interaction check failed", "output": "touch control did not respond"},
                },
            }

        monkeypatch.setattr(integrations, "run_build", fake_build)
        await coordinator.approve_plan()
        await asyncio.wait_for(coordinator._build_task, 2)
        state = coordinator.snapshot()
        assert state["phase"] == "FAILED"
        assert state["verification"]["build_status"] == "passed"
        assert state["verification"]["interaction_status"] == "failed"
        assert "touch control did not respond" in state["verification"]["details"]

    asyncio.run(scenario())


def test_delayed_diagnosis_cannot_reply_in_another_project(tmp_path, monkeypatch):
    """A slow model answer must stay with the project that supplied its evidence."""

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("旧项目")
        coordinator.store.add_plan(proposal())
        coordinator.store.approve_plan()
        coordinator.store.fail_build(1, "构建失败", "需要排查")
        old_project_id = coordinator.snapshot()["id"]
        started = asyncio.Event()
        release = asyncio.Event()

        async def fake_diagnose(_text, _snapshot):
            started.set()
            await release.wait()
            return "旧项目的诊断结论"

        monkeypatch.setattr(coordinator, "diagnose_issue", fake_diagnose, raising=False)
        pending = asyncio.create_task(coordinator.message("为什么出错了"))
        await asyncio.wait_for(started.wait(), 2)
        new_project = await coordinator.create_project("新项目", folder_name="新项目目录")
        assert new_project["id"] != old_project_id
        release.set()
        try:
            await asyncio.wait_for(pending, 2)
        except StateError:
            # Rejecting the stale turn is also valid.
            pass

        state = coordinator.snapshot()
        assert state["id"] == new_project["id"]
        assert all(message["text"] != "旧项目的诊断结论" for message in state["messages"])

    asyncio.run(scenario())


def test_diagnosis_repairs_failed_build_with_saved_error_context(tmp_path, monkeypatch):
    """A repair decision resumes the existing project with the failure evidence."""
    from backend import integrations
    from backend.core import diagnostics

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("做一个预约页面")
        coordinator.store.add_plan(proposal())
        coordinator.store.approve_plan()
        source = Path(coordinator.snapshot()["source_dir"])
        source.mkdir(parents=True)
        (source / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")
        error = "npm run build failed: unresolved import ./AppointmentForm"
        verification_detail = "构建退出码 1，尚未运行核心交互检查"
        assert coordinator.store.fail_build(1, error, "构建检查失败", {
            "build_status": "failed",
            "interaction_status": "not_run",
            "details": verification_detail,
        })
        original_plan_id = coordinator.snapshot()["plan"]["id"]
        builds = []

        async def fake_diagnose(snapshot, question, *, evidence=None):
            assert question == "请查 bug 并自动修复"
            assert snapshot["last_error"] == error
            return {"reply": "已定位到构建检查失败，将检查 import 并修复。", "action": "repair_build"}

        async def fake_build(snapshot, _on_event, _on_question):
            builds.append(snapshot)
            return {"status": "paused"}

        monkeypatch.setattr(diagnostics, "diagnose", fake_diagnose)
        monkeypatch.setattr(integrations, "run_build", fake_build)
        await coordinator.message("请查 bug 并自动修复")
        await asyncio.wait_for(coordinator._build_task, 2)

        assert len(builds) == 1
        assert builds[0]["repair_context"]["last_error"] == error
        assert builds[0]["repair_context"]["verification"] == verification_detail
        state = coordinator.snapshot()
        assert state["phase"] == "PAUSED"
        assert state["spec_version"] == 1
        assert state["plan"]["id"] == original_plan_id
        assert any("构建 Agent" in message["text"] for message in state["messages"] if message["role"] == "assistant")

    asyncio.run(scenario())


def test_explicit_error_ui_change_is_not_mistaken_for_diagnosis(tmp_path, monkeypatch):
    """Mentioning errors in a feature request must not invoke fault diagnosis."""

    async def scenario():
        coordinator = Coordinator(tmp_path)
        coordinator.store.create_project("做一个预约页面")
        coordinator.store.add_plan(proposal())

        async def no_model_call():
            pass

        async def forbidden_diagnosis(_text, _snapshot):
            raise AssertionError("explicit feature change was routed to diagnosis")

        coordinator._plan_turn = no_model_call
        monkeypatch.setattr(coordinator, "diagnose_issue", forbidden_diagnosis)
        state = await coordinator.message("增加报错提示")

        assert state["spec_version"] == 2
        assert state["phase"] == "CLARIFY"
        assert state["plan"] is None

    asyncio.run(scenario())
