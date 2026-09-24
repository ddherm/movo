# Movo

**VOICE TO PROTOTYPE** — 用语音和文字与编程 Agent 协作，把想法做成可打开、可检查的网页原型。

Movo 是一个单用户、多项目的本机 MVP。它把需求讨论、方案确认、代码构建、进度追踪和语音通话放在同一条项目对话里。手机可以通过私有网络连接运行服务的电脑；项目源码由用户在该电脑上选择存放位置。

## 为什么做

这个项目受到 Muse 的启发。AI 工具变化很快，与其等一个完整产品，不如先做出能运行的 MVP，验证“从想法到原型”的工作流是否真的能提升效率。

语音对话会是日常使用 AI 的重要方式。豆包的电话功能让我觉得，持续对话比一问一答更自然；我想进一步把这种体验与 coding agent 深度耦合：用户可以边说边澄清需求、查看真实进度、回答开发中的问题，并在 Agent 说话时直接插话。9 月 24 日早上，我看到了 OpenAI 的 [Voice agents 文档](https://developers.openai.com/api/docs/guides/voice-agents)和 [GPT-Live 相关发布记录](https://developers.openai.com/api/docs/changelog)，这让我更确信该方向值得探索。官方记录显示 GPT-Live 1 于 **9 月 10 日**开放 API；这里的 9 月 24 日是我看到它的时间，并非发布日期。Movo 当前使用的是自己组合的语音链路，**不依赖 OpenAI Voice agents 或 GPT-Live**。

这仍是验证可行性的作品。它聚焦个人把想法变成前端原型的流程，没有把多用户权限、生产部署和任意软件项目自动交付纳入当前范围。

## 能做什么

- 从文字想法或参考截图开始，用 DeepSeek 澄清需求、生成可确认的方案；配置 Tavily 后可以附上检索来源。
- 方案确认后才启动 OpenHands 编程任务；展示真实事件进度、待答决策、构建与核心交互检查结果。
- 在同一项目里继续用文字或语音提问、答复决策和修改需求，保留版本与会话状态。
- 通话时保持麦克风开启；用户开口可打断 Agent 播报，短暂停顿中的语音会合并成一轮，随后按新对话内容继续。MiniMax 合成语速为 **1.2 倍**。
- 在网页中浏览**运行服务的电脑**上的文件夹，选择已有父文件夹；Movo 自动创建项目专用子文件夹。可以选主盘、外接盘，或通过配置加入其他位置。
- 在 Docker 工作区执行 Agent、构建和交互检查；仅在检查通过后提供静态预览。构建失败可查看原因，并在安全可行时继续修复或重新验收。

流程：想法 → 澄清 → 资料/方案 → 用户确认 → 构建 → 验证 → 可预览。Agent 遇到需要用户决定的问题时暂停等待，避免把推测当作确认。

## 技术栈

| 层 | 实现 |
|---|---|
| 前端 | React 19、Vite 7、Pipecat WebRTC 客户端 |
| 本机 API 与状态 | Python 3.12/3.13、FastAPI、SQLite |
| 需求讨论与编程 | DeepSeek、OpenHands SDK / Agent Server、Docker |
| 语音 | Pipecat、SmallWebRTC、Silero VAD、FunASR / SenseVoiceSmall 识别、MiniMax TTS |
| 可选集成 | Tavily 检索、ntfy 通知、Tailscale Serve 私有 HTTPS |

后端依赖定义在 pyproject.toml，锁定解析在 uv.lock；requirements.txt 是从锁文件导出的、包含 agent / voice / dev 依赖的 pip 清单。前端版本由 frontend/package-lock.json 固定。

## 环境要求

目前完整的 Agent / Docker 构建流程在 **macOS** 上验收过。需要：

- Python 3.12 或 3.13、[uv](https://docs.astral.sh/uv/getting-started/installation/)；
- Node.js 22.14 或更新的 22.x、npm；
- Docker Desktop 已启动；执行真实编程任务时使用；
- DeepSeek API Key；使用语音时还需要 MiniMax API Key 和 Group ID；
- 足够的内盘空间存放 Python 环境、语音模型、Agent 会话和临时构建工作区。

文件夹选择器也能列出 Windows 主机上的盘符与目录，但完整的 Windows 语音和 OpenHands 构建流程尚未实机验收。手机只是访问客户端：所选文件夹始终位于**运行服务的电脑**上，不是手机的本地目录。

## 安装与启动（macOS）

~~~bash
git clone https://github.com/ddherm/movo.git
cd movo

# 在本机原生磁盘创建 Python 环境；外接 exFAT 盘不适合放虚拟环境。
export UV_PROJECT_ENVIRONMENT="$HOME/.voice-programming-companion/.venv"
uv sync --frozen --python python3.12 --extra agent --extra voice --extra dev

cd frontend
npm ci
npm run build
cd ..

cp .env.example .env
# 用本机编辑器填写 .env；至少设置 DEEPSEEK_API_KEY。
"$UV_PROJECT_ENVIRONMENT/bin/python" scripts/check_setup.py

# 需要语音时执行一次，下载本地识别模型。
"$UV_PROJECT_ENVIRONMENT/bin/python" scripts/prepare_voice.py

"$UV_PROJECT_ENVIRONMENT/bin/python" -m uvicorn backend.api:app --host 127.0.0.1 --port 8000
~~~

浏览器打开 http://127.0.0.1:8000/。如果 8000 已被占用，先用 lsof -nP -iTCP:8000 -sTCP:LISTEN 查明占用者，再选择空闲端口；无需重启整个 Docker Desktop。更新前端代码后需重新运行 npm run build；更新 .env 或后端代码后需重启 API 进程。

没有 uv 时，可在 Python 3.12/3.13 的虚拟环境中运行 pip install -r requirements.txt，再执行上述前端构建、配置和启动步骤。requirements.txt 包含完整语音与 Agent 依赖，安装量较大；日常开发优先使用 uv.lock。

## 配置

复制 .env.example 为 .env，在运行服务的电脑上填写。**不要把真实密钥填进网页、语音、截图或 Git。** .env 已被忽略，.env.example 只包含空白占位值。

| 配置 | 作用 |
|---|---|
| DEEPSEEK_API_KEY | 必填；需求讨论和编程 Agent 使用。DEEPSEEK_MODEL、DEEPSEEK_AGENT_MODEL 可按模板调整。 |
| MINIMAX_API_KEY、MINIMAX_GROUP_ID | 启用中文语音回复时必填；语音识别模型由 prepare_voice.py 下载到本机。 |
| TAVILY_API_KEY | 可选；为方案提供可核对的检索来源。 |
| NTFY_TOPIC、NTFY_TOKEN、PUBLIC_PROJECT_URL | 可选；发送不包含项目内容的提醒。topic 应随机且难猜。 |
| VOICE_COMPANION_DATA_DIR | SQLite 状态、截图和本机密钥的位置。模板默认使用用户主盘上的历史兼容目录 ~/.voice-programming-companion。 |
| PROJECT_STORAGE_ROOTS | 可选的额外项目父目录；macOS/Linux 用冒号分隔，Windows 用分号分隔。用户目录和本机磁盘会自动出现在选择器中。 |
| OPENHANDS_STATE_ROOT | 可选的 Agent 会话目录；应使用支持文件锁的原生磁盘，不要指向 exFAT。 |
| AGENT_MAX_SECONDS、AGENT_MAX_ITERATIONS、AGENT_MAX_ACTIONS、AGENT_MAX_COST_USD | 单轮编程任务的观察与限制参数；费用阈值依赖模型供应商上报，仍应在供应商侧设置账户额度。 |

运行 python scripts/check_setup.py 检查必需配置；GET /api/config/status 仅返回布尔状态，不返回密钥。GET /api/voice/status 可查看语音链路缺少的配置或模型。

### 项目放在哪里

新建项目时打开文件夹选择器，浏览运行服务的电脑上的现有**父文件夹**，选中后由 Movo 新建项目子文件夹。无需事先创建空项目目录。主盘和已挂载外接盘都可选；如果目标未出现，可加入 PROJECT_STORAGE_ROOTS。正在构建或等待决策的项目不能切换，以免后台结果写到别的项目。

对 exFAT 等不适合 Agent 锁文件及依赖安装的盘，Movo 在内盘创建临时工作副本，完成 Agent、构建和检查后，先检查目标目录是否被同时修改，再同步结果。即使原项目也在主盘，同一流程仍可运行，只会额外占用临时空间。磁盘断开或冲突时，工作副本会保留并报告位置，不覆盖用户改动。OpenHands 会话和容器内依赖也留在原生磁盘；现有安装默认使用 ~/.voice-programming-companion/agent-state/，以便已有项目续接。

工作副本会跳过 .env*、.npmrc、.git、node_modules 等敏感或可重建文件。依赖这些文件构建的已有项目需要自行调整构建输入。预览只读取构建产物；它不代表已完成全面的安全或产品验收。

## 手机上使用

电脑和手机加入同一 Tailscale 私有网络，保持 Movo 与 Docker Desktop 在电脑上运行。在电脑执行：

~~~bash
tailscale serve --bg 8000
tailscale serve status
~~~

用 serve status 提供的私有 HTTPS 地址在手机打开页面；如需 ntfy 回访链接，将它填入 PUBLIC_PROJECT_URL 后重启服务。选择项目、输入想法或上传截图、确认方案，之后可查看进度和预览，也可进入项目通话。移动网络下 WebRTC 能否建立音频连接受网络路径影响，应在实际手机上测试；当前未配置专用 TURN。电脑睡眠、服务停止或外接盘断开时，相应操作会不可用。

Tailscale Serve 提供 tailnet 内访问。不要用公开 Funnel 链接直接暴露此单用户 MVP。

## 验证与开发

~~~bash
"$UV_PROJECT_ENVIRONMENT/bin/python" -m pytest -q
cd frontend && npm run build
~~~

自动测试覆盖状态机、项目目录、Agent 隔离与同步、敏感文件处理、语音中断和语速。真实设备上的麦克风回声、网络缓冲、截图输入与完整 Agent 项目仍需按自己的环境验收；自动测试通过不等于每台设备上的通话体验相同。

主要目录：backend/core/ 负责状态与编排；backend/integrations/ 连接 Agent、语音、搜索和通知；frontend/ 是网页；scripts/ 提供配置检查与模型准备；tests/ 保存可重复的回归测试。运行数据、私人验收记录和本机密钥都不属于公开仓库。
