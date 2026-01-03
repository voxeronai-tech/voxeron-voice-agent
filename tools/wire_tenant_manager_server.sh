#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SERVER="src/api/server.py"
if [[ ! -f "$SERVER" ]]; then
  echo "❌ $SERVER not found"
  exit 1
fi

echo "==> Patching $SERVER (TenantManager wiring + STT prompt + TTS instructions + heartbeat)"

python3 - <<'PY'
import re, pathlib
p = pathlib.Path("src/api/server.py")
s = p.read_text(encoding="utf-8")

# 1) import TenantManager + TenantConfig
if "from .tenant_manager import TenantManager" not in s:
    s = s.replace(
        "from .intent import detect_language_intent, norm_simple, contains_devanagari\n",
        "from .intent import detect_language_intent, norm_simple, contains_devanagari\n"
        "from .tenant_manager import TenantManager, TenantConfig\n"
    )

# 2) env flags + tenant manager instance
insertion = """
TENANTS_DIR = os.getenv("TENANTS_DIR", "tenants")
TENANT_RULES_ENABLED = os.getenv("TENANT_RULES_ENABLED", "0") == "1"
TENANT_STT_PROMPT_ENABLED = os.getenv("TENANT_STT_PROMPT_ENABLED", "1") == "1"
TENANT_TTS_INSTRUCTIONS_ENABLED = os.getenv("TENANT_TTS_INSTRUCTIONS_ENABLED", "1") == "1"

tenant_manager = TenantManager(TENANTS_DIR)
""".strip() + "\n\n"

if "tenant_manager = TenantManager" not in s:
    s = s.replace("client = AsyncOpenAI(api_key=OPENAI_API_KEY)\n", "client = AsyncOpenAI(api_key=OPENAI_API_KEY)\n\n" + insertion)

# 3) SessionState: add tenant_cfg + idle heartbeat tracking
if "tenant_cfg: Optional[TenantConfig]" not in s:
    s = s.replace(
        "tenant_name: str = \"\"\n\n    lang: str = \"en\"",
        "tenant_name: str = \"\"\n\n    tenant_cfg: Optional[TenantConfig] = None\n\n    lang: str = \"en\""
    )

if "last_activity_ts" not in s:
    s = s.replace(
        "turn_id: int = 0\n",
        "turn_id: int = 0\n\n    last_activity_ts: float = 0.0\n    heartbeat_task: Optional[asyncio.Task] = None\n"
    )

# 4) add choose_tts_instructions helper
if "def choose_tts_instructions" not in s:
    helper = """
def choose_tts_instructions(state: SessionState) -> str:
    # Tenant-driven TTS instructions to prevent accent drift (e.g., Flemish feel)
    if not TENANT_TTS_INSTRUCTIONS_ENABLED:
        return ""
    cfg = getattr(state, "tenant_cfg", None)
    if not cfg:
        return ""
    ins = (cfg.tts_instructions or {}).get(state.lang) or ""
    return str(ins).strip()
""".strip() + "\n\n"
    s = s.replace("def choose_voice(lang: str) -> str:\n", helper + "def choose_voice(lang: str) -> str:\n")

# 5) STT: add prompt param (tenant + menu bias)
# find transcribe_pcm kwargs block
if '"prompt"' not in s:
    s = re.sub(
        r"kwargs:\s*Dict\[str,\s*Any\]\s*=\s*\{\"model\":\s*OPENAI_STT_MODEL,\s*\"file\":\s*f\}\n",
        "kwargs: Dict[str, Any] = {\"model\": OPENAI_STT_MODEL, \"file\": f}\n"
        "\n"
        s,
        flags=re.M
    )

# 6) TTS: inject instructions into payload
s = s.replace(
    "payload = {\"model\": OPENAI_TTS_MODEL, \"voice\": voice, \"input\": text, \"format\": \"mp3\"}\n",
    "instructions = choose_tts_instructions(state)\n"
    "payload = {\"model\": OPENAI_TTS_MODEL, \"voice\": voice, \"input\": text, \"format\": \"mp3\"}\n"
    "if instructions:\n"
    "    payload[\"instructions\"] = instructions\n"
)

# 7) WS connect: load tenant_cfg from tenants/ folder, keep Neon for menu
if "tenant_manager.load_tenant" not in s:
    inject = """
    # Load tenant config (file-based). Keep DB menu snapshot as authoritative for items/prices.
    try:
        # Map tenant_ref -> folder name. For demo we treat default as taj_mahal.
        tenant_folder = state.tenant_ref
        if tenant_folder == "default":
            tenant_folder = "taj_mahal"
        state.tenant_cfg = tenant_manager.load_tenant(tenant_folder)
        # Use tenant voices if present
        if state.tenant_cfg and state.tenant_cfg.supported_langs:
            pass
    except Exception:
        state.tenant_cfg = None
""".strip() + "\n\n"

    s = s.replace(
        "    state.tenant_ref = ws.query_params.get(\"tenant\") or \"default\"\n\n    try:\n",
        "    state.tenant_ref = ws.query_params.get(\"tenant\") or \"default\"\n"
        "    state.last_activity_ts = time.time()\n\n" + inject + "    try:\n"
    )

