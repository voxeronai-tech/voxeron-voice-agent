import os
import io
import json
import asyncio
import logging
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List

import webrtcvad
import httpx
from openai import OpenAI

logger = logging.getLogger("src.agent.voice_agent")


def _normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    # cheap normalize
    for ch in [".", ",", "!", "?", ":", ";", "\"", "'", "(", ")", "[", "]"]:
        s = s.replace(ch, "")
    s = " ".join(s.split())
    return s


def _jaccard_similarity(a: str, b: str) -> float:
    a = _normalize_text(a)
    b = _normalize_text(b)
    if not a or not b:
        return 0.0
    sa = set(a.split())
    sb = set(b.split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def pcm16_to_wav_bytes(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw PCM16LE mono into a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return buf.getvalue()


@dataclass
class SessionState:
    sample_rate: int = 16000
    vad: webrtcvad.Vad = field(default_factory=lambda: webrtcvad.Vad(2))  # 0..3, 2 is good
    # incoming PCM buffer (raw int16le)
    pcm_buffer: bytearray = field(default_factory=bytearray)

    # VAD framing
    frame_ms: int = 20
    frame_bytes: int = 640  # 20ms @ 16k mono int16
    speeching: bool = False
    speech_frames: List[bytes] = field(default_factory=list)

    # VAD tuning
    min_speech_ms: int = 240         # need at least 240ms speech before STT
    end_silence_ms: int = 420        # stop after ~420ms silence
    max_utterance_ms: int = 6000     # safety cap

    speech_ms: int = 0
    silence_ms: int = 0

    # Agent playback hints (client reports start/end)
    agent_playing: bool = False
    last_agent_text: str = ""
    last_agent_play_ts: float = 0.0

    # Conversation memory
    history: List[Dict[str, str]] = field(default_factory=list)
    language: str = "english"


class VoiceAgent:
    """
    Hard-mode demo agent:
    - Client streams raw PCM frames (int16le @ 16kHz)
    - Server runs VAD and only sends speech segments to STT as WAV
    - Anti-ghost: drop transcripts that match recent agent speech
    - Barge-in: when speech starts, clear client audio queue immediately
    """

    def __init__(self):
        self.openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.http = httpx.AsyncClient(timeout=30.0)

        self.stt_model = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")
        self.chat_model = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")

        # ElevenLabs
        self.eleven_api_key = os.getenv("ELEVENLABS_API_KEY", "")
        self.eleven_model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
        self.voice_english = os.getenv("ELEVENLABS_VOICE_ENGLISH") or os.getenv("ELEVENLABS_VOICE_ID")
        self.voice_dutch = os.getenv("ELEVENLABS_VOICE_DUTCH")
        self.voice_hindi = os.getenv("ELEVENLABS_VOICE_HINDI")

        # session_id -> state
        self.sessions: Dict[str, SessionState] = {}
        # session_id -> websocket
        self.ws: Dict[str, Any] = {}

        logger.info("VoiceAgent initialized (STT=%s CHAT=%s)", self.stt_model, self.chat_model)

    # ---------------------------
    # Websocket lifecycle
    # ---------------------------
    async def on_connect(self, session_id: str, ws: Any, sample_rate: int = 16000):
        self.ws[session_id] = ws
        st = SessionState(sample_rate=sample_rate)

        # Compute frame_bytes based on sample_rate (must stay 20ms)
        st.frame_bytes = int((sample_rate * (st.frame_ms / 1000.0)) * 2)

        # VAD works best at 8k/16k/32k/48k; we use 16k from client.
        if sample_rate != 16000:
            logger.warning("[%s] sample_rate=%s (recommended 16000)", session_id, sample_rate)

        self.sessions[session_id] = st

        # Greeting (text + audio)
        greeting = (
            "Namaste! Welcome to Taj Mahal. "
            "You can order in English, Nederlands, or हिंदी. "
            "What would you like today?"
        )
        await self._send_agent(session_id, greeting)

    async def on_disconnect(self, session_id: str):
        self.ws.pop(session_id, None)
        self.sessions.pop(session_id, None)

    async def on_client_event(self, session_id: str, data: Dict[str, Any]):
        st = self.sessions.get(session_id)
        if not st:
            return

        t = data.get("type")
        if t == "playback":
            state = data.get("state")
            if state == "start":
                st.agent_playing = True
                st.last_agent_play_ts = asyncio.get_event_loop().time()
            elif state == "end":
                st.agent_playing = False
            return

        if t == "barge_in":
            # client already stopped playback; we can optionally clear pending TTS queue
            await self._send_json(session_id, {"type": "clear_audio_queue"})
            return

        if t == "client_mode":
            # currently informational
            return

    # ---------------------------
    # Audio ingestion
    # ---------------------------
    async def on_audio_bytes(self, session_id: str, chunk: bytes):
        st = self.sessions.get(session_id)
        if not st:
            return
        if not chunk:
            return

        # Append to raw PCM buffer (client sends int16le)
        st.pcm_buffer.extend(chunk)

        # Process frames
        while len(st.pcm_buffer) >= st.frame_bytes:
            frame = bytes(st.pcm_buffer[:st.frame_bytes])
            del st.pcm_buffer[:st.frame_bytes]
            await self._process_vad_frame(session_id, st, frame)

    async def _process_vad_frame(self, session_id: str, st: SessionState, frame: bytes):
        # VAD expects 16-bit PCM mono at supported rates, frame must be 10/20/30ms.
        is_speech = False
        try:
            is_speech = st.vad.is_speech(frame, st.sample_rate)
        except Exception:
            # if something is off, ignore this frame
            return

        if is_speech:
            st.silence_ms = 0
            st.speech_ms += st.frame_ms

            if not st.speeching:
                st.speeching = True
                st.speech_frames = []
                st.speech_ms = st.frame_ms
                st.silence_ms = 0

                # HARD-MODE barge-in:
                # As soon as we detect user speech, kill agent playback on client
                await self._send_json(session_id, {"type": "clear_audio_queue"})

            st.speech_frames.append(frame)

            # safety cap
            if st.speech_ms >= st.max_utterance_ms:
                await self._finalize_utterance(session_id, st)

        else:
            if st.speeching:
                st.silence_ms += st.frame_ms

                # still keep a tiny tail (optional) -> helps last phonemes
                # but don't add too much silence
                if st.silence_ms <= 120:
                    st.speech_frames.append(frame)

                if st.silence_ms >= st.end_silence_ms:
                    await self._finalize_utterance(session_id, st)

    async def _finalize_utterance(self, session_id: str, st: SessionState):
        st.speeching = False

        total_ms = st.speech_ms
        st.speech_ms = 0
        st.silence_ms = 0

        if total_ms < st.min_speech_ms:
            st.speech_frames = []
            return

        pcm16 = b"".join(st.speech_frames)
        st.speech_frames = []

        # Convert to WAV and transcribe
        wav_bytes = pcm16_to_wav_bytes(pcm16, sample_rate=st.sample_rate)
        text = await self._transcribe_wav(session_id, st, wav_bytes)
        if not text:
            return

        # Send transcript to client
        await self._send_json(session_id, {"type": "transcript", "text": text})

        # Generate response
        reply = await self._chat(session_id, st, text)
        await self._send_agent(session_id, reply)

    # ---------------------------
    # STT / anti-ghost
    # ---------------------------
    async def _transcribe_wav(self, session_id: str, st: SessionState, wav_bytes: bytes) -> Optional[str]:
        try:
            audio_file = io.BytesIO(wav_bytes)
            audio_file.name = "audio.wav"  # critical: correct extension

            kwargs = {
                "model": self.stt_model,
                "file": audio_file,
                "temperature": 0.0,
            }
            resp = await asyncio.to_thread(lambda: self.openai.audio.transcriptions.create(**kwargs))
            text = getattr(resp, "text", "") or ""
            text = text.strip()
            if not text:
                return None

            # ---- Anti-ghost rules ----
            # 1) If we are (or very recently were) playing agent audio, and transcript overlaps a lot with last agent text -> drop.
            now = asyncio.get_event_loop().time()
            recently_playing = st.agent_playing or ((now - st.last_agent_play_ts) < 1.2)

            if recently_playing and st.last_agent_text:
                sim = _jaccard_similarity(text, st.last_agent_text)
                if sim >= 0.55:
                    logger.info("[%s] dropping likely-echo transcript (sim=%.2f): %r", session_id, sim, text)
                    return None

            # 2) Drop very short junk
            if len(text) < 2:
                return None

            return text

        except Exception as e:
            logger.exception("[%s] STT error: %s", session_id, e)
            return None

    # ---------------------------
    # CHAT + TTS
    # ---------------------------
    def _voice_for_language(self, lang: str) -> Optional[str]:
        lang = (lang or "").lower().strip()
        if lang.startswith("dut") or "neder" in lang:
            return self.voice_dutch or self.voice_english
        if lang.startswith("hin") or "हिं" in lang:
            return self.voice_hindi or self.voice_english
        return self.voice_english

    async def _chat(self, session_id: str, st: SessionState, user_text: str) -> str:
        # lightweight language switching
        ut = (user_text or "").lower()
        if "nederlands" in ut or "dutch" in ut:
            st.language = "dutch"
        elif "hindi" in ut or "हिंदी" in user_text:
            st.language = "hindi"
        elif "english" in ut:
            st.language = "english"

        system = (
            "You are a helpful order-taking agent for Taj Mahal (Indian restaurant). "
            "Keep replies short. Ask one question at a time. "
            "Common items: butter chicken, tikka masala, dal, samosa, rice, naan. "
            "Confirm order details. If unclear, ask to repeat. "
            "Do not invent menu items."
        )

        messages = [{"role": "system", "content": system}]
        if st.history:
            messages.extend(st.history[-10:])
        messages.append({"role": "user", "content": user_text})

        try:
            resp = await asyncio.to_thread(
                lambda: self.openai.chat.completions.create(
                    model=self.chat_model,
                    messages=messages,
                    temperature=0.4,
                )
            )
            content = resp.choices[0].message.content if resp and resp.choices else ""
            content = (content or "").strip()
            if not content:
                content = "Sorry, I didn't catch that. Could you repeat your order?"

            st.history = (st.history + [{"role": "user", "content": user_text}, {"role": "assistant", "content": content}])[-24:]
            return content
        except Exception as e:
            logger.exception("[%s] LLM error: %s", session_id, e)
            return "Sorry, something went wrong. Could you repeat that?"

    async def _tts_elevenlabs(self, st: SessionState, text: str) -> bytes:
        if not self.eleven_api_key:
            return b""
        voice_id = self._voice_for_language(st.language)
        if not voice_id:
            return b""

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": self.eleven_api_key,
            "accept": "audio/mpeg",
            "content-type": "application/json",
        }
        payload = {"text": text, "model_id": self.eleven_model_id}

        try:
            r = await self.http.post(url, headers=headers, json=payload)
            r.raise_for_status()
            return r.content
        except Exception:
            return b""

    async def _send_agent(self, session_id: str, text: str):
        st = self.sessions.get(session_id)
        if not st:
            return

        st.last_agent_text = text or ""

        # send text
        await self._send_json(session_id, {"type": "agent_text", "text": text})

        # send audio (if configured)
        audio = await self._tts_elevenlabs(st, text)
        if audio:
            b64 = __import__("base64").b64encode(audio).decode("ascii")
            await self._send_json(session_id, {"type": "agent_audio", "audio_base64": b64})

    async def _send_json(self, session_id: str, payload: Dict[str, Any]):
        ws = self.ws.get(session_id)
        if not ws:
            return
        try:
            await ws.send_text(json.dumps(payload))
        except Exception:
            pass

