# ANALYSIS: Optima-Voice Demo vs Voxeron Voice Agent

**Purpose**  
This document provides an evidence-based technical comparison between the Optima-Voice demo widget and the Voxeron Voice Agent implementation, focusing on:

- Audio capture + transport
- Voice Activity Detection (VAD) and turn-taking
- Barge-in (interrupting TTS)
- TTS delivery mode and perceived latency

The goal is to extract practical improvements Voxeron can adopt **without sacrificing its deterministic, parser-first architecture**.

---

## 1) Executive Summary

Optima-Voice feels smoother primarily because of **real-time audio orchestration**, not because of superior “AI intelligence”.

From verified inspection of Optima’s demo widget:

- Client-side AudioWorklet captures mic audio and performs local VAD.
- Audio is sent over WebSocket as **base64 PCM16 @ 16kHz** in frequent chunks.
- The backend is **ElevenLabs Conversational AI (ConvAI)** via a direct WebSocket connection.
- The client handles streaming audio playback, interruption, and UI states tightly.

Voxeron currently feels more robotic due to:

- Server-side VAD settings that are too easy to trigger (noise/cough) and too slow to finalize utterances.
- Non-streaming TTS (wait for full MP3 bytes) increasing “time-to-first-audio”.
- A barge-in threshold mismatch (wrong RMS scale) that effectively disables server-side barge-in.

**Conclusion:**  
Voxeron can close most of the “smoothness gap” by aligning VAD/turn-finalization parameters and improving TTS responsiveness, without changing its deterministic orchestration model.

---

## 2) Evidence Collected (What was actually verified)

### 2.1 Optima widget integration target (confirmed)
The Optima widget connects directly to ElevenLabs ConvAI:

- WebSocket URL pattern:
  - `wss://api.elevenlabs.io/v1/convai/conversation?agent_id=<agentId>`

No evidence of runtime `fetch()` calls to Optima backend for audio/AI inference was found inside the widget bundle.

### 2.2 Optima inbound/outbound event model (confirmed)
Optima’s widget handles these WebSocket event types (ElevenLabs → client):

- `conversation_initiation_metadata`
- `user_transcript`
- `agent_response`
- `audio`
- `interruption`
- `ping`

Optima’s widget sends (client → ElevenLabs):

- `conversation_initiation_client_data`
- `user_message`
- `user_activity`
- `pong`

Audio upload is done as JSON payloads containing `user_audio_chunk` (base64 bytes).

### 2.3 Optima client audio pipeline (confirmed)
The widget uses an `AudioWorkletProcessor` to:

- Downsample/convert mic input to **PCM16** with target **16kHz**
- Compute local VAD states:
  - `startSpeaking`
  - `stopSpeaking`
- Emit audio chunks (`chunk`) to the main thread

Observed VAD parameters in the worklet:

- `LOCAL_VOICE_THRESHOLD = 0.35` (peak amplitude)
- `LOCAL_VOICE_AVG_THRESHOLD = 0.06` (average amplitude)
- `SILENCE_DURATION_MS = 800`

Chunking behavior suggests ~100ms frame sizing, with periodic flush to WS.

---

## 3) Optima-Voice Architecture (Derived From Verified Signals)

### 3.1 High-level pipeline

- Browser mic capture (WebAudio)
- AudioWorklet: local VAD + PCM16 conversion
- WebSocket streaming to ElevenLabs ConvAI
- Server returns text + streaming audio
- Client queues audio and plays via WebAudio buffer source
- Interruption stops current playback immediately

### 3.2 Why it feels smooth
- Tight client-side VAD → instant UX feedback (“Listening”)
- Frequent audio chunking → low latency
- Streaming audio playback → short time-to-first-audio
- First-class barge-in → user can interrupt naturally

---

## 4) Voxeron Current Implementation (Verified From Repo)

### 4.1 VAD implementation
Voxeron uses a server-side energy-gate VAD:

- File: `src/api/services/audio.py`
- Uses RMS energy from PCM16 frames: `rms_pcm16(frame) -> float in [0..1]`
- Configurable parameters:
  - `FRAME_MS`
  - `ENERGY_FLOOR`
  - `SPEECH_CONFIRM_FRAMES`
  - `SILENCE_END_MS`
  - `MIN_UTTERANCE_MS`

### 4.2 Current VAD defaults (from `src/api/server.py`)
Defaults are environment-driven:

- `ENERGY_FLOOR = 0.006`
- `SPEECH_CONFIRM_FRAMES = 3`
- `SILENCE_END_MS = 650`
- `MIN_UTTERANCE_MS = 900`