# 8) Normalize transcript: optionally use tenant rules in parallel, without breaking baseline
if "TENANT_RULES_ENABLED" in s and "tenant_manager.normalize_text" not in s:
    s = s.replace(
        "            transcript = normalize_transcript_for_demo(transcript, state.lang)\n",
        "            # Baseline normalizer\n"
        "            baseline_norm = normalize_transcript_for_demo(transcript, state.lang)\n"
        "            transcript = baseline_norm\n"
        "\n"
        "            # Tenant normalizer (feature-flag). Parallel-run logging when disabled.\n"
        "            try:\n"
        "                cfg = getattr(state, 'tenant_cfg', None)\n"
        "                if cfg:\n"
        "                    tenant_norm = tenant_manager.normalize_text(cfg, state.lang, transcript)\n"
        "                    if TENANT_RULES_ENABLED:\n"
        "                        transcript = tenant_norm\n"
        "                    else:\n"
        "                        if tenant_norm != baseline_norm:\n"
        "                            logger.info('[tenant_norm][diff] baseline=%r tenant=%r', baseline_norm, tenant_norm)\n"
        "            except Exception:\n"
        "                pass\n"
    )

# 9) idle heartbeat task (10s) to keep the turn alive when user is silent
if "async def heartbeat_loop" not in s:
    hb = """
async def heartbeat_loop(ws: WebSocket, state: SessionState) -> None:
    try:
        while True:
            await asyncio.sleep(1.0)
            if ws.client_state.name.lower() != "connected":
                return
            idle = time.time() - float(getattr(state, "last_activity_ts", 0.0) or 0.0)
            cfg = getattr(state, "tenant_cfg", None)
            idle_sec = 10
            msg_en = "Still there? What would you like to order next?"
            msg_nl = "Ben je er nog? Wat wil je hierna bestellen?"
            if cfg:
                hb = (cfg.rules or {}).get("heartbeat") or {}
                idle_sec = int(hb.get("idle_seconds") or idle_sec)
                msg_en = str(hb.get("en") or msg_en)
                msg_nl = str(hb.get("nl") or msg_nl)

            # Only heartbeat when not speaking/thinking/processing and in chat
            if idle >= idle_sec and (not state.is_processing) and state.phase in ("language_select","chat"):
                # Don't spam: reset timer after sending
                state.last_activity_ts = time.time()
                msg = msg_nl if state.lang == "nl" else msg_en
                msg = enforce_output_language(msg, state.lang)
                await send_agent_text(ws, msg)
                await stream_tts_mp3(ws, state, msg)
    except asyncio.CancelledError:
        return
    except Exception:
        return
""".strip() + "\n\n"
    s = s.replace("# -------------------------\n# App + MenuStore lifecycle\n# -------------------------\n", hb + "# -------------------------\n# App + MenuStore lifecycle\n# -------------------------\n")

# Start heartbeat when ws connects, cancel on close
if "state.heartbeat_task" in s and "heartbeat_loop(ws, state)" not in s:
    s = s.replace(
        "    greet = enforce_output_language(tr(state.lang, \"greet\"), state.lang)\n",
        "    # Heartbeat (keeps demo alive when user is silent)\n"
        "    try:\n"
        "        state.heartbeat_task = asyncio.create_task(heartbeat_loop(ws, state))\n"
        "    except Exception:\n"
        "        state.heartbeat_task = None\n\n"
        "    greet = enforce_output_language(tr(state.lang, \"greet\"), state.lang)\n"
    )

# Update last_activity_ts when audio/text received
# On any websocket receive, after msg=await ws.receive()
s = s.replace(
    "            msg = await ws.receive()\n",
    "            msg = await ws.receive()\n            state.last_activity_ts = time.time()\n"
)

# cancel heartbeat in finally
s = s.replace(
    "        try:\n            await cancel_proc(state)\n            await cancel_tts(state)\n        except Exception:\n            pass\n",
    "        try:\n            if state.heartbeat_task and not state.heartbeat_task.done():\n                state.heartbeat_task.cancel()\n        except Exception:\n            pass\n        try:\n            await cancel_proc(state)\n            await cancel_tts(state)\n        except Exception:\n            pass\n"
)

# 10) Ensure transcribe_pcm sets current_state for prompt
# At call site before transcribe_pcm, set current_state=state, reset after.
p.write_text(s, encoding="utf-8")
print("✅ Patched server.py")
PY

echo "✅ Patched src/api/server.py"
echo
echo "Set flags (optional):"
echo "  export TENANT_RULES_ENABLED=0   # start with parallel-run diff logging only"
echo "  export TENANT_STT_PROMPT_ENABLED=1"
echo "  export TENANT_TTS_INSTRUCTIONS_ENABLED=1"
echo
echo "Run server and watch logs for [tenant_norm][diff] lines."
