"""Optional Pipecat SmallWebRTC voice bridge for the shared project state.

The caller owns ``on_utterance`` and must pass the final transcript through
the same state transitions used by text input. Audio is never stored here.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

UtteranceCallback = Callable[[str, str], Awaitable[str]]
ReplyCallback = Callable[[str], Awaitable[str]]
SpeakCallback = Callable[[str], Awaitable[None]]
_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FUNASR_MODEL = "iic/SenseVoiceSmall"


class _VoiceReplyQueue:
    """Keep state updates ordered while discarding speech from interrupted turns.

    Pipecat cancels the processor handling a normal frame on interruption. A
    project state update must instead finish independently so the next user
    utterance sees the complete conversation. Only its audio becomes stale.
    """

    def __init__(self, reply: ReplyCallback, speak: SpeakCallback):
        self._reply = reply
        self._speak = speak
        self._turn = 0
        self._closed = False
        self._reply_lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def interrupt(self) -> None:
        async with self._output_lock:
            self._turn += 1

    def submit(self, text: str) -> None:
        if self._closed or not text.strip():
            return
        turn = self._turn
        task = asyncio.create_task(self._run(text.strip(), turn))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, text: str, turn: int) -> None:
        # Do not overlap state transitions for utterances in the same call.
        async with self._reply_lock:
            try:
                reply = await self._reply(text)
            except Exception:
                logger.exception("voice state callback failed")
                reply = "处理暂时失败，请稍后重试。"
            async with self._output_lock:
                if not self._closed and turn == self._turn and isinstance(reply, str) and reply.strip():
                    await self._speak(reply.strip()[:600])

    async def close(self) -> None:
        # In-flight state transitions finish, but may no longer emit audio.
        async with self._output_lock:
            self._closed = True


class _VoiceTurnCollector:
    """Combine VAD segments into one user turn before requesting a reply."""

    def __init__(self, replies: _VoiceReplyQueue):
        self._replies = replies
        self._segments: list[str] = []

    async def started(self) -> None:
        # The previous turn was cleared by stopped(). Preserve any current
        # transcript that raced ahead of the high-priority start frame.
        await self._replies.interrupt()

    def transcription(self, text: str) -> None:
        if text.strip():
            self._segments.append(text.strip())

    def stopped(self) -> None:
        text = " ".join(self._segments)
        self._segments.clear()
        self._replies.submit(text)


def _cached_funasr_model() -> str | None:
    """Resolve the downloaded model without making a network request."""
    try:
        from modelscope.hub.snapshot_download import snapshot_download

        path = Path(snapshot_download(_FUNASR_MODEL, local_files_only=True))
    except (ImportError, OSError, RuntimeError, ValueError):
        return None
    if not (path / "model.pt").is_file() or not (path / "config.yaml").is_file():
        return None
    return str(path.resolve())


def voice_status() -> dict[str, Any]:
    """Report prerequisites, without claiming a network/model health check."""
    missing: list[str] = []
    for name in ("MINIMAX_API_KEY", "MINIMAX_GROUP_ID"):
        if not os.getenv(name, "").strip():
            missing.append(name)
    for module, label in (("pipecat", "pipecat-ai[funasr,webrtc,silero]"), ("funasr", "FunASR"), ("torch", "PyTorch"), ("kaldi_native_fbank", "kaldi-native-fbank"), ("aiortc", "aiortc"), ("aiohttp", "aiohttp")):
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(label)
    if "FunASR" not in missing and _cached_funasr_model() is None:
        missing.append("FunASR 模型未准备：运行 python scripts/prepare_voice.py")
    enabled = not missing
    return {"available": enabled, "enabled": enabled, "missing": missing, "checked": "configuration_and_local_model"}


def _voice_imports() -> dict[str, Any]:
    """Load optional and heavy Pipecat modules only for an offered call."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame, UserStartedSpeakingFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineWorker
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.services.funasr.stt import FunASRSTTService
    from pipecat.services.minimax.tts import MiniMaxHttpTTSService
    from pipecat.transcriptions.language import Language
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.request_handler import (
        ConnectionMode,
        IceCandidate,
        SmallWebRTCPatchRequest,
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.turns.user_start import VADUserTurnStartStrategy
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_processor import UserTurnProcessor
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.workers.runner import WorkerRunner

    return locals()


async def _run_voice_session(
    connection: Any,
    project_id: str,
    on_utterance: UtteranceCallback,
    imports: dict[str, Any],
    stt: Any,
    tts: Any,
    aiohttp_session: Any,
) -> None:
    """Run one call. Services are pre-created so an invalid setup fails at offer."""
    Frame = imports["Frame"]
    FrameDirection = imports["FrameDirection"]
    FrameProcessor = imports["FrameProcessor"]
    TranscriptionFrame = imports["TranscriptionFrame"]
    TTSSpeakFrame = imports["TTSSpeakFrame"]
    UserStartedSpeakingFrame = imports["UserStartedSpeakingFrame"]

    class StateBridge(FrameProcessor):
        def __init__(self):
            super().__init__()
            self._replies = _VoiceReplyQueue(
                lambda text: on_utterance(project_id, text),
                lambda reply: self.push_frame(TTSSpeakFrame(text=reply)),
            )
            self._turn = _VoiceTurnCollector(self._replies)

        async def process_frame(self, frame: Frame, direction: FrameDirection):
            await super().process_frame(frame, direction)
            if isinstance(frame, UserStartedSpeakingFrame) and direction == FrameDirection.UPSTREAM:
                # Pipecat emits this once per semantic turn, before its
                # interruption frame. A resumed VAD segment keeps prior text.
                await self._turn.started()
            elif isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
                self._turn.transcription(frame.text)
            await self.push_frame(frame, direction)

        def finish_turn(self) -> None:
            self._turn.stopped()

        async def cleanup(self):
            await self._replies.close()
            await super().cleanup()

    runner = None
    try:
        transport = imports["SmallWebRTCTransport"](
            connection,
            params=imports["TransportParams"](audio_in_enabled=True, audio_out_enabled=True),
        )
        vad = imports["VADProcessor"](vad_analyzer=imports["SileroVADAnalyzer"]())
        # FunASR segments by VAD. The turn processor converts a new speech
        # start into a Pipecat interruption, clearing TTS and outgoing audio.
        turns = imports["UserTurnProcessor"](
            user_turn_strategies=imports["UserTurnStrategies"](
                start=[imports["VADUserTurnStartStrategy"]()],
                stop=[imports["SpeechTimeoutUserTurnStopStrategy"]()],
            )
        )
        bridge = StateBridge()

        @turns.event_handler("on_user_turn_stopped")
        async def on_user_turn_stopped(_processor: Any, _strategy: Any) -> None:
            bridge.finish_turn()

        # Collect transcriptions before the turn processor sees them, so its
        # stop event cannot overtake a final transcription in Pipecat's queues.
        pipeline = imports["Pipeline"]([transport.input(), vad, stt, bridge, turns, tts, transport.output()])
        worker = imports["PipelineWorker"](pipeline)
        runner = imports["WorkerRunner"](handle_sigint=False)
        await runner.add_workers(worker)

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(_transport: Any, _client: Any) -> None:
            await runner.cancel()

        await runner.run()
    except Exception:
        logger.exception("voice session failed for project %s", project_id)
    finally:
        await aiohttp_session.close()


def create_voice_router(on_utterance: UtteranceCallback) -> APIRouter:
    """Create ``/voice/status`` and ``/voice/offer`` POST/PATCH routes.

    Include with ``prefix='/api'`` to match the SmallWebRTC browser transport.
    The offer's requestData/request_data must contain ``project_id``.
    """
    handler: Any | None = None
    tasks: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def lifespan(_router: APIRouter):
        yield
        for task in tuple(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if handler is not None:
            await handler.close()

    router = APIRouter(prefix="/voice", lifespan=lifespan)

    @router.get("/status")
    async def status() -> dict[str, Any]:
        return voice_status()

    @router.post("/offer")
    async def offer(payload: dict[str, Any]) -> dict[str, str]:
        nonlocal handler
        started = monotonic()
        status = voice_status()
        if not status["available"]:
            raise HTTPException(503, detail={"reason": "voice unavailable", "missing": status["missing"]})
        data = payload.get("request_data", payload.get("requestData"))
        project_id = data.get("project_id") if isinstance(data, dict) else None
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise HTTPException(400, detail="requestData.project_id is required")
        if not isinstance(payload.get("sdp"), str) or payload.get("type") != "offer":
            raise HTTPException(400, detail="invalid WebRTC offer")
        model_path = _cached_funasr_model()
        if model_path is None:
            raise HTTPException(503, detail="FunASR model is not prepared")

        # The handler swallows callback exceptions, so load model/services before
        # handing it the SDP. An unavailable model must produce an HTTP error.
        try:
            import aiohttp

            imports = _voice_imports()
            stt = await asyncio.to_thread(
                imports["FunASRSTTService"],
                settings=imports["FunASRSTTService"].Settings(
                    model=model_path, language=imports["Language"].ZH
                ),
            )
            session = aiohttp.ClientSession()
            tts = imports["MiniMaxHttpTTSService"](
                api_key=os.environ["MINIMAX_API_KEY"],
                group_id=os.environ["MINIMAX_GROUP_ID"],
                aiohttp_session=session,
                base_url=os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.chat/v1/t2a_v2"),
                settings=imports["MiniMaxHttpTTSService"].Settings(
                    language=imports["Language"].ZH,
                    voice=os.getenv("MINIMAX_VOICE_ID", "Calm_Woman"),
                    model=os.getenv("MINIMAX_TTS_MODEL", "speech-2.8-turbo"),
                    speed=1.2,
                ),
            )
            logger.info("voice services prepared in %.1fs", monotonic() - started)
        except Exception as exc:
            if "session" in locals():
                await session.close()
            logger.warning("voice prerequisite/model setup failed: %s", type(exc).__name__)
            raise HTTPException(503, detail=f"voice setup unavailable: {type(exc).__name__}") from exc

        if handler is None:
            handler = imports["SmallWebRTCRequestHandler"](
                connection_mode=imports["ConnectionMode"].SINGLE
            )
        request = imports["SmallWebRTCRequest"](
            sdp=payload["sdp"],
            type="offer",
            pc_id=payload.get("pc_id"),
            restart_pc=payload.get("restart_pc"),
            request_data=data,
        )
        used = False

        async def start(connection: Any) -> None:
            nonlocal used
            used = True
            task = asyncio.create_task(
                _run_voice_session(connection, project_id, on_utterance, imports, stt, tts, session)
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        try:
            answer = await handler.handle_web_request(request, start)
            if not answer:
                raise HTTPException(503, detail="WebRTC connection returned no answer")
            logger.info("voice SDP answer ready in %.1fs", monotonic() - started)
            return answer
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("WebRTC offer failed: %s", type(exc).__name__)
            raise HTTPException(503, detail=f"WebRTC setup failed: {type(exc).__name__}") from exc
        finally:
            if not used:
                await session.close()

    @router.patch("/offer")
    async def patch_offer(payload: dict[str, Any]) -> dict[str, str]:
        if handler is None:
            raise HTTPException(404, detail="WebRTC session not found")
        pc_id = payload.get("pc_id")
        candidates = payload.get("candidates", [])
        if not isinstance(pc_id, str) or not isinstance(candidates, list):
            raise HTTPException(400, detail="invalid ICE candidate patch")
        try:
            from pipecat.transports.smallwebrtc.request_handler import IceCandidate, SmallWebRTCPatchRequest

            normalized = [
                IceCandidate(
                    candidate=item["candidate"],
                    sdp_mid=item.get("sdp_mid", item.get("sdpMid")),
                    sdp_mline_index=item.get("sdp_mline_index", item.get("sdpMLineIndex")),
                )
                for item in candidates
            ]
            await handler.handle_patch_request(SmallWebRTCPatchRequest(pc_id=pc_id, candidates=normalized))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, detail="invalid ICE candidate patch") from exc
        return {"status": "success"}

    return router
