from __future__ import annotations

from src.api.menu_store import MenuSnapshot, MenuItem
from src.api.text import norm_text

def taj_minimal_menu_snapshot(lang: str = "en") -> MenuSnapshot:
    snap = MenuSnapshot(
        tenant_id="test-tenant-id",
        tenant_name="Taj Mahal (test)",
        default_language="english",
    )

    # Minimal items needed to reproduce the bug
    items = {
        "butter_chicken": MenuItem(
            item_id="butter_chicken",
            name="Butter Chicken",
            description="",
            price_pickup=0.0,
            price_delivery=0.0,
            category_id=None,
            is_available=True,
            tags={},
        ),
        # Biryani leaves (so choosing “lamb” can map to a leaf)
        "lamb_biryani": MenuItem(
            item_id="lamb_biryani",
            name="Lamb Biryani",
            description="",
            price_pickup=0.0,
            price_delivery=0.0,
            category_id=None,
            is_available=True,
            tags={},
        ),
        "chicken_biryani": MenuItem(
            item_id="chicken_biryani",
            name="Chicken Biryani",
            description="",
            price_pickup=0.0,
            price_delivery=0.0,
            category_id=None,
            is_available=True,
            tags={},
        ),
    }

    snap.items_by_id.update(items)

    # name_choices + alias_map (parser uses these)
    for iid, it in items.items():
        n = norm_text(it.name)
        snap.name_choices.append((n, iid))
        snap.alias_map[n] = iid

    # Also allow “biryani” as a head token (but not a leaf)
    # Disambiguation code checks ambiguity_options["biryani"] for options list.
    snap.ambiguity_options["biryani"] = [
        "lamb", "chicken", "tikka", "mix", "mushroom", "prawn", "vegetarian"
    ]

    return snap
