import asyncio
from types import SimpleNamespace

from src.api.session_controller import SessionController


# -------------------------
# Minimal fakes (RC3 scope)
# -------------------------

class FakeWS:
    async def send_json(self, *_a, **_k): return None
    async def send_text(self, *_a, **_k): return None
    async def send_bytes(self, *_a, **_k): return None


class FakeOrder:
    def __init__(self):
        self.items = {}

    def add(self, item_id, qty):
        self.items[item_id] = int(self.items.get(item_id, 0) + int(qty))

    def set_qty(self, item_id, qty):
        self.items[item_id] = int(qty)

    def summary(self, menu):
        parts = []
        for iid in sorted(self.items.keys()):
            parts.append(f"{self.items[iid]}x {menu.display_name(iid)}")
        return ", ".join(parts)


class FakeMenu:
    def __init__(self, items):
        self._items = items
        self.alias_map = {}
        self.name_choices = [(v, k) for k, v in items.items()]

    def display_name(self, item_id):
        return self._items.get(item_id, item_id)


class FakeOA:
    def __init__(self, transcript):
        self.transcript = transcript

    async def transcribe_pcm(self, *_a, **_k):
        return self.transcript

    async def chat(self, *_a, **_k):
        return '{"reply": "", "add": [], "remove": []}'


def _mk_controller_minimal() -> SessionController:
    ctrl = SessionController.__new__(SessionController)

    # Flags used by process_utterance
    ctrl.tenant_rules_enabled = False
    ctrl.tenant_stt_prompt_enabled = False
    ctrl.tenant_tts_instructions_enabled = False

    async def _noop(*_a, **_k):
        return None

    # Callbacks referenced in process_utterance
    ctrl.send_thinking = _noop
    ctrl.clear_thinking = _noop
    ctrl.send_user_text = _noop
    ctrl.send_agent_text = _noop
    ctrl.tts_end = _noop

    # Misc
    ctrl.enforce_output_language = lambda t, lang: t

    return ctrl


# ==========================================================
# RC3 tests
# ==========================================================

def test_pending_nan_variant_split_plain_garlic_sets_both():
    ctrl = _mk_controller_minimal()

    async def _noop(*_a, **_k):
        return None

    ctrl._speak = _noop

    ctrl._find_naan_item_for_variant = (
        lambda menu, v:
            "naan_plain" if v == "plain"
            else "naan_garlic" if v == "garlic"
            else None
    )
    ctrl._is_nan_item = lambda menu, iid: str(iid).startswith("naan")

    st = SimpleNamespace()
    st.menu = FakeMenu({"naan_plain": "Nan", "naan_garlic": "Nan Garlic"})
    st.order = FakeOrder()
    st.pending_choice = "nan_variant"
    st.pending_qty = 2
    st.nan_prompt_count = 0
    st.pending_cart_check = False
    st.cart_check_snapshot = ""
    st.lang = "en"

    ctrl.state = st

    ok = asyncio.run(ctrl._handle_pending_nan_variant(FakeWS(), "one plain naan and one garlic naan"))

    assert ok is True
    assert st.order.items["naan_plain"] == 1
    assert st.order.items["naan_garlic"] == 1


def test_extra_garlic_naan_increments_variant_only():
    ctrl = _mk_controller_minimal()

    spoken = {"text": None}

    async def _speak(_ws, text):
        spoken["text"] = text

    ctrl._speak = _speak

    ctrl._find_naan_item_for_variant = (
        lambda menu, v:
            "naan_garlic" if v == "garlic"
            else "naan_plain" if v == "plain"
            else None
    )

    st = SimpleNamespace()
    st.is_processing = False
    st.turn_id = 0
    st.lang = "en"
    st.lang_locked = True
    st.tenant_ref = "taj_mahal"
    st.tenant_cfg = None

    st.phase = "chat"

    st.menu = FakeMenu({"naan_plain": "Nan", "naan_garlic": "Nan Garlic"})
    st.order = FakeOrder()
    st.order.items = {"naan_plain": 1, "naan_garlic": 1}

    # Offer tracking defaults (process_utterance touches these)
    st.offered_item_id = None
    st.offered_ts = 0.0
    st.offered_label = None

    # Slot/flow flags referenced in process_utterance
    st.pending_choice = None
    st.pending_name = False
    st.pending_fulfillment = False
    st.pending_cart_check = False
    st.cart_check_snapshot = ""
    st.pending_confirm = False
    st.order_finalized = False
    st.fulfillment_mode = None
    st.customer_name = None

    st.pending_qty = 1
    st.pending_qty_hold = None
    st.pending_qty_deadline = 0.0
    st.last_added = []
    st.last_added_ts = 0.0

    ctrl.state = st
    ctrl.oa = FakeOA("add one extra garlic naan")

    asyncio.run(ctrl.process_utterance(FakeWS(), b"\x00" * 320))

    assert st.order.items["naan_garlic"] == 2
    assert st.order.items["naan_plain"] == 1
    assert spoken["text"] is not None


def test_checkout_intent_normalized_contractions():
    ctrl = SessionController.__new__(SessionController)

    assert ctrl._is_checkout_intent("No, that ll be all.") is True
    assert ctrl._is_checkout_intent("That s all") is True
    assert ctrl._is_checkout_intent("That s it") is True

    assert ctrl._is_checkout_intent("Dat is alles") is True
    assert ctrl._is_done_intent("Dat was alles") is True

    # Ordering intent (not necessarily checkout)
    assert ctrl._is_ordering_intent_global("Ik wil bestellen.") is True
