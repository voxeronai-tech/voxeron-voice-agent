from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_kitchen_status(
    tenant_id: str,
    wait_time_min: int,
    capacity_status: str,
    location_id: str | None = None,
    notes: str | None = None,
    blackout_items: Optional[List[str]] = None,
    updated_at: str | None = None,
) -> Dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "location_id": location_id,
        "wait_time_min": int(wait_time_min),
        "capacity_status": capacity_status,
        "notes": notes,
        "blackout_items": blackout_items or [],
        "updated_at": updated_at or _now_iso(),
    }


def make_menu(
    tenant_id: str,
    items: List[Dict[str, Any]],
    categories: Optional[List[Dict[str, Any]]] = None,
    currency: str = "EUR",
    location_id: str | None = None,
    updated_at: str | None = None,
) -> Dict[str, Any]:
    """
    Must match:
      architecture/schemas/domain/menu.v0.6.json

    Notes:
      - Top-level fields allowed: tenant_id, location_id, currency, items, updated_at
      - No top-level `categories` or `generated_at` in v0.6 contract
      - MenuItem requires: item_id, name, price(Money), available
    """
    # Build category_id -> name map for item.category (string|null)
    cat_map: Dict[Any, str] = {}
    for c in (categories or []):
        cid = c.get("category_id") or c.get("id")
        name = c.get("name_en") or c.get("name")
        if cid is not None and name:
            cat_map[str(cid)] = str(name)

    menu_items: List[Dict[str, Any]] = []
    for r in items:
        item_id = str(r.get("item_id") or "")
        name = (r.get("name_en") or r.get("name") or "").strip()
        desc = (r.get("description_en") or r.get("description") or None)
        cat_id = r.get("category_id") or r.get("category") or None
        cat_name = cat_map.get(str(cat_id)) if cat_id is not None else None

        # Availability heuristic:
        # - If DB has explicit availability/available fields, use them
        # - Otherwise default True (demo)
        if "available" in r:
            available = bool(r.get("available"))
        elif "availability" in r:
            available = bool(r.get("availability"))
        else:
            available = True

        # Money object required by schema
        price_amount = float(r.get("price_delivery") or r.get("price") or 0.0)

        menu_items.append(
            {
                "item_id": item_id,
                "name": name if name else item_id,
                "description": desc,
                "price": {"amount": price_amount, "currency": currency},
                "available": available,
                "tags": [],        # optional, but safe and schema-valid
                "allergens": [],   # optional, but safe and schema-valid
                "category": cat_name,
            }
        )

    return {
        "tenant_id": tenant_id,
        "location_id": location_id,
        "currency": currency,
        "items": menu_items,
        "updated_at": updated_at or _now_iso(),
    }
