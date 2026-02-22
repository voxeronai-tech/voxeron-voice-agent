# src/api/menu_store.py
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

from ..db.database import db  # ✅ reuse your existing global Database() instance
from .text import norm_text

logger = logging.getLogger(__name__)

_GENERIC_NAAN_ALIASES = {"naan", "nan", "naam"}


def _is_flavored_naan_item_name(name_norm: str) -> bool:
    return any(x in name_norm for x in ("garlic", "cheese", "keema", "peshawari", "butter", "boter", "knoflook"))


def _prefer_new_generic_naan_mapping(existing_name: str, new_name: str) -> bool:
    # Prefer mapping generic "naan/nan/naam" to the plain/regular naan (if present)
    if not _is_flavored_naan_item_name(existing_name):
        return False
    if not _is_flavored_naan_item_name(new_name):
        return True
    return False


@dataclass
class MenuItem:
    item_id: str
    name: str
    description: str
    price_pickup: float
    price_delivery: float
    category_id: Optional[int]
    is_available: bool
    tags: Dict[str, Any]
    customizable_spice: Optional[bool] = None
    default_spice_level: Optional[str] = None


@dataclass
class MenuSnapshot:
    tenant_id: str
    tenant_name: str
    default_language: str = "english"

    items_by_id: Dict[str, MenuItem] = field(default_factory=dict)
    name_choices: List[Tuple[str, str]] = field(default_factory=list)  # (norm_name, item_id)
    alias_map: Dict[str, str] = field(default_factory=dict)           # norm_alias -> item_id

    # Tenant ontology overrides (bridge until DB metadata exists)
    ontology_families: Dict[str, Any] = field(default_factory=dict)          # family_id -> def
    ontology_alias_to_family: Dict[str, str] = field(default_factory=dict)   # alias_norm -> family_id

    def family_label(self, family_id: str) -> str:
        fam = (self.ontology_families or {}).get(family_id) or {}
        aliases = fam.get("aliases") if isinstance(fam, dict) else None
        if isinstance(aliases, list) and aliases:
            a0 = str(aliases[0] or "").strip()
            return a0 or family_id
        return family_id

    def family_requires_variant(self, family_id: str) -> bool:
        fam = (self.ontology_families or {}).get(family_id) or {}
        if isinstance(fam, dict):
            rv = fam.get("requires_variant")
            return True if rv is None else bool(rv)
        return True

    def detect_family_mentions(self, transcript: str) -> List[str]:
        """
        Detect family mentions using tenant ontology aliases.
        Returns opaque family_ids in deterministic order of appearance.
        Longest-alias wins, no domain hardcoding.
        """
        tnorm = norm_text(transcript or "")
        if not tnorm:
            return []
        amap = self.ontology_alias_to_family or {}
        if not amap:
            return []

        t = f" {tnorm} "
        # longest match first to avoid "nan" stealing "naan", etc.
        pairs = sorted(amap.items(), key=lambda kv: len(kv[0] or ""), reverse=True)

        hits: List[tuple[int, str]] = []  # (pos, family_id)
        seen: set[str] = set()

        for alias_norm, fam_id in pairs:
            if not alias_norm or not fam_id:
                continue
            needle = f" {alias_norm} "
            pos = t.find(needle)
            if pos >= 0 and fam_id not in seen:
                seen.add(fam_id)
                hits.append((pos, fam_id))

        hits.sort(key=lambda x: x[0])
        return [fid for _pos, fid in hits]

    def display_name(self, item_id: str) -> str:
        it = self.items_by_id.get(item_id)
        return it.name if it else item_id

