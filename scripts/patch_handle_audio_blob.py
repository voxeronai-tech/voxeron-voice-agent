from __future__ import annotations
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
p = ROOT / "src/agent/voice_agent.py"
src = p.read_text(encoding="utf-8").splitlines(True)  # keep line endings

def ensure_import(lines: list[str], import_stmt: str) -> list[str]:
    # If already present, do nothing
    if any(import_stmt in ln for ln in lines):
        return lines

    # Insert after the last import/from block near the top
    insert_at = 0
    for i, ln in enumerate(lines[:200]):
        if ln.startswith("import ") or ln.startswith("from "):
            insert_at = i + 1

    lines.insert(insert_at, import_stmt + "\n")
    return lines

# Ensure basic imports used by the new function exist
src = ensure_import(src, "import os")
src = ensure_import(src, "import asyncio")
# Optional may be imported as `from typing import Optional, ...`
if not any(re.match(r"^from typing import .*Optional", ln) for ln in src):
    src = ensure_import(src, "from typing import Optional")

# Find start of handle_audio_blob
start = None
for i, ln in enumerate(src):
    if re.match(r"^\s*async def handle_audio_blob\s*\(", ln):
        start = i
        break
if start is None:
    raise SystemExit("Could not find: async def handle_audio_blob(")

# Determine indentation of the function line
indent = re.match(r"^(\s*)", src[start]).group(1)

# Find end: next 'async def' at same indentation (or EOF)
end = None
for j in range(start + 1, len(src)):
    if src[j].startswith(indent) and re.match(rf"^{re.escape(indent)}async def\s+\w+\s*\(", src[j]):
        end = j
        break
if end is None:
    end = len(src)

new_block = f"""{indent}async def handle_audio_blob(self, session_id: str, audio: bytes) -> Optional[str]:
{indent}    \"\"\"
{indent}    Receives a binary audio blob from the websocket (typically MediaRecorder chunks),
{indent}    dumps it for inspection, and transcribes using OpenAI.

{indent}    Key fix vs 400 "corrupted/unsupported":
{indent}    - Always pass a file-like object with a correct filename extension
{indent}      matching the container (default: audio.webm).
{indent}    \"\"\"
{indent}    try:
{indent}        if not audio or len(audio) < 800:
{indent}            return None

{indent}        # Dump incoming audio so we can confirm container/codec via `file logs/audio_*.webm`
{indent}        try:
{indent}            from src.utils.audio_debug import dump_audio_blob
{indent}            ext = (os.getenv("OPENAI_AUDIO_FILENAME_HINT", "audio.webm").split(".")[-1] or "bin")
{indent}            dump_audio_blob(session_id, audio, ext=ext)
{indent}        except Exception:
{indent}            pass

{indent}        import io
{indent}        audio_file = io.BytesIO(audio)

{indent}        filename = os.getenv("OPENAI_AUDIO_FILENAME_HINT", "audio.webm").strip() or "audio.webm"
{indent}        audio_file.name = filename

{indent}        model = os.getenv("OPENAI_STT_MODEL", "whisper-1")

{indent}        resp = await asyncio.to_thread(
{indent}            lambda: self.openai.audio.transcriptions.create(
{indent}                model=model,
{indent}                file=audio_file,
{indent}            )
{indent}        )
{indent}        text = (getattr(resp, "text", None) or "").strip()
{indent}        return text or None

{indent}    except Exception as e:
{indent}        logger.exception("[%s] STT error: %s", session_id, e)
{indent}        return None
"""

# Backup
bak = p.with_suffix(".py.bak")
bak.write_text("".join(src), encoding="utf-8")

# Replace block
out_lines = src[:start] + [new_block] + src[end:]
p.write_text("".join(out_lines), encoding="utf-8")

print("✅ Patched handle_audio_blob()")
print("Backup:", bak)
print("File:", p)
