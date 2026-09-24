"""Print capability checks without displaying secret values."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ModuleNotFoundError as exc:
    if exc.name != "dotenv":
        raise
    raise SystemExit("缺少 python-dotenv。请先按 README 在项目根目录运行 uv sync，再执行本脚本。") from exc


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def flag(label: str, ready: bool, note: str = "") -> None:
    print(f"{'OK ' if ready else '-- '} {label}{f' — {note}' if note else ''}")


def command_ok(command: list[str]) -> bool:
    try:
        return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    topic = os.getenv("NTFY_TOPIC", "").strip()
    authenticated_ntfy = bool(os.getenv("NTFY_TOKEN", "").strip())
    safe_topic = bool(topic) and (authenticated_ntfy or (len(topic) >= 24 and len(set(topic)) >= 10))
    flag("Python 3.12+", sys.version_info >= (3, 12), sys.version.split()[0])
    flag("DeepSeek 凭证", bool(os.getenv("DEEPSEEK_API_KEY")))
    flag("MiniMax 凭证", bool(os.getenv("MINIMAX_API_KEY") and os.getenv("MINIMAX_GROUP_ID")))
    flag("Tavily 搜索（可选）", bool(os.getenv("TAVILY_API_KEY")))
    flag("ntfy 提醒（可选）", bool(safe_topic and os.getenv("PUBLIC_PROJECT_URL")), "无访问令牌时使用 24 字符以上随机 topic")
    flag("OpenHands SDK", bool(importlib.util.find_spec("openhands")))
    flag("Pipecat", bool(importlib.util.find_spec("pipecat")))
    flag("FunASR 音频特征提取", bool(importlib.util.find_spec("kaldi_native_fbank")))
    flag("Docker daemon", bool(shutil.which("docker")) and command_ok(["docker", "info"]))
    flag("Tailscale", bool(shutil.which("tailscale")) and command_ok(["tailscale", "status"]))
    flag("前端构建产物", (ROOT / "frontend" / "dist" / "index.html").is_file())
    print("检查只验证配置和本机组件；模型、安卓蜂窝语音与生成原型仍需真实联调。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
