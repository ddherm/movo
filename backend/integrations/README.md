# External integrations

These adapters are optional. Missing credentials, Python packages, Docker or
model files return an explicit unavailable/error state. Do not interpret an
empty Tavily result as an unavailable search: `search()` raises
`IntegrationUnavailable` for service failures.

## Interfaces

- `await run_build(snapshot, on_event, on_question)` executes an approved plan
  in an OpenHands SDK `DockerWorkspace`. `on_event(kind, summary, detail)` and
  `on_question(body)` are async callbacks. `on_question` returns the saved
  question ID. The result includes `status`, `conversation_id`, `source_dir`,
  `preview_dir`, `verification` and `reason`.
- `await verify_saved_build(snapshot)` checks an existing source directory in a
  fresh Docker workspace without calling an LLM. It returns the same result
  shape and needs Docker and the SDK, but does not need `DEEPSEEK_API_KEY`.
- `await ask_progress(snapshot, question)` asks the currently running agent and
  prefixes its answer with the core state's latest recorded phase/event. It
  reports when no live agent is attached.
- `await search(query)` returns Tavily `{title,url,snippet}` hits.
- `await send_notification(kind, project_url)` sends a neutral ntfy message and
  returns whether delivery succeeded. Recognized kinds: `plan_review`,
  `decision`, `waiting_decision`, `ready`, `failed`.
- `create_voice_router(on_utterance)` creates `/voice/status` and POST/PATCH
  `/voice/offer`; mount it with `/api` as the app prefix. The browser's Pipecat
  offer sends `requestData: {project_id}`. The adapter combines final FunASR
  segments for one user turn, calls
  `await on_utterance(project_id, text) -> reply_text`, then speaks the returned
  text through MiniMax at 1.2x speed. VAD turn start interrupts old TTS and
  output audio; state callbacks finish in order, but interrupted replies are
  not spoken. This callback should use the text path's
  state machine. `/voice/status` returns `enabled`, `available`, `missing`, and
  `checked: configuration_and_local_model`; a true `enabled` does not imply
  provider credentials have been tested. Run `python scripts/prepare_voice.py`
  before the first call. The offer loads the cached FunASR snapshot by local
  path and returns 503 on missing dependencies, configuration or model setup.

## OpenHands runtime

The verified official image tag is
`ghcr.io/openhands/agent-server:1.49.5-python` (GHCR manifest includes both
amd64 and arm64). SDK packages should be pinned together to the 1.49 release
family: `openhands-sdk==1.49.4`, `openhands-tools==1.49.4`,
`openhands-workspace==1.49.2`. Python >=3.12 is required. Every selected
project is copied into a private native-disk working tree before Docker starts.
Only that working tree is mounted at `/workspace`; its changes are checked for
conflicts and synced back to the selected project after the run. Agent sessions,
events, locks, and `node_modules` stay in a separate native-disk directory under
`~/.voice-programming-companion/agent-state/` by default. With
`VOICE_COMPANION_DATA_DIR` configured, the local encryption key is stored in
its `agent-keys/` directory, outside both mounts. A provided
`agent_conversation_id` is attached strictly; if server persistence cannot
load it, the adapter fails rather than silently creating a replacement.
`agent_session` emits the conversation ID immediately, so core can persist it.
The SDK's stock DockerWorkspace publishes its port on every host interface.
This adapter's pinned SDK subclass binds it to `127.0.0.1` on the host.
The pinned `1.49.5-python` arm64 image was smoke tested with health checks,
container command execution, a project bind mount, and cleanup. A complete
DeepSeek Agent run and cross restart conversation recovery still require
provider credentials for a live integration test.

`AGENT_MAX_ITERATIONS` (default 35), `AGENT_MAX_ACTIONS` (80), and
`AGENT_MAX_SECONDS` (900) constrain each run. `AGENT_MAX_COST_USD` (3) is a
**soft** observed USD threshold: after SDK action events the adapter reads
remote conversation metrics and pauses when the threshold is reached. The
remote `RemoteConversation` API has no server-enforced budget parameter;
callbacks and provider billing may lag a call, so a hard dollar ceiling is not
guaranteed. A provider account limit or a metered proxy is needed for one.

The agent writes `decision-request.json` only when a product decision blocks
work. Its format is:

```json
{
  "trigger": "why a decision is needed",
  "question": "one focused question",
  "impact": "what changes depending on the choice",
  "options": [
    {"id": "a", "label": "First option", "impact": "tradeoff"},
    {"id": "b", "label": "Second option", "impact": "tradeoff"}
  ],
  "recommended_option_id": "a",
  "blocking_scope": "build"
}
```

The adapter checks `npm run build`, `/workspace/dist/index.html` and
`npm run test:interaction` in Docker. The preview is static and read only at
`dist/`; local script/link assets must use relative paths and exist under
`dist` so a nested preview URL works. This is a build/interaction command
check, not a browser visual QA.
No `ready` result is returned without both scripts succeeding.
When OpenHands reports the structured `MaxIterationsReached` error, the
adapter attempts these checks before marking the run failed. A timeout,
another stop reason, a pending product decision, or any failed check keeps the
run from becoming ready.

## Environment

| Service | Required | Optional defaults |
|---|---|---|
| OpenHands/DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_AGENT_MODEL=deepseek/deepseek-flash`, `DEEPSEEK_BASE_URL=https://api.deepseek.com`, `OPENHANDS_SERVER_IMAGE=ghcr.io/openhands/agent-server:1.49.5-python`, `AGENT_MAX_ITERATIONS=35`, `AGENT_MAX_ACTIONS=80`, `AGENT_MAX_SECONDS=900`, `AGENT_MAX_COST_USD=3` |
| Tavily | `TAVILY_API_KEY` | none |
| ntfy | `NTFY_TOPIC` | `NTFY_BASE_URL=https://ntfy.sh`, `NTFY_TOKEN` |
| Voice | `MINIMAX_API_KEY`, `MINIMAX_GROUP_ID` | `MINIMAX_BASE_URL=https://api.minimaxi.chat/v1/t2a_v2`, `MINIMAX_VOICE_ID=Calm_Woman`, `MINIMAX_TTS_MODEL=speech-2.8-turbo` |

Without `NTFY_TOKEN`, `NTFY_TOPIC` must be a random name of at least 24
characters with at least 10 distinct characters; short predictable public
topics are rejected. A short topic is allowed with a configured token.

For voice install `pipecat-ai[funasr,webrtc,silero]`, `torch`, and
`kaldi-native-fbank`. FunASR needs a compatible fbank backend to transcribe
audio; its Pipecat extra does not install one. Run `scripts/prepare_voice.py`
once to download SenseVoiceSmall before starting calls. MiniMax uses Pipecat's
base `aiohttp` dependency. TURN/ICE relay may be needed beyond direct LAN access.

## API sources

- [OpenHands Docker example](https://github.com/OpenHands/software-agent-sdk/blob/main/examples/02_remote_agent_server/02_convo_with_docker_sandboxed_server.py), [RemoteConversation source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/conversation/impl/remote_conversation.py), [DockerWorkspace source](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-workspace/openhands/workspace/docker/workspace.py)
- [Pipecat SmallWebRTC request handler](https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/transports/smallwebrtc/request_handler.py), [FunASR](https://docs.pipecat.ai/api-reference/server/services/stt/funasr), [MiniMax HTTP TTS](https://docs.pipecat.ai/api-reference/server/services/tts/minimax)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search), [ntfy publishing](https://docs.ntfy.sh/publish/)
