from __future__ import annotations

import io
import time
import wave
from typing import Optional


def pcm16_to_wav(pcm16: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16)
    return buf.getvalue()


def rms_pcm16(frame: bytes) -> float:
    if not frame:
        return 0.0
    n = len(frame) // 2
    if n <= 0:
        return 0.0
    ssum = 0.0
    for i in range(0, len(frame), 2):
        v = int.from_bytes(frame[i : i + 2], "little", signed=True)
        fv = v / 32768.0
        ssum += fv * fv
    return (ssum / max(1, n)) ** 0.5


class VAD:
    """
    Simple energy-gate VAD.
    feed(frame, energy) returns utterance bytes when speech ends, else None.
    """

    def __init__(
        self,
        frame_ms: int,
        energy_floor: float,
        speech_confirm_frames: int,
        silence_end_ms: int,
        min_utterance_ms: int,
    ):
        self.frame_ms = int(frame_ms)
        self.energy_floor = float(energy_floor)
        self.speech_confirm_frames = int(speech_confirm_frames)
        self.silence_end_ms = int(silence_end_ms)
        self.min_utterance_ms = int(min_utterance_ms)

        self.in_speech = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.buf = bytearray()
        self.started_at = 0.0

    def reset(self) -> None:
        self.in_speech = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.buf = bytearray()
        self.started_at = 0.0

    def feed(self, frame: bytes, energy: float) -> Optional[bytes]:
        is_voice = energy >= self.energy_floor

        if not self.in_speech:
            if is_voice:
                self.speech_frames += 1
                if self.speech_frames >= self.speech_confirm_frames:
                    self.in_speech = True
                    self.started_at = time.time()
                    self.buf.extend(frame)
                    self.silence_frames = 0
            else:
                self.speech_frames = 0
            return None

        # in speech
        self.buf.extend(frame)
        if is_voice:
            self.silence_frames = 0
        else:
            self.silence_frames += 1

        silence_ms = self.silence_frames * self.frame_ms
        utter_ms = int((time.time() - self.started_at) * 1000.0)

        if silence_ms >= self.silence_end_ms and utter_ms >= self.min_utterance_ms:
            out = bytes(self.buf)
            self.reset()
            return out

        return None
