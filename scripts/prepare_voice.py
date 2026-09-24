"""Download the FunASR model before accepting browser calls.

Run once while the Mac has internet access. The call endpoint only uses this
local snapshot and never waits on ModelScope during WebRTC negotiation.
"""

from __future__ import annotations

from pathlib import Path

from modelscope.hub.snapshot_download import snapshot_download


def main() -> None:
    try:
        model_dir = Path(snapshot_download("iic/SenseVoiceSmall", local_files_only=True))
    except (OSError, RuntimeError, ValueError):
        model_dir = None
    if model_dir is None or not (model_dir / "model.pt").is_file() or not (model_dir / "config.yaml").is_file():
        print("正在下载 FunASR SenseVoiceSmall 模型，首次运行可能需要几分钟…")
        model_dir = Path(snapshot_download("iic/SenseVoiceSmall"))
    if not (model_dir / "model.pt").is_file() or not (model_dir / "config.yaml").is_file():
        raise RuntimeError("FunASR 模型下载不完整，请重新运行此命令。")
    print("FunASR 模型已准备好。")


if __name__ == "__main__":
    main()
