import os
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# Ensure repo root is on sys.path so "import src.*" works
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

load_dotenv()

print("==[verify_04_db_init]==")
print("ROOT:", ROOT)
print("DATABASE_URL set:", bool(os.getenv("DATABASE_URL")))
print()

try:
    from src.db.database import db
except Exception as e:
    print("❌ Failed to import db:", repr(e))
    raise

print("DB object:", db)
print("Has pool attr:", hasattr(db, "pool"))
print("pool is None:", getattr(db, "pool", None) is None)
print()

async def try_methods():
    candidates = [
        "connect",
        "init",
        "init_pool",
        "initialize",
        "startup",
        "open",
        "create_pool",
    ]
    for name in candidates:
        fn = getattr(db, name, None)
        if fn and callable(fn):
            print(f"▶ Trying db.{name}()")
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
                print(f"✅ db.{name}() succeeded")
                return name
            except Exception as e:
                print(f"❌ db.{name}() failed: {type(e).__name__}: {e}")
    return None

async def test_query():
    if not hasattr(db, "get_menu_items"):
        print("⚠️ db.get_menu_items not found")
        return
    try:
        rows = await db.get_menu_items()
        print(f"✅ Query OK: get_menu_items() -> {len(rows)} rows")
        if rows:
            print("sample keys:", list(rows[0].keys())[:30])
    except Exception as e:
        print(f"❌ Query failed: {type(e).__name__}: {e}")

async def try_close():
    for name in ["close", "shutdown", "dispose"]:
        fn = getattr(db, name, None)
        if fn and callable(fn):
            print(f"▶ Trying db.{name}()")
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
                print(f"✅ db.{name}() succeeded")
                return
            except Exception as e:
                print(f"❌ db.{name}() failed: {type(e).__name__}: {e}")

async def main():
    method = await try_methods()
    print()
    print("init method used:", method)
    print("pool is None after init:", getattr(db, "pool", None) is None)
    print()
    await test_query()
    print()
    await try_close()
    print()
    print("==[end verify_04_db_init]== ")

asyncio.run(main())
