from __future__ import annotations
from pathlib import Path
import binascii
from datetime import datetime, timezone

# Always write under repo root, regardless of current working directory
REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = REPO_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def dump_audio_blob(session_id: str, audio_bytes: bytes, ext: str = "bin") -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = LOG_DIR / f"audio_{session_id}_{ts}.{ext}"
    out.write_bytes(audio_bytes)
    head = audio_bytes[:32]
    print(f"[audio_debug] wrote={out} bytes={len(audio_bytes)} head32={binascii.hexlify(head).decode()}")
    return out
