"""Voice turn regressions without microphone, model, or provider requests."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from backend.integrations import voice


def test_interrupted_reply_finishes_state_update_but_only_new_turn_speaks():
    async def scenario():
        old_started = asyncio.Event()
        release_old = asyncio.Event()
        spoken = []
        handled = []

        async def reply(text):
            handled.append(text)
            if text == "旧问题":
                old_started.set()
                await release_old.wait()
            return f"回答：{text}"

        async def speak(text):
            spoken.append(text)

        queue = voice._VoiceReplyQueue(reply, speak)
        queue.submit("旧问题")
        await asyncio.wait_for(old_started.wait(), 1)
        await queue.interrupt()
        queue.submit("新问题")
        release_old.set()
        await asyncio.wait_for(asyncio.gather(*tuple(queue._tasks)), 1)

        assert handled == ["旧问题", "新问题"]
        assert spoken == ["回答：新问题"]
        await queue.close()
        queue.submit("挂断后")
        assert handled == ["旧问题", "新问题"]

    asyncio.run(scenario())


def test_short_pause_segments_are_answered_as_one_turn():
    async def scenario():
        handled = []
        spoken = []

        async def reply(text):
            handled.append(text)
            return f"收到：{text}"

        async def speak(text):
            spoken.append(text)

        queue = voice._VoiceReplyQueue(reply, speak)
        turns = voice._VoiceTurnCollector(queue)
        await turns.started()
        turns.transcription("先做一个页面")
        turns.transcription("再加搜索框")
        assert handled == []
        turns.stopped()
        await asyncio.wait_for(asyncio.gather(*tuple(queue._tasks)), 1)

        assert handled == ["先做一个页面 再加搜索框"]
        assert spoken == ["收到：先做一个页面 再加搜索框"]
        await queue.close()

    asyncio.run(scenario())


def test_late_start_signal_preserves_transcript_already_seen():
    async def scenario():
        handled = []

        async def reply(text):
            handled.append(text)
            return text

        queue = voice._VoiceReplyQueue(reply, lambda _text: asyncio.sleep(0))
        turns = voice._VoiceTurnCollector(queue)
        turns.transcription("先说的内容")
        await turns.started()
        turns.transcription("后说的内容")
        turns.stopped()
        await asyncio.wait_for(asyncio.gather(*tuple(queue._tasks)), 1)
        assert handled == ["先说的内容 后说的内容"]
        await queue.close()

    asyncio.run(scenario())


def test_voice_offer_uses_minimax_native_speed_1_2(monkeypatch):
    captured = {}

    class Settings:
        def __init__(self, **values):
            self.__dict__.update(values)

    class STT:
        def __init__(self, *, settings):
            captured["stt"] = settings

    class TTS:
        def __init__(self, *, settings, **_kwargs):
            captured["tts"] = settings

    STT.Settings = Settings
    TTS.Settings = Settings

    class Session:
        async def close(self):
            captured["session_closed"] = True

    class Handler:
        def __init__(self, **_kwargs):
            pass

        async def handle_web_request(self, _request, _start):
            return {"sdp": "answer", "type": "answer"}

    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(ClientSession=Session))
    monkeypatch.setattr(voice, "voice_status", lambda: {"available": True, "missing": []})
    monkeypatch.setattr(voice, "_cached_funasr_model", lambda: "/cached/funasr")
    monkeypatch.setattr(voice, "_voice_imports", lambda: {
        "FunASRSTTService": STT,
        "MiniMaxHttpTTSService": TTS,
        "Language": SimpleNamespace(ZH="zh"),
        "SmallWebRTCRequestHandler": Handler,
        "ConnectionMode": SimpleNamespace(SINGLE="single"),
        "SmallWebRTCRequest": lambda **values: values,
    })
    monkeypatch.setenv("MINIMAX_API_KEY", "test-only")
    monkeypatch.setenv("MINIMAX_GROUP_ID", "test-only")

    router = voice.create_voice_router(lambda _project, _text: asyncio.sleep(0))
    offer = next(route.endpoint for route in router.routes if route.path == "/voice/offer" and "POST" in route.methods)

    async def scenario():
        answer = await offer({"sdp": "offer", "type": "offer", "requestData": {"project_id": "project_1"}})
        assert answer["type"] == "answer"

    asyncio.run(scenario())
    assert captured["tts"].speed == 1.2
    assert captured["session_closed"] is True
