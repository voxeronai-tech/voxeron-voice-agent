import os
import json
import logging
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv
import openai
from elevenlabs import ElevenLabs

from src.db.database import db
from src.api.contract_validate import validate_payload
from src.agent.contract_adapters import make_kitchen_status, make_menu

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEMO_TENANT_ID = os.getenv("DEMO_TENANT_ID") or "ca25e23c-c1a2-41e7-82be-ad8187c4c459"
DEMO_LOCATION_ID = os.getenv("DEMO_LOCATION_ID") or None

class VoiceAgent:
    def __init__(self):
        self.openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.elevenlabs = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))
        self.voice_id = os.getenv("ELEVENLABS_VOICE_ID", "wlmwDR77ptH6bKHZui0l")

        self.conversations = {}
        self.session_orders = {}
        logger.info(f"✅ VoiceAgent initialized with voice: {self.voice_id}")

    async def _ensure_db(self):
        # db.pool is created by db.connect()
        if getattr(db, "pool", None) is None:
            await db.connect()

    def get_conversation(self, session_id):
        if session_id not in self.conversations:
            self.conversations[session_id] = [{
                "role": "system",
                "content": """You are Maya, a warm Indian host at Taj Mahal restaurant taking phone orders.

YOUR PERSONALITY:
- Speak in Indian English style: "What would you like to have?", "Kindly let me know"
- Use phrases: "Wonderful choice!", "Absolutely!", "Most definitely"
- Be warm, hospitable, and helpful

YOUR TASK:
Help customers build their order step by step.

CONVERSATION FLOW:
1. Greet warmly
2. When customer mentions a dish:
   - Confirm the dish name
   - Ask about spice level (mild, medium, hot, extra hot) for curries
   - Ask quantity if not mentioned
   - Ask if they want sides (naan, rice)
3. Suggest popular items if they're unsure
4. Before finishing, ask: "Anything else?" or "Would you like drinks or dessert?"
5. At the end, ask for delivery address or if it's pickup

IMPORTANT:
- Keep responses SHORT (1-2 sentences max)
- Don't list prices unless asked
- Natural conversation, not robotic
- If you don't understand, ask politely

TOOLS YOU CAN USE:
- get_menu()
- get_kitchen_status()
- create_pos_order(order_draft)"""
            }]

            self.session_orders[session_id] = {
                "items": [],
                "customer_name": None,
                "customer_phone": None,
                "order_type": None,
            }
        return self.conversations[session_id]

    async def transcribe(self, audio_bytes):
        import io
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "audio.webm"
        transcript = await asyncio.to_thread(
            self.openai_client.audio.transcriptions.create,
            model=os.getenv("OPENAI_STT_MODEL", "whisper-1"),
            file=audio_file
        )
        return transcript.text.strip()

    # ----------------------------
    # Contract tools (v0.6)
    # ----------------------------

    async def get_kitchen_status(self) -> dict:
        # Demo: static signal, but contract-enforced
        payload = make_kitchen_status(
            tenant_id=DEMO_TENANT_ID,
            location_id=DEMO_LOCATION_ID,
            wait_time_min=10,
            capacity_status="normal",
            notes=None,
            blackout_items=[],
        )
        validate_payload("domain/kitchen_status.v0.6.json", payload)
        return payload

    async def get_menu(self) -> dict:
        await self._ensure_db()
        items = await db.get_menu_items()
        categories = await db.get_categories()

        payload = make_menu(
            tenant_id=DEMO_TENANT_ID,
            location_id=DEMO_LOCATION_ID,
            items=items,
            categories=categories,
            currency="EUR",
        )
        validate_payload("domain/menu.v0.6.json", payload)
        return payload

    async def create_pos_order(self, order_draft: dict) -> dict:
        """
        Contract tool: create_pos_order(OrderDraft) -> OrderResult
        - Validates input/output using v0.6 schemas
        - Persists minimal order info in DB (customers/orders/order_items)
        - Uses DB-safe idempotency via orders.session_id = f"{session_id}:{idempotency_key}" if provided
        """
        await self._ensure_db()

        validate_payload("domain/order_draft.v0.6.json", order_draft)

        tenant_id = order_draft["tenant_id"]           # UUID string (matches DB)
        session_id = order_draft["session_id"]
        location_id = order_draft.get("location_id")
        fulfillment_type = order_draft["fulfillment_type"]  # "pickup" | "delivery"
        idempotency_key = order_draft.get("idempotency_key")

        db_session_id = f"{session_id}:{idempotency_key}" if idempotency_key else session_id
        order_type = fulfillment_type.upper()  # -> PICKUP/DELIVERY

        customer_name = None
        customer_phone = None
        if order_draft.get("customer"):
            customer_name = order_draft["customer"].get("name")
            customer_phone = order_draft["customer"].get("phone")

        async with db.pool.acquire() as con:
            # 1) idempotency check (demo approach)
            if idempotency_key:
                existing = await con.fetchrow(
                    """
                    SELECT order_id, created_at
                    FROM orders
                    WHERE tenant_id = $1 AND session_id = $2
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    tenant_id, db_session_id
                )
                if existing:
                    result = {
                        "tenant_id": tenant_id,
                        "location_id": location_id,
                        "session_id": session_id,
                        "status": "created",
                        "order_id": str(existing["order_id"]),
                        "provider": "internal_db",
                        "message": "Order retrieved (idempotent)",
                        "created_at": existing["created_at"].isoformat() if existing["created_at"] else None,
                    }
                    validate_payload("domain/order_result.v0.6.json", result)
                    return result

            # 2) create customer
            customer_id = await con.fetchval(
                """
                INSERT INTO customers (tenant_id, name, phone)
                VALUES ($1, $2, $3)
                RETURNING customer_id
                """,
                tenant_id,
                customer_name or f"Voice Customer ({session_id[:8]})",
                customer_phone or db_session_id
            )

            # 3) create order (total computed from items)
            created_at = datetime.now(timezone.utc)
            order_id = await con.fetchval(
                """
                INSERT INTO orders (tenant_id, customer_id, order_type, total_amount, session_id, order_status, created_at, language, notes)
                VALUES ($1, $2, $3, 0, $4, 'NEW', $5, NULL, NULL)
                RETURNING order_id
                """,
                tenant_id, customer_id, order_type, db_session_id, created_at
            )

            total_amount = 0.0
            for it in order_draft["items"]:
                item_id = it["item_id"]
                qty = int(it["quantity"])

                # Unit price: use contract if present, else lookup delivery price
                unit_price = None
                if it.get("unit_price") and isinstance(it["unit_price"], dict):
                    unit_price = float(it["unit_price"].get("amount", 0.0))

                if unit_price is None or unit_price == 0.0:
                    row = await con.fetchrow(
                        "SELECT price_delivery FROM menu_items WHERE item_id = $1 AND tenant_id = $2",
                        item_id, tenant_id
                    )
                    unit_price = float(row["price_delivery"]) if row else 0.0

                line_total = unit_price * qty
                total_amount += line_total

                customizations = {}
                if it.get("modifiers") is not None:
                    customizations["modifiers"] = it.get("modifiers") or []
                if it.get("notes"):
                    customizations["notes"] = it.get("notes")

                await con.execute(
                    """
                    INSERT INTO order_items (tenant_id, order_id, item_id, quantity, price_at_order, customizations)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                    """,
                    tenant_id,
                    order_id,
                    item_id,
                    qty,
                    line_total,
                    json.dumps(customizations) if customizations else None
                )

            await con.execute(
                "UPDATE orders SET total_amount = $1 WHERE order_id = $2",
                total_amount, order_id
            )

        result = {
            "tenant_id": tenant_id,
            "location_id": location_id,
            "session_id": session_id,
            "status": "created",
            "order_id": str(order_id),
            "provider": "internal_db",
            "message": "Order created successfully",
            "created_at": created_at.isoformat(),
        }
        validate_payload("domain/order_result.v0.6.json", result)
        logger.info(f"✅ create_pos_order: order_id={order_id} tenant_id={tenant_id} session_id={session_id}")
        return result

    # ----------------------------
    # LLM interaction
    # ----------------------------

    async def generate_response(self, session_id: str, user_text: str, is_greeting=False):
        if is_greeting:
            return "Namaste! Welcome to Taj Mahal restaurant. What would you like to order today?"

        conversation = self.get_conversation(session_id)
        conversation.append({"role": "user", "content": user_text})

        # NOTE: Using legacy functions interface for now (works for demo).
        # We keep tool schema simple: order_draft passed as JSON object.
        functions = [
            {
                "name": "get_menu",
                "description": "Fetch the full menu (items + currency) for this tenant",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_kitchen_status",
                "description": "Fetch current kitchen load and estimated wait time",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "create_pos_order",
                "description": "Create an order in the POS/internal DB using an OrderDraft payload",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_draft": {"type": "object", "description": "OrderDraft v0.6 payload"}
                    },
                    "required": ["order_draft"]
                },
            },
        ]

        response = await asyncio.to_thread(
            self.openai_client.chat.completions.create,
            model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
            messages=conversation,
            functions=functions,
            function_call="auto",
            temperature=0.7,
            max_tokens=180
        )

        message = response.choices[0].message

        if message.function_call:
            function_name = message.function_call.name

            # SAFE: parse JSON args (no eval)
            try:
                function_args = json.loads(message.function_call.arguments or "{}")
            except Exception as e:
                function_args = {}
                logger.error(f"❌ Failed to parse function_call.arguments as JSON: {e}")

            logger.info(f"🔍 Calling function: {function_name} args keys: {list(function_args.keys())}")

            if function_name == "get_menu":
                function_result = await self.get_menu()
            elif function_name == "get_kitchen_status":
                function_result = await self.get_kitchen_status()
            elif function_name == "create_pos_order":
                function_result = await self.create_pos_order(function_args["order_draft"])
            else:
                function_result = {"error": "Unknown function"}

            conversation.append({
                "role": "function",
                "name": function_name,
                "content": json.dumps(function_result, ensure_ascii=False)
            })

            second_response = await asyncio.to_thread(
                self.openai_client.chat.completions.create,
                model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
                messages=conversation,
                temperature=0.7,
                max_tokens=180
            )

            final_message = second_response.choices[0].message.content
            conversation.append({"role": "assistant", "content": final_message})
            return final_message

        reply = message.content
        conversation.append({"role": "assistant", "content": reply})
        return reply

    async def text_to_speech(self, text: str) -> bytes:
        try:
            audio_generator = await asyncio.to_thread(
                self.elevenlabs.text_to_speech.convert,
                voice_id=self.voice_id,
                text=text,
                model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
            )
            audio_bytes = b""
            for chunk in audio_generator:
                audio_bytes += chunk
            return audio_bytes

        except Exception as e:
            logger.error(f"ElevenLabs TTS error: {e}, falling back to OpenAI")
            response = await asyncio.to_thread(
                self.openai_client.audio.speech.create,
                model="tts-1",
                voice="nova",
                input=text
            )
            return response.content
