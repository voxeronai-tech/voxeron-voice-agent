from __future__ import annotations

import io
import logging
import time
import wave
from collections import deque
from typing import Deque, Optional

logger = logging.getLogger(__name__)


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
    Simple energy-gate VAD with pre-roll buffering.

    feed(frame, energy) returns utterance bytes when speech ends, else None.

    Why pre-roll:
    The original implementation only started buffering after speech was confirmed
    (speech_confirm_frames). This can truncate the beginning of an utterance,
    especially with soft starts or filler words. Pre-roll keeps a small rolling
    buffer of recent frames and prepends it once speech is confirmed.
    """

    def __init__(
        self,
        frame_ms: int,
        energy_floor: float,
        speech_confirm_frames: int,
        silence_end_ms: int,
        min_utterance_ms: int,
        *,
        preroll_ms: int = 300,
        debug: bool = False,
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

        self._preroll_frames = max(1, int(preroll_ms) // max(1, self.frame_ms))
        self._preroll: Deque[bytes] = deque(maxlen=self._preroll_frames)
        self._debug = bool(debug)

    def reset(self) -> None:
        self.in_speech = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.buf = bytearray()
        self.started_at = 0.0
        self._preroll.clear()

    def feed(self, frame: bytes, energy: float) -> Optional[bytes]:
        is_voice = energy >= self.energy_floor

        if not self.in_speech:
            self._preroll.append(frame)

            if is_voice:
                self.speech_frames += 1
                if self.speech_frames >= self.speech_confirm_frames:
                    self.in_speech = True
                    self.started_at = time.time()
                    self.silence_frames = 0

                    # Start utterance buffer with pre-roll frames
                    self.buf = bytearray().join(self._preroll)

                    if self._debug:
                        logger.info(
                            "VAD: enter_speech confirm_frames=%s energy=%.4f preroll_frames=%s preroll_bytes=%s",
                            self.speech_confirm_frames,
                            energy,
                            self._preroll_frames,
                            len(self.buf),
                        )
            else:
                self.speech_frames = 0
            return None

        # In speech: buffer all frames
        self.buf.extend(frame)

        if is_voice:
            self.silence_frames = 0
        else:
            self.silence_frames += 1

        silence_ms = self.silence_frames * self.frame_ms
        utter_ms = int((time.time() - self.started_at) * 1000.0)

        if silence_ms >= self.silence_end_ms and utter_ms >= self.min_utterance_ms:
            if self._debug:
                logger.info(
                    "VAD: end_speech utter_ms=%s silence_ms=%s total_bytes=%s",
                    utter_ms,
                    silence_ms,
                    len(self.buf),
                )
            out = bytes(self.buf)
            self.reset()
            return out

        return None
