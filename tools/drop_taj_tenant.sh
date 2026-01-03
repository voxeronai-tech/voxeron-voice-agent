#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p tenants/taj_mahal

cat > tenants/taj_mahal/tenant.json <<'JSON'
{
  "tenant_name": "Taj Mahal Restaurant",
  "base_language": "en",
  "supported_langs": ["en", "nl"],
  "tts": {
    "voices": { "en": "cedar", "nl": "marin" },
    "instructions": {
      "en": "Speak in English with a warm, natural male Indian accent. Friendly restaurant host. Not robotic.",
      "nl": "Spreek vloeiend Nederlands met een natuurlijke Nederlandse uitspraak. Vriendelijk en niet robotachtig."
    }
  },
  "stt": {
    "prompt_base": "Taj Mahal Indian restaurant. Menu vocabulary: butter chicken, chicken tikka masala, biryani, samosa, naan, garlic naan, rice, lamb rogan josh, lamb vindaloo, paneer. Languages: Nederlands, English.",
    "prompt_max_items": 12
  }
}
JSON

cat > tenants/taj_mahal/phonetics.json <<'JSON'
{
  "gates": {
    "naam_to_naan": true
  },
  "patterns": {
    "*": [
      { "pattern": "\\bnederl[âa]ns\\b", "replace": "Nederlands", "flags": ["I"] },
      { "pattern": "\\bnederlants\\b", "replace": "Nederlands", "flags": ["I"] },
      { "pattern": "\\bnee\\s*de\\s*lons\\b", "replace": "Nederlands", "flags": ["I"] }
    ],
    "nl": [
      { "pattern": "\\breis\\b", "replace": "rijst", "flags": ["I"] },
      { "pattern": "\\brijs\\b", "replace": "rijst", "flags": ["I"] },
      { "pattern": "\\blamp\\b", "replace": "lamb", "flags": ["I"] },
      { "pattern": "\\blam\\b", "replace": "lamb", "flags": ["I"] }
    ]
  }
}
JSON

cat > tenants/taj_mahal/rules.json <<'JSON'
{
  "upsell": {
    "enabled": true,
    "avoid_if_already_in_order": ["naan", "rijst", "rice"]
  },
  "heartbeat": {
    "idle_seconds": 10,
    "en": "Still there? What would you like to order next?",
    "nl": "Ben je er nog? Wat wil je hierna bestellen?"
  }
}
JSON

echo "✅ Dropped tenants/taj_mahal/*"