class MenuStore:
    """
    MenuStore loads a per-tenant snapshot from Neon and builds alias_map.

    IMPORTANT: This implementation matches YOUR DB schema:
      - tenants: tenant_id, tenant_ref, name, default_language
      - menu_items: name_en/name_nl, description_en/description_nl, tags, etc.
      - menu_item_aliases: item_id, alias_text, tenant_id (lang may exist, but not required)
    """

    def __init__(self, database_url: str = "", ttl_seconds: int = 180, schema: str = "public"):
        self.schema = schema or "public"
        self.ttl_seconds = int(ttl_seconds or 180)
        self._cache: Dict[Tuple[str, str], Tuple[float, MenuSnapshot]] = {}

        # If a DATABASE_URL is passed, ensure env matches what src/db/database.py expects.
        if database_url and not os.getenv("DATABASE_URL"):
            os.environ["DATABASE_URL"] = database_url

    async def start(self) -> None:
        await db.connect()

    async def close(self) -> None:
        await db.close()

    # -----------------------------
    # Ontology helpers (bridge phase)
    # -----------------------------

    def _build_family_alias_map(self, families: dict) -> Dict[str, str]:
        """
        Build alias_norm -> family_id map from rules.json ontology.
        Deterministic conflict handling: first-seen alias wins.
        """
        out: Dict[str, str] = {}
        if not isinstance(families, dict):
            return out

        for fam_id, fam_def in families.items():
            if not fam_id or not isinstance(fam_def, dict):
                continue
            aliases = fam_def.get("aliases")
            if not isinstance(aliases, list):
                continue
            for a in aliases:
                a_norm = norm_text(str(a or ""))
                if not a_norm:
                    continue
                out.setdefault(a_norm, str(fam_id))
        return out

    def _apply_rules_ontology(self, snap: MenuSnapshot, rules: Optional[dict]) -> None:
        """
        Attach tenant ontology overrides onto an existing MenuSnapshot.
        Safe to call repeatedly, last call wins.
        """
        rules_obj: dict = rules if isinstance(rules, dict) else {}
        onto = rules_obj.get("ontology") if isinstance(rules_obj.get("ontology"), dict) else {}
        fams = onto.get("families") if isinstance(onto.get("families"), dict) else {}

        snap.ontology_families = fams
        snap.ontology_alias_to_family = self._build_family_alias_map(fams)

    # -----------------------------
    # Snapshot
    # -----------------------------

    async def get_snapshot(
        self,
        tenant_ref: str,
        lang: str = "en",
        *,
        rules: Optional[dict] = None,
    ) -> Optional[MenuSnapshot]:
        now = time.time()
        cache_key = (tenant_ref, (lang or "en").lower())

        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < self.ttl_seconds:
            snap = cached[1]
            self._apply_rules_ontology(snap, rules)
            return snap

        await db.connect()
        assert db.pool is not None

        async with db.pool.acquire() as conn:
            tenant = await conn.fetchrow(
                f"""
                SELECT tenant_id, name, default_language
                FROM {self.schema}.tenants
                WHERE tenant_ref = $1
                """,
                tenant_ref,
            )
            if not tenant:
                return None

            snap = MenuSnapshot(
                tenant_id=str(tenant["tenant_id"]),
                tenant_name=str(tenant["name"]),
                default_language=str(tenant.get("default_language") or "english"),
            )

            # Attach ontology (from rules.json) AFTER snap exists
            self._apply_rules_ontology(snap, rules)

            # pick language columns
            lang_n = (lang or "en").lower()
            if lang_n == "nl":
                name_col = "name_nl"
                desc_col = "description_nl"
            else:
                name_col = "name_en"
                desc_col = "description_en"

            rows = await conn.fetch(
                f"""
                SELECT
                    item_id,
                    {name_col} AS name,
                    {desc_col} AS description,
                    price_pickup,
                    price_delivery,
                    category_id,
                    is_available,
                    tags,
                    customizable_spice,
                    default_spice_level
                FROM {self.schema}.menu_items
                WHERE tenant_id = $1 AND is_available = TRUE
                """,
                snap.tenant_id,
            )

            def _set_alias(alias_norm: str, item_id: str, item_name_norm: str) -> None:
                """
                Alias map is for concrete item_ids only. Family aliases live in ontology_alias_to_family.
                """
                if not alias_norm or len(alias_norm) < 3:
                    return

                if alias_norm in _GENERIC_NAAN_ALIASES:
                    # avoid mapping "naan" -> garlic naan if plain exists
                    if _is_flavored_naan_item_name(item_name_norm):
                        return
                    existing = snap.alias_map.get(alias_norm)
                    if existing and existing in snap.items_by_id:
                        existing_name = norm_text(snap.items_by_id[existing].name)
                        if not _prefer_new_generic_naan_mapping(existing_name, item_name_norm):
                            return

                snap.alias_map[alias_norm] = item_id

            for r in rows:
                item_id = str(r["item_id"])
                name = (r.get("name") or "").strip() or item_id
                description = (r.get("description") or "").strip()

                tags = r.get("tags") or {}
                if not isinstance(tags, dict):
                    tags = {}

                item = MenuItem(
                    item_id=item_id,
                    name=name,
                    description=description,
                    price_pickup=float(r.get("price_pickup") or 0),
                    price_delivery=float(r.get("price_delivery") or 0),
                    category_id=int(r["category_id"]) if r.get("category_id") is not None else None,
                    is_available=bool(r.get("is_available", True)),
                    tags=tags,
                    customizable_spice=bool(r.get("customizable_spice")) if r.get("customizable_spice") is not None else None,
                    default_spice_level=(str(r.get("default_spice_level")).strip() if r.get("default_spice_level") else None),
                )

                snap.items_by_id[item_id] = item
                name_norm = norm_text(name)
                snap.name_choices.append((name_norm, item_id))

                # name itself is an alias
                _set_alias(name_norm, item_id, name_norm)

                # tags.keywords (comma or newline separated)
                kws = tags.get("keywords") or ""
                if isinstance(kws, str) and kws.strip():
                    for part in kws.replace("\n", ",").split(","):
                        _set_alias(norm_text(part), item_id, name_norm)

                # tags.aliases (list)
                aliases = tags.get("aliases")
                if isinstance(aliases, list):
                    for a in aliases[:30]:
                        _set_alias(norm_text(str(a)), item_id, name_norm)

            # explicit alias table
            alias_rows = await conn.fetch(
                f"""
                SELECT item_id, alias_text
                FROM {self.schema}.menu_item_aliases
                WHERE tenant_id = $1
                """,
                snap.tenant_id,
            )

            for ar in alias_rows:
                iid = str(ar["item_id"])
                if iid not in snap.items_by_id:
                    continue
                alias = norm_text(ar.get("alias_text") or "")
                if not alias:
                    continue
                name_norm = norm_text(snap.items_by_id[iid].name)
                _set_alias(alias, iid, name_norm)

        self._cache[cache_key] = (now, snap)
        return snap