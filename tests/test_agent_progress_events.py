from types import SimpleNamespace

from backend.integrations.openhands import _agent_event_summary


def sdk_event(name, **fields):
    event = type(name, (), {})()
    for key, value in fields.items():
        setattr(event, key, value)
    return event


def test_file_action_labels_hide_paths_and_content():
    private_path = "/workspace/private/payment-api-key.txt"
    event = sdk_event(
        "ActionEvent",
        tool_name="file_editor",
        action=SimpleNamespace(command="create", path=private_path, file_text="secret-value"),
    )
    summary = _agent_event_summary(event)

    assert summary == "Agent 正在创建项目文件"
    assert private_path not in summary
    assert "secret-value" not in summary
    event.action.command = "view"
    assert _agent_event_summary(event) == "Agent 正在查看项目文件"


def test_terminal_actions_report_only_allowlisted_activity():
    event = sdk_event("ActionEvent", tool_name="terminal", action=SimpleNamespace(command="npm run build"))
    assert _agent_event_summary(event) == "Agent 正在执行构建检查"
    event.action.command = "npm run test:interaction"
    assert _agent_event_summary(event) == "Agent 正在运行项目测试"
    event.action.command = "npm ci --no-audit"
    assert _agent_event_summary(event) == "Agent 正在安装项目依赖"
    event.action.command = "cat /workspace/.env && echo private-token"
    assert _agent_event_summary(event) == "Agent 正在执行终端操作"


def test_observations_and_unknown_tools_are_safe():
    assert _agent_event_summary(sdk_event("ObservationEvent", tool_name="file_editor")) == "项目文件操作已返回结果"
    assert _agent_event_summary(sdk_event("ObservationEvent", tool_name="terminal")) == "终端操作已返回结果"
    unknown = sdk_event("ActionEvent", tool_name="secret-tool-name", action=None)
    assert _agent_event_summary(unknown) == "Agent 正在执行开发操作"
