import asyncio
import os
import sys
from pathlib import Path
from pprint import pprint

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

from src.db.database import db
from src.agent.voice_agent_with_db import VoiceAgent
from src.api.contract_validate import validate_payload

async def main():
    print("==[verify_10_create_pos_order_tool]==")
    await db.connect()

    async with db.pool.acquire() as con:
        t = await con.fetchrow("SELECT tenant_id, name FROM tenants LIMIT 1")
        tenant_id = str(t["tenant_id"])
        print("tenant:", t["name"], tenant_id)

        mi = await con.fetchrow("""
            SELECT item_id, name_en, price_delivery
            FROM menu_items
            ORDER BY item_id
            LIMIT 1
        """)

    agent = VoiceAgent()

    order_draft = {
        "tenant_id": tenant_id,
        "location_id": None,
        "session_id": "sess_tool_001",
        "fulfillment_type": "pickup",
        "items": [
            {
                "item_id": mi["item_id"],
                "name": mi["name_en"] or mi["item_id"],
                "quantity": 2,
                "unit_price": {"amount": float(mi["price_delivery"]), "currency": "EUR"},
                "modifiers": [],
                "notes": None,
            }
        ],
        "idempotency_key": "tool-smoke-1",
    }

    print("\n-- validating input schema --")
    validate_payload("domain/order_draft.v0.6.json", order_draft)

    print("\n-- calling tool --")
    result = await agent.create_pos_order(order_draft)

    print("\n-- result --")
    pprint(result)

    print("\n-- validating output schema --")
    validate_payload("domain/order_result.v0.6.json", result)
    print("\n✅ create_pos_order tool contract OK")

    await db.close()
    print("\n==[end verify_10_create_pos_order_tool]==")

asyncio.run(main())
