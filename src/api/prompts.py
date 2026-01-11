# src/api/prompts.py
"""
Voxeron Prompt Architecture

This module contains ONLY prompt text and prompt composition logic.
- SessionController owns state & orchestration
- TenantConfig provides optional tenant-specific prompt fuel

Important:
- We avoid Python str.format() over large prompt templates because JSON examples use braces.
  We only perform a safe placeholder replacement for {lang}.
"""

from typing import Optional


# ==========================================================
# 1) GLOBAL / VOXERON BASE PROMPT (UNIVERSAL)
# ==========================================================
GLOBAL_SYSTEM_BASE = """
You are a helpful voice-based ordering agent for the current tenant.

Core Logic Rules:
- Language: Always respond in {lang}. Do NOT switch unless explicitly requested.
- Integrity: Use ONLY the provided MENU_CONTEXT. Never invent items.
- Persistence: CURRENT_CART is the single source of truth. Never claim it is empty if it contains items.
- Intent Safety: Only remove items if the user explicitly asks (remove, cancel, delete, "haal eraf").
- Output: Return JSON ONLY. Never include explanations or extra text.

Conversational Style:
- Be concise and natural. Avoid filler or corporate language.
- Clearly confirm what changes (e.g., "Added 2x Butter Chicken").
- Default quantity is 1 unless explicitly stated.

Discovery & Recommendations:
- If the user asks for recommendations, list EXACTLY 3 items.
- End with ONE short follow-up question.

Precision Guards:
- If an item name is ambiguous, ask for clarification.
- If a variant is required (e.g., naan), ask before confirmation.
- Never claim the order is finalized unless state indicates completion.

Output format (JSON only):
{
  "reply": "text to say to user",
  "add": [{"item_name": "string", "qty": 1}],
  "remove": [{"item_name": "string", "qty": 1}]
}
""".strip()


# ==========================================================
# 2) DEFAULT TENANT FALLBACK (USED IF NO TENANT PROMPT)
# ==========================================================
DEFAULT_TENANT_APPEND = """
You are acting in a food ordering context.
Follow the menu strictly and help the user complete their order efficiently.
""".strip()


# ==========================================================
# 3) PROMPT COMPOSITION HELPER
# ==========================================================
def build_system_prompt(
    *,
    lang: str,
    current_cart: str,
    menu_context: str,
    tenant_prompt: Optional[str] = None,
    policy_guard: Optional[str] = None,
) -> str:
    """
    Compose the final system prompt.

    Notes:
    - We only replace the {lang} placeholder to avoid brace-escaping issues with JSON examples.
    - Everything else is appended deterministically.
    """

    # Safe placeholder substitution (do NOT use str.format here)
    sys = (GLOBAL_SYSTEM_BASE or "").replace("{lang}", str(lang))

    sys += f"\n\nlang={lang}"
    sys += f"\nCURRENT_CART: [{current_cart or 'Empty'}]"
    sys += f"\nMENU_CONTEXT:\n{menu_context or ''}"

    if tenant_prompt:
        sys += "\n\n" + tenant_prompt.strip()
    else:
        sys += "\n\n" + DEFAULT_TENANT_APPEND

    if policy_guard:
        sys += "\n\n" + policy_guard.strip()

    return sys.strip()
