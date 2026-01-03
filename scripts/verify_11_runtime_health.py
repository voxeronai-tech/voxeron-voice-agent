import os
import sys
import json
import time
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def http_json(url: str):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    print("==[verify_11_runtime_health]==")
    print("ROOT:", ROOT)

    # Show env presence (no secrets)
    print("DATABASE_URL set:", bool(os.getenv("DATABASE_URL")))
    print("OPENAI_API_KEY set:", bool(os.getenv("OPENAI_API_KEY")))
    print("ELEVENLABS_API_KEY set:", bool(os.getenv("ELEVENLABS_API_KEY")))

    # Backend HTTP
    try:
        root = http_json("http://127.0.0.1:8000/")
        print("\nBackend /:", root)
    except Exception as e:
        print("\nBackend /: FAILED:", e)
        print("Hint: is uvicorn running on 127.0.0.1:8000 ?")
        return

    # Tenant current
    try:
        cur = http_json("http://127.0.0.1:8000/tenant/current")
        print("Backend /tenant/current:", cur)
    except Exception as e:
        print("Backend /tenant/current: FAILED:", e)

    print("\nFrontend expected URL (when started): http://127.0.0.1:5173/voice_widget.html")
    print("WebSocket URL used by frontend: ws://127.0.0.1:8000/ws?session_id=...")

    print("\n==[end verify_11_runtime_health]==")

if __name__ == "__main__":
    main()