Implications:

- Speech start triggers too easily (confirm_frames=3 + low floor)
- Utterance finalize is delayed (min_utterance_ms=900)

### 4.3 Server-side barge-in threshold bug (verified)
In `src/api/server.py`:

- RMS energy `e = rms_pcm16(frame)` is in `[0..1]`
- But `BARGE_IN_RMS` was set to `450.0`

This makes server-side barge-in effectively **non-functional** because `e` can never reach `450.0`.

### 4.4 TTS delivery mode (verified)
Voxeron TTS uses OpenAI audio/speech with full MP3 bytes:

- File: `src/api/services/openai_client.py`
- Function: `tts_mp3_bytes(...)`
- Behavior: waits for full MP3 bytes before playback begins

Implication: higher time-to-first-audio and “robotic” start timing compared to streaming playback.

---

## 5) Root Cause of “Robotic” Feel

The perceived difference is mostly explained by:

1) **VAD start gate too lenient**
- cough/throat-clear triggers listening and barge-in

2) **Utterance finalize delay too large**
- `MIN_UTTERANCE_MS=900ms` creates unnatural waiting before STT triggers

3) **Non-streaming TTS**
- waiting for full MP3 bytes increases response latency and makes the agent feel less interactive

4) **Server-side barge-in disabled by scale mismatch**
- `BARGE_IN_RMS=450.0` cannot be reached with `[0..1]` RMS values

---

## 6) Optima-Parity Parameter Tuning for Voxeron

These changes align Voxeron’s turn-taking closer to the Optima feel while remaining deterministic.

### 6.1 Recommended defaults

- `SILENCE_END_MS = 800` (match Optima)
- Increase speech onset confirmation window:
  - `SPEECH_CONFIRM_FRAMES = 8` (assuming `FRAME_MS=20ms`, this is ~160ms)
- Reduce minimum utterance requirement:
  - `MIN_UTTERANCE_MS = 300` (filters coughs, reduces lag)
- Slightly raise floor to reduce noise triggers:
  - `ENERGY_FLOOR = 0.01` (starting point; tune empirically)
- Fix barge-in threshold scale:
  - `BARGE_IN_RMS = 0.06` (align with Optima avg threshold region)

### 6.2 Suggested code defaults (server env fallbacks)

- In `src/api/server.py`:

- `ENERGY_FLOOR` default: `"0.01"`
- `SPEECH_CONFIRM_FRAMES` default: `"8"`
- `MIN_UTTERANCE_MS` default: `"300"`
- `SILENCE_END_MS` default: `"800"`

- `BARGE_IN_RMS`: `0.06`

Note: If `FRAME_MS` is 10ms instead of 20ms, scale `SPEECH_CONFIRM_FRAMES` roughly 2× (e.g., 16).

---

## 7) What Voxeron Can Adopt From Optima Without Copying Their Platform

Optima uses ElevenLabs ConvAI as a managed platform. Voxeron does not need to adopt that dependency to gain smoothness.

### 7.1 Immediate wins (days)
- Tune VAD thresholds and timing (as above)
- Fix server-side barge-in threshold scale
- Reduce perceived response latency via shorter turn-finalization

### 7.2 Mid-term wins (weeks)
- Move VAD to client side (AudioWorklet) so UI reacts instantly
- Stream PCM16 chunks over WS rather than waiting for full utterance
- Introduce streaming TTS or segmented TTS (short “lead-in” first)

---

## 8) Strategic Differentiation: Voxeron vs Optima

Optima (from observed implementation) is a polished wrapper around a managed voice-agent platform.

Voxeron’s differentiators remain:

- deterministic parser-first orchestration
- invariants around cart/order mutations
- observability and telemetry at decision boundaries
- multi-tenant + multi-domain architecture
- EU-sovereign deployment path (platform strategy)

**Key point:**  
Smoothness is an engineering and parameter problem. Determinism is an architectural strategy. Voxeron can have both.

---

## 9) Next Steps

1) Apply VAD default tuning and barge-in scale fix
2) Validate against live demo behavior:
   - cough/throat-clear should not trigger endless listening
   - short utterances should finalize quickly
   - agent barge-in should remain responsive but gated
3) Reduce TTS time-to-first-audio:
   - segmented TTS (“Okay.” then full response) as a low-cost interim step
4) Plan client-side VAD + streaming loop as a structural improvement

---

**Status:** Analysis complete  
**Owner:** Voxeron engineering  
**Last updated:** 2026-01-27
