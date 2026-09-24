"""Diagnostic Agent uses evidence, keeps secrets private and only proposes fixes."""

from __future__ import annotations

import asyncio

from backend.core import diagnostics


def _snapshot(tmp_path, error: str) -> dict:
    return {
        "phase": "FAILED",
        "source_dir": str(tmp_path),
        "last_error": error,
        "plan": {"status": "approved"},
        "pending_question": None,
        "verification": None,
        "events": [{"kind": "error", "summary": error}],
    }


def test_agent_server_500_explains_observed_failure_without_guessing(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    error = "HTTPStatusError: Server error '500 Internal Server Error' for url 'http://127.0.0.1:33941/api/conversations'"
    result = asyncio.run(diagnostics.diagnose(_snapshot(tmp_path, error), "为什么出错了"))

    assert "Agent Server" in result["reply"]
    assert "500" in result["reply"]
    assert "无法判定" in result["reply"]
    assert result["action"] == "none"
    assert "127.0.0.1" not in result["reply"]
    assert result["diagnostic_artifacts"]["dist_entry_exists"] is False


def test_recorded_reproduction_is_used_for_the_original_500(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    snapshot = _snapshot(tmp_path, "HTTPStatusError: 500 at http://127.0.0.1:33941/api/conversations")
    snapshot["events"].insert(0, {
        "kind": "diagnostic_finding",
        "summary": "相同配置在 exFAT 上复现会话锁文件错误，内盘状态目录创建成功",
    })

    result = asyncio.run(diagnostics.diagnose(snapshot, "为什么出错了"))

    assert "exFAT" in result["reply"]
    assert "内盘状态目录" in result["reply"]
    assert result["action"] == "none"


def test_unrelated_provider_500_is_not_called_agent_server_creation(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    snapshot = _snapshot(tmp_path, "HTTPStatusError: 500 at https://api.example.invalid/chat/completions")

    result = asyncio.run(diagnostics.diagnose(snapshot, "为什么出错了"))

    assert "Agent Server 创建会话" not in result["reply"]
    assert result["action"] == "none"


def test_saved_artifact_can_be_proposed_for_verification(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")

    result = asyncio.run(diagnostics.diagnose(_snapshot(tmp_path, "ConversationRunError: MaxIterationsReached"), "为什么失败了"))

    assert result["action"] == "verify_saved"
    assert result["diagnostic_artifacts"]["dist_entry_exists"] is True


def test_unapproved_project_never_proposes_automatic_recovery(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<h1>ok</h1>", encoding="utf-8")
    snapshot = _snapshot(tmp_path, "ConversationRunError: MaxIterationsReached")
    snapshot["plan"] = {"status": "draft"}

    result = asyncio.run(diagnostics.diagnose(snapshot, "能恢复吗"))

    assert result["action"] == "none"


def test_build_check_failure_with_source_proposes_repair(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")
    snapshot = _snapshot(tmp_path, "构建产物或核心交互检查未通过。")
    snapshot["verification"] = {"build_status": "failed", "interaction_status": "not_run", "details": "Build command returned nonzero."}

    result = asyncio.run(diagnostics.diagnose(snapshot, "能自动修好吗"))

    assert result["action"] == "repair_build"
    assert result["diagnostic_artifacts"]["package_json_exists"] is True


def test_failed_interaction_after_iteration_limit_proposes_repair_not_repeat_verification(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / "package.json").write_text('{}', encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text('<h1>saved</h1>', encoding="utf-8")
    snapshot = _snapshot(tmp_path, "ConversationRunError: MaxIterationsReached")
    snapshot["verification"] = {"build_status": "passed", "interaction_status": "failed", "details": "touch control did not respond"}

    result = asyncio.run(diagnostics.diagnose(snapshot, "能修好吗"))

    assert result["action"] == "repair_build"
    assert "核心交互" in result["reply"]


def test_diagnosis_sees_empty_entrypoint_and_only_permitted_recovery_actions(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{}', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_bytes(b"")
    snapshot = _snapshot(tmp_path, "interaction check failed")
    snapshot["verification"] = {"build_status": "passed", "interaction_status": "failed", "details": "entrypoint timeout"}
    observed = {}

    async def model(case):
        observed.update(case)
        return {"reply": "入口文件为空，交互启动不了。我会修复入口并重验。", "action": "repair_build", "rationale": "文件大小和交互失败相符", "confidence": 0.8}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    result = asyncio.run(diagnostics.diagnose(snapshot, "为什么失败了？"))

    assert observed["diagnostic_artifacts"]["empty_source_files"] == ["src/main.js"]
    assert observed["permitted_actions"] == ["none", "repair_build"]
    assert result["action"] == "repair_build"
    assert "入口文件为空" in result["reply"]


def test_model_uncertainty_does_not_suppress_safe_source_inspection(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{}', encoding="utf-8")
    snapshot = _snapshot(tmp_path, "interaction check failed")
    snapshot["verification"] = {"build_status": "passed", "interaction_status": "failed", "details": "core interaction timed out"}

    async def model(_case):
        return {"reply": "我还不知道具体哪行有错。", "action": "none", "rationale": "需要看代码", "confidence": 0.5}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    result = asyncio.run(diagnostics.diagnose(snapshot, "能修好吗？"))

    assert result["action"] == "repair_build"
    assert "查看失败输出和现有代码" in result["reply"]


def test_model_cannot_propose_source_repair_for_agent_server_500(tmp_path, monkeypatch):
    calls = []

    async def model(_case):
        calls.append(_case)
        return {"reply": "源文件一定坏了，立即改源码。", "action": "repair_build", "rationale": "我猜的", "confidence": 1}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    snapshot = _snapshot(tmp_path, "HTTPStatusError: 500 at http://127.0.0.1:33941/api/conversations")

    result = asyncio.run(diagnostics.diagnose(snapshot, "为什么出错了"))

    assert result["action"] == "none"
    assert calls == []
    assert "源文件一定坏了" not in result["reply"]


def test_model_with_server_detail_still_cannot_approve_wrong_repair(tmp_path, monkeypatch):
    async def model(_case):
        assert _case["failure_location"] == "agent_server_conversation_create"
        return {"reply": "我将修复项目源码。", "action": "repair_build", "rationale": "猜测", "confidence": 1}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    snapshot = _snapshot(tmp_path, "HTTPStatusError: 500 at http://127.0.0.1:33941/api/conversations")

    result = asyncio.run(diagnostics.diagnose(snapshot, "为什么出错了", evidence={"server_error_detail": "database initialization failed"}))

    assert result["action"] == "none"
    assert "我将修复项目源码" not in result["reply"]


def test_sanitized_server_hint_in_last_error_reaches_model(tmp_path, monkeypatch):
    captured = {}

    async def model(case):
        captured["case"] = case
        return {"reply": "服务端提示数据库初始化失败，接下来应核查该初始化步骤。", "action": "none", "rationale": "服务端异常提示", "confidence": 0.8}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    snapshot = _snapshot(tmp_path, "HTTPStatusError: 500 /api/conversations；服务端异常：database initialization failed")

    result = asyncio.run(diagnostics.diagnose(snapshot, "为什么出错了"))

    assert captured["case"]["server_error_detail"] == "database initialization failed"
    assert result["action"] == "none"
    assert "数据库初始化失败" in result["reply"]


def test_generic_server_hint_does_not_become_root_cause_evidence(tmp_path, monkeypatch):
    async def model(_case):
        raise AssertionError("Generic HTTP text is not server-side diagnostic evidence")

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    snapshot = _snapshot(tmp_path, "HTTPStatusError: 500 /api/conversations；服务端异常：Internal Server Error")

    result = asyncio.run(diagnostics.diagnose(snapshot, "为什么出错了"))

    assert result["action"] == "none"


def test_model_input_omits_credentials_urls_paths_and_source_contents(tmp_path, monkeypatch):
    captured = {}
    token = "sk-FAKE-DIAGNOSTIC-TOKEN-2026"
    monkeypatch.setenv("DEEPSEEK_API_KEY", token)
    (tmp_path / "package.json").write_text(f'{{"secret":"{token}"}}', encoding="utf-8")

    async def model(case):
        captured["case"] = case
        return {"reply": f"见 {tmp_path}/package.json {token}", "action": "none", "rationale": "可继续排查", "confidence": 0.5}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    snapshot = _snapshot(tmp_path, f"HTTPStatusError: 500 at http://127.0.0.1:33941/api/conversations {token}")
    result = asyncio.run(diagnostics.diagnose(
        snapshot, "为何失败？", evidence={"server_error_detail": f"内部错误 {token} at {tmp_path}"}
    ))
    serialized = str(captured["case"])

    assert token not in serialized
    assert str(tmp_path) not in serialized
    assert "127.0.0.1" not in serialized
    assert token not in result["reply"]
    assert str(tmp_path) not in result["reply"]


def test_model_sees_bounded_project_goal_and_approved_plan(tmp_path, monkeypatch):
    captured = {}

    async def model(case):
        captured["context"] = case["project_context"]
        return {"reply": "目标是帮助学生安排学习。", "action": "none", "rationale": "已确认方案", "confidence": 0.9}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    snapshot = _snapshot(tmp_path, "")
    snapshot["phase"] = "READY"
    snapshot["idea"] = "给学生做学习计划页"
    snapshot["spec_version"] = 3
    snapshot["plan"] = {
        "status": "approved", "goal": "帮助学生安排学习", "audience": "学生",
        "flow": "输入目标后查看计划", "scope": "单页网页", "acceptance": "手机上输入一次目标",
        "secret_extra": "should not leave the server",
    }

    result = asyncio.run(diagnostics.diagnose(snapshot, "目前方案是什么？"))

    assert result["reply"] == "目标是帮助学生安排学习。"
    assert captured["context"]["idea"] == "给学生做学习计划页"
    assert captured["context"]["plan"]["goal"] == "帮助学生安排学习"
    assert captured["context"]["plan_status"] == "approved"
    assert "secret_extra" not in str(captured["context"])


def test_invalid_model_result_falls_back_without_model_call_in_test(tmp_path, monkeypatch):
    async def model(_case):
        return {"action": "retry_build"}

    monkeypatch.setattr(diagnostics, "_ask_model", model)
    result = asyncio.run(diagnostics.diagnose(_snapshot(tmp_path, "Unknown failure"), "为什么？"))

    assert result["action"] == "none"
    assert "当前项目处于失败状态" in result["reply"]
