from __future__ import annotations

import io
import logging
import time
import wave
from collections import deque
from typing import Deque, Optional


log = logging.getLogger(__name__)


def pcm16_to_wav(pcm16: bytes, sr: int) -> bytes:
    """
    Wrap raw PCM16 mono data into a WAV container.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sr)
        wf.writeframes(pcm16)
    return buf.getvalue()


def rms_pcm16(frame: bytes) -> float:
    """
    RMS energy of a PCM16 mono frame (bytes). Returns 0.0 for empty frames.
    """
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

    Usage:
        vad = VAD(...)
        utter = vad.feed(frame_bytes, energy=rms_pcm16(frame_bytes))
        if utter is not None:
            # utter is complete utterance PCM bytes (mono, pcm16)

    Behavior:
      - While not in speech, we keep a small preroll deque of frames (preroll_ms).
      - When speech begins (after speech_confirm_frames), we prepend preroll frames
        to the utterance buffer to avoid truncating the start.
      - Speech ends after silence_end_ms of below-threshold frames AND min_utterance_ms met.
    """

    def __init__(
        self,
        *,
        energy_floor: float,
        frame_ms: int,
        speech_confirm_frames: int,
        silence_end_ms: int,
        min_utterance_ms: int,
        preroll_ms: int = 160,
        debug: bool = False,
    ) -> None:
        self.energy_floor = float(energy_floor)
        self.frame_ms = int(frame_ms)
        self.speech_confirm_frames = int(speech_confirm_frames)
        self.silence_end_ms = int(silence_end_ms)
        self.min_utterance_ms = int(min_utterance_ms)

        # State
        self.in_speech = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.buf = bytearray()
        self.started_at = 0.0

        # Preroll (store last N frames while idle)
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
        """
        Feed one frame of PCM bytes and its computed energy (e.g., rms_pcm16(frame)).
        Returns utterance bytes when speech ends, otherwise None.
        """
        is_voice = energy >= self.energy_floor

        if not self.in_speech:
            # Always collect preroll while idle (including non-voice frames)
            self._preroll.append(frame)

            if is_voice:
                self.speech_frames += 1
                if self.speech_frames >= self.speech_confirm_frames:
                    # Speech start confirmed
                    self.in_speech = True
                    self.started_at = time.time()
                    self.silence_frames = 0

                    # Prepend preroll frames to avoid truncating start
                    for fr in self._preroll:
                        self.buf.extend(fr)
                    self._preroll.clear()

                    # Also include current frame (in case maxlen trimmed it)
                    self.buf.extend(frame)

                    if self._debug:
                        log.debug(
                            "VAD start: preroll_frames=%d buf_bytes=%d energy=%.5f floor=%.5f",
                            self._preroll_frames,
                            len(self.buf),
                            energy,
                            self.energy_floor,
                        )
            else:
                # not voice; reset speech confirmation counter
                self.speech_frames = 0

            return None

        # In speech: accumulate
        self.buf.extend(frame)

        if is_voice:
            self.silence_frames = 0
        else:
            self.silence_frames += 1

        silence_ms = self.silence_frames * self.frame_ms
        utter_ms = int((time.time() - self.started_at) * 1000.0)

        if silence_ms >= self.silence_end_ms and utter_ms >= self.min_utterance_ms:
            out = bytes(self.buf)
            if self._debug:
                log.debug(
                    "VAD end: utter_ms=%d silence_ms=%d out_bytes=%d",
                    utter_ms,
                    silence_ms,
                    len(out),
                )
            self.reset()
            return out

        return None
