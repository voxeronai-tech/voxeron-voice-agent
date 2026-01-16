from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("taj-agent")

try:
    import yaml  # type: ignore
except Exception:
    yaml = None


# -------------------------
# Data model
# -------------------------
@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    tenant_name: str
    base_language: str
    supported_langs: Tuple[str, ...]
    tts_voices: Dict[str, str]
    tts_instructions: Dict[str, str]
    stt_prompt_base: str
    stt_prompt_max_items: int
    phonetics: Dict[str, Any]
    rules: Dict[str, Any]
    intents: Dict[str, Any]  # hydrated from intents.yaml
    aliases: Dict[str, Any]  # hydrated from aliases.json (optional)

# -------------------------
# Helpers
# -------------------------
def _read_json(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def norm_simple(s: str) -> str:
    """
    Lowercase + remove punctuation => spaces + collapse whitespace.
    Matches your baseline intent-ish normalization.
    """
    s = (s or "").lower()
    cleaned = []
    for ch in s:
        cleaned.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(cleaned).split()).strip()


def _flags_from_list(flags: Optional[List[str]]) -> int:
    f = 0
    for x in (flags or []):
        if x == "I":
            f |= re.IGNORECASE
        elif x == "M":
            f |= re.MULTILINE
    return f


# -------------------------
# TenantManager
# -------------------------
class TenantManager:
    """
    Loads tenant config from:
      <base_dir>/<tenant_id>/
        tenant.json
        phonetics.json
        rules.json
        intents.yaml (optional)
    """

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()
        self._cache: Dict[str, TenantConfig] = {}
        self._compiled_cache: Dict[Tuple[str, str], List[Tuple[re.Pattern, str]]] = {}
        # compiled_cache key: (tenant_id, lang) where lang in {"*", "en", "nl", ...}

        # intents cache: tenant_id -> (mtime, data)
        self._intents_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    def tenant_path(self, tenant_id: str) -> Path:
        return (self.base_dir / tenant_id).resolve()

    # -------------------------
    # intents.yaml loading (with mtime refresh)
    # -------------------------
    def _read_yaml(self, p: Path) -> Dict[str, Any]:
        if yaml is None:
            # no hard crash in demo mode; just log and behave as empty.
            logger.warning("PyYAML not installed; intents.yaml will be ignored. Install: pip install pyyaml")
            return {}
        raw = p.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        return data if isinstance(data, dict) else {}

    def _load_intents_yaml(self, tenant_id: str) -> Dict[str, Any]:
        tdir = self.tenant_path(tenant_id)
        intents_yaml = tdir / "intents.yaml"
        if not intents_yaml.exists():
            # cache empty with mtime=0 to avoid repeated fs checks
            self._intents_cache[tenant_id] = (0.0, {})
            return {}

        try:
            mtime = intents_yaml.stat().st_mtime
        except Exception:
            mtime = 0.0

        cached = self._intents_cache.get(tenant_id)
        if cached and cached[0] == mtime:
            return cached[1]

        # (re)load from disk
        try:
            data = self._read_yaml(intents_yaml)
        except Exception as e:
            logger.warning("Failed to load intents.yaml for %s: %s", tenant_id, e)
            data = {}

        # normalize top-level lang keys to lowercase (EN -> en)
        normed: Dict[str, Any] = {}
        for k, v in (data or {}).items():
            if isinstance(k, str):
                normed[k.strip().lower()] = v
            else:
                normed[str(k)] = v

        self._intents_cache[tenant_id] = (mtime, normed)
        return normed

    def get_intent_for_language(
        self,
        cfg: Optional[TenantConfig],
        lang: str,
        key: str,
        default: Optional[List[str]] = None,
    ) -> List[str]:
        """
        Returns a list of trigger strings for a given key in intents.yaml.

        Example keys:
          - replacement_triggers
          - affirmation_triggers
          - negation_triggers
          - more_triggers
          - order_summary_triggers
          - tasty_triggers
        """
        default = default or []
        if not cfg or not isinstance(getattr(cfg, "intents", None), dict):
            return default

        want = (lang or "").strip().lower() or (cfg.base_language or "en").strip().lower()

        def _get(lang_key: str) -> Optional[List[str]]:
            block = cfg.intents.get(lang_key)
            if not isinstance(block, dict):
                return None
            val = block.get(key)
            if isinstance(val, list):
                out: List[str] = []
                for x in val:
                    s = str(x).strip()
                    if s:
                        out.append(s)
                return out
            if isinstance(val, str):
                s = val.strip()
                return [s] if s else None
            return None

        # hierarchy: requested lang -> base lang -> en -> default
        for l in [want, (cfg.base_language or "en").strip().lower(), "en"]:
            v = _get(l)
            if v is not None:
                return v

        return default

    def load_tenant(self, tenant_id: str) -> TenantConfig:
        tenant_id = (tenant_id or "").strip()
        if not tenant_id:
            raise ValueError("tenant_id is empty")

        if tenant_id in self._cache:
            return self._cache[tenant_id]

        tdir = self.tenant_path(tenant_id)
        tenant_json = tdir / "tenant.json"
        phonetics_json = tdir / "phonetics.json"
        rules_json = tdir / "rules.json"

        if not tenant_json.exists():
            raise FileNotFoundError(f"Missing {tenant_json}")
        if not phonetics_json.exists():
            raise FileNotFoundError(f"Missing {phonetics_json}")
        if not rules_json.exists():
            raise FileNotFoundError(f"Missing {rules_json}")

        tenant = _read_json(tenant_json)
        phonetics = _read_json(phonetics_json)
        rules = _read_json(rules_json)

        aliases_json = tdir / "aliases.json"
        aliases: Dict[str, Any] = {}
        if aliases_json.exists():
            try:
                aliases = _read_json(aliases_json)
            except Exception as e:
                logger.warning("aliases.json load failed for %s: %s", tenant_id, e)
                aliases = {}

        tts = tenant.get("tts") or {}
        stt = tenant.get("stt") or {}

        # hydrate intents.yaml (safe fallback to {})
        intents: Dict[str, Any] = {}
        try:
            intents = self._load_intents_yaml(tenant_id)
        except Exception as e:
            logger.warning("intents.yaml hydration failed for %s: %s", tenant_id, e)
            intents = {}

        cfg = TenantConfig(
            tenant_id=tenant_id,
            tenant_name=str(tenant.get("tenant_name") or tenant_id),
            base_language=str(tenant.get("base_language") or "en").strip().lower(),
            supported_langs=tuple([str(x).strip().lower() for x in (tenant.get("supported_langs") or ["en"])]),
            tts_voices=dict((tts.get("voices") or {})),
            tts_instructions=dict((tts.get("instructions") or {})),
            stt_prompt_base=str(stt.get("prompt_base") or ""),
            stt_prompt_max_items=int(stt.get("prompt_max_items") or 0),
            phonetics=phonetics,
            rules=rules,
            intents=intents,
            aliases=aliases,
        )

        self._cache[tenant_id] = cfg

        # Invalidate compiled patterns for this tenant if any existed
        for k in list(self._compiled_cache.keys()):
            if k[0] == tenant_id:
                self._compiled_cache.pop(k, None)

        return cfg

    # Optional helper during live tuning: clear a single tenant cache
    def clear_tenant_cache(self, tenant_id: str) -> None:
        tenant_id = (tenant_id or "").strip()
        if not tenant_id:
            return
        self._cache.pop(tenant_id, None)
        self._intents_cache.pop(tenant_id, None)
        for k in list(self._compiled_cache.keys()):
            if k[0] == tenant_id:
                self._compiled_cache.pop(k, None)

    # -------------------------
    # Transcript aliases (tenant-scoped sanitization)
    # -------------------------
    def _aliases_for_lang(self, cfg: Optional[TenantConfig], lang: str) -> Dict[str, Any]:
        if not cfg:
            return {}
        a = cfg.aliases or {}
        if not isinstance(a, dict):
            return {}

        by_lang = a.get("by_lang")
        if isinstance(by_lang, dict):
            lang_key = (lang or "").lower().strip()
            merged: Dict[str, Any] = {}
            for k in ("exact", "regex"):
                if k in a:
                    merged[k] = a.get(k)
            lang_obj = by_lang.get(lang_key)
            if isinstance(lang_obj, dict):
                for k in ("exact", "regex"):
                    if k in lang_obj:
                        merged[k] = lang_obj.get(k)
            return merged

        return a
    def _compile_aliases(self, cfg: Optional[TenantConfig], lang: str) -> List[Tuple[re.Pattern, str]]:
        """
        Compile alias replacements into regex patterns.
        Cached per (tenant_id, lang).
        """
        if not cfg:
            return []

        cache_key = (cfg.tenant_id, f"alias:{lang}")
        if cache_key in self._compiled_cache:
            return self._compiled_cache[cache_key]

        aliases = self._aliases_for_lang(cfg, lang)
        compiled: List[Tuple[re.Pattern, str]] = []

        for src, dst in aliases.items():
            if not src or not dst:
                continue
            # word-boundary safe, normalized matching
            pat = re.compile(r"\b" + re.escape(src) + r"\b", re.IGNORECASE)
            compiled.append((pat, dst))

        self._compiled_cache[cache_key] = compiled
        return compiled

    def apply_aliases(self, cfg: Optional[TenantConfig], text: str, lang: str) -> str:
        """
        Apply tenant-scoped transcript aliases before intent parsing.
        """
        if not text or not cfg:
            return text

        out = text
        for pat, repl in self._compile_aliases(cfg, lang):
            if pat.search(out):
                logger.info("[ALIAS] %r -> %r", pat.pattern, repl)
                out = pat.sub(repl, out)

        return out

    def _compile_alias_rules(self, tenant_id: str, lang: str, aliases: Dict[str, Any]) -> List[Tuple[re.Pattern, str]]:
        cache_key = (tenant_id, f"aliases:{(lang or '*').lower()}")
        cached = self._compiled_cache.get(cache_key)
        if cached is not None:
            return cached

        rules: List[Tuple[re.Pattern, str]] = []

        exact = aliases.get("exact")
        if isinstance(exact, dict):
            for src, dst in exact.items():
                if not isinstance(src, str) or not isinstance(dst, str):
                    continue
                src_n = norm_simple(src)
                dst_n = norm_simple(dst)
                if not src_n:
                    continue
                pat = re.compile(rf"(?<!\w){re.escape(src_n)}(?!\w)", flags=re.UNICODE)
                rules.append((pat, dst_n))

        rx = aliases.get("regex")
        if isinstance(rx, list):
            for item in rx:
                if not isinstance(item, dict):
                    continue
                pat_s = item.get("pattern")
                repl = item.get("repl")
                flags = _flags_from_list(item.get("flags")) | re.UNICODE
                if isinstance(pat_s, str) and isinstance(repl, str) and pat_s.strip():
                    try:
                        rules.append((re.compile(pat_s, flags=flags), repl))
                    except Exception:
                        continue

        self._compiled_cache[cache_key] = rules
        return rules

    def apply_aliases(self, cfg: Optional[TenantConfig], text: str, lang: str) -> Tuple[str, List[Tuple[str, str]]]:
        """
        Deterministic transcript sanitization using tenant-scoped aliases.json.
        Returns (sanitized_text, applied_pairs).
        """
        raw = text or ""
        norm = norm_simple(raw)

        if not cfg or not norm:
            return raw, []

        aliases = self._aliases_for_lang(cfg, lang)
        if not aliases:
            return raw, []

        rules = self._compile_alias_rules(cfg.tenant_id, lang, aliases)
        if not rules:
            return raw, []

        out = norm
        applied: List[Tuple[str, str]] = []
        for pat, repl in rules:
            new = pat.sub(repl, out)
            if new != out:
                applied.append((pat.pattern, repl))
                out = new

        return out, applied

    # -------------------------
    # Normalization / phonetics
    # -------------------------
    def _compile_patterns(self, cfg: TenantConfig, lang_key: str) -> List[Tuple[re.Pattern, str]]:
        """
        Compile patterns for "*" or specific language from cfg.phonetics["patterns"].
        """
        cache_key = (cfg.tenant_id, lang_key)
        if cache_key in self._compiled_cache:
            return self._compiled_cache[cache_key]

        pat_root = (cfg.phonetics or {}).get("patterns") or {}
        rules = pat_root.get(lang_key) or []
        compiled: List[Tuple[re.Pattern, str]] = []

        for r in rules:
            if not isinstance(r, dict):
                continue
            pat = r.get("pattern")
            repl = r.get("replace", "")
            if not pat:
                continue
            flags = _flags_from_list(r.get("flags"))
            try:
                rx = re.compile(pat, flags=flags)
                compiled.append((rx, str(repl)))
            except re.error:
                # skip bad regex rather than crash tests
                continue

        self._compiled_cache[cache_key] = compiled
        return compiled

    def _apply_patterns(self, cfg: TenantConfig, lang: str, text: str) -> str:
        out = text
        for rx, repl in self._compile_patterns(cfg, "*"):
            out = rx.sub(repl, out)
        for rx, repl in self._compile_patterns(cfg, lang):
            out = rx.sub(repl, out)
        return out

    def _gate_naam_to_naan(self, cfg: TenantConfig, text: str) -> str:
        """
        Conservative gate:
        Replace 'naam/name' -> 'naan' only if:
          - quantity words present
          OR
          - restaurant intent markers like "graag naam", "naam erbij", "ik wil naam"
        """
        gates = (cfg.phonetics or {}).get("gates") or {}
        if not bool(gates.get("naam_to_naan", False)):
            return text

        norm = norm_simple(text)
        if not norm:
            return text

        qty_words = {"een", "twee", "drie", "vier", "vijf", "one", "two", "three", "four", "five"}
        toks = set(norm.split())
        has_qty = bool(toks & qty_words)

        intent_markers = (
            "graag naam",
            "naam erbij",
            "naam er bij",
            "ik wil naam",
            "wil graag naam",
            "ik wilde graag naam",
        )
        has_intent = any(m in norm for m in intent_markers)

        if has_qty or has_intent:
            out = re.sub(r"\bnaam\b", "naan", text, flags=re.IGNORECASE)
            out = re.sub(r"\bname\b", "naan", out, flags=re.IGNORECASE)
            return out

        return text

    def normalize_text(self, cfg: TenantConfig, lang: str, text: str) -> str:
        """
        Apply tenant phonetics rules and conservative gates.
        """
        lang = (lang or cfg.base_language or "en").strip().lower()
        if lang not in cfg.supported_langs:
            lang = cfg.base_language or "en"

        out = (text or "").strip()
        if not out:
            return out

        # Apply regex patterns: global "*" then language-specific
        out = self._apply_patterns(cfg, lang, out)

        # Apply gates
        out = self._gate_naam_to_naan(cfg, out)

        return out
    def strip_affirmation_prefix(
        self,
        cfg: Optional[TenantConfig],
        text: str,
        lang: str,
    ) -> Tuple[str, bool, str]:
        """
        Tenant-scoped prefix stripping:
        If the utterance starts with an affirmation trigger AND has remainder,
        strip the trigger and return the remainder so deterministic parsing can proceed.

        Examples (nl):
          "doe maar de chicken" -> "chicken" (stripped=True)
          "ja doe maar butter chicken" -> "butter chicken" (stripped=True)
          "doe maar" -> "doe maar" (stripped=False)  # no remainder
        """
        raw = (text or "").strip()
        if not raw:
            return raw, False, ""

        triggers = self.get_intent_for_language(cfg, lang, "affirmation_triggers", default=[])
        if not triggers:
            return raw, False, ""

        # Prefer longest match first ("ja doe maar" before "doe maar")
        trig_list = sorted(
            [t.strip() for t in triggers if (t or "").strip()],
            key=len,
            reverse=True,
        )

        low = raw.lower().strip()

        for trig in trig_list:
            tl = trig.lower().strip()
            if not tl:
                continue

            # Exact only -> do NOT strip (no remainder)
            if low == tl:
                return raw, False, trig

            if low.startswith(tl):
                # Boundary check: do NOT match inside a longer word (e.g. "oké" must NOT match trigger "ok")
                next_idx = len(tl)
                if next_idx < len(low):
                    nxt = low[next_idx]
                    # If the next char is a letter/number, then trigger is embedded in a word -> ignore
                    if nxt.isalnum():
                        continue

                after = raw[len(trig):].lstrip(" ,.!?:;")
                if not after:
                    return raw, False, trig

                # Optional: strip leading articles (nl/en)
                after2 = after
                after2_low = after2.lower()
                for art in ("de ", "het ", "een ", "the ", "a ", "an "):
                    if after2_low.startswith(art):
                        after2 = after2[len(art):].lstrip()
                        break

                return after2, True, trig

        return raw, False, ""

