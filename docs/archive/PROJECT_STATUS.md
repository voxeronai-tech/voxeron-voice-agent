
---

## 🌍 KILLER FEATURE: MULTILINGUAL SUPPORT

### Languages Supported
1. **Hindi-English Mix** (Hinglish) - DEFAULT
2. **Dutch** (Netherlands)
3. **Pure Hindi**

### Implementation Strategy

#### Greeting Flow
```
Agent: "Namaste! Welcome to Taj Mahal. 
        For English, say 'English'.
        Voor Nederlands, zeg 'Nederlands'.
        हिंदी के लिए, 'हिंदी' कहें."

User: "English" / "Nederlands" / "हिंदी"

Agent: [Switches language and continues]
```

#### Language Detection & Switching
- Detect language from user's first response
- Store language preference in session
- All subsequent responses in chosen language
- Allow mid-conversation switching: "Switch to Dutch"

---

### PHASE 0: Multilingual Foundation (NEW - PRIORITY)

**Goal:** Support 3 languages with seamless switching

#### Step 0.1 - Language State Management
**Add to VoiceAgent:**
```python
self.session_language = {}  # Track language per session
self.supported_languages = {
    'hinglish': {
        'code': 'hi-en',
        'voice_id': 'wlmwDR77ptH6bKHZui0l',  # Indian English
        'greeting': 'Namaste! Welcome to Taj Mahal...',
        'system_prompt': 'You speak Hindi-English mix (Hinglish)...'
    },
    'dutch': {
        'code': 'nl',
        'voice_id': '[DUTCH_VOICE_ID]',
        'greeting': 'Welkom bij Taj Mahal...',
        'system_prompt': 'Je spreekt Nederlands...'
    },
    'hindi': {
        'code': 'hi',
        'voice_id': '[HINDI_VOICE_ID]',
        'greeting': 'ताज महल में आपका स्वागत है...',
        'system_prompt': 'आप हिंदी में बात करते हैं...'
    }
}
self.default_language = 'hinglish'  # Configurable
```

#### Step 0.2 - Language Detection
- Parse first user utterance for language keywords
- Update session language
- Regenerate system prompt in chosen language

#### Step 0.3 - Dynamic TTS Voice Selection
- Use different ElevenLabs voice per language
- Dutch voice for Dutch
- Hindi voice for Hindi
- Indian English (current) for Hinglish

#### Step 0.4 - Multilingual Menu
- Menu items have translations:
  - `name_en`: "Butter Chicken"
  - `name_nl`: "Boter Kip" 
  - `name_hi`: "मक्खन चिकन"
- Respond in user's language
- Prices remain same regardless of language

#### Step 0.5 - Configuration
**Add to .env:**
```bash
DEFAULT_LANGUAGE=hinglish  # or 'dutch' or 'hindi'
ELEVENLABS_VOICE_HINGLISH=wlmwDR77ptH6bKHZui0l
ELEVENLABS_VOICE_DUTCH=[to_be_configured]
ELEVENLABS_VOICE_HINDI=[to_be_configured]
```

---

### Updated Roadmap Priority

**NEW ORDER:**
1. **Phase 0:** Multilingual Support (FOUNDATION)
2. **Phase 1:** Order State Management
3. **Phase 2:** Database Integration (with translations)
4. **Phase 3:** Intelligent Conversation
5. **Phase 4:** Production Polish
6. **Phase 5:** Demo Ready

**Rationale:** Multilingual is the **killer differentiator**. Build it first so all subsequent features work multilingually from the start.

---

### Database Schema Updates Needed

**Add to menu_items table:**
- ✅ `name_nl` - Already exists!
- ✅ `name_en` - Already exists!
- ❌ `name_hi` - Need to add (Hindi translations)
- ❌ `description_hi` - Need to add

**Add to menu_categories:**
- ✅ `name_nl` - Already exists!
- ✅ `name_en` - Already exists!
- ❌ `name_hi` - Need to add

**Migration:**
```sql
ALTER TABLE menu_items 
ADD COLUMN name_hi VARCHAR(255),
ADD COLUMN description_hi TEXT;

ALTER TABLE menu_categories
ADD COLUMN name_hi VARCHAR(255),
ADD COLUMN description_hi TEXT;
```

---

### Testing Multilingual

**Test Scenarios:**

**Scenario 1: Dutch Customer**
```
Agent: "Namaste! Welcome... Voor Nederlands, zeg 'Nederlands'..."
User: "Nederlands"
Agent: "Uitstekend! Wat wilt u bestellen?"
User: "Butter chicken"
Agent: "Prima keuze! Wilt u het mild, medium, of pittig?"
```

**Scenario 2: Hindi Customer**
```
Agent: "Namaste! Welcome... हिंदी के लिए, 'हिंदी' कहें"
User: "हिंदी"
Agent: "बहुत अच्छा! आप क्या ऑर्डर करना चाहेंगे?"
User: "Butter chicken"
Agent: "शानदार! आप मसाला कितना तीखा चाहेंगे?"
```

**Scenario 3: Mid-Conversation Switch**
```
Agent: [Speaking Dutch]
User: "Switch to English"
Agent: "Of course! Continuing in English. What would you like to order?"
```

---

### ElevenLabs Voice IDs Needed

**Current:**
- ✅ Indian English (Hinglish): `wlmwDR77ptH6bKHZui0l`

**Need to Configure:**
- ❓ Dutch voice: [Browse ElevenLabs voice library]
- ❓ Hindi voice: [Browse ElevenLabs voice library]

**Action Item:** Test and select appropriate voices for each language.

---

### Success Criteria for Phase 0

**Multilingual Complete When:**
- ✅ Agent greets with language options
- ✅ User can choose language
- ✅ All responses in chosen language
- ✅ Menu items shown in chosen language
- ✅ Can switch languages mid-conversation
- ✅ Each language uses appropriate voice
- ✅ Default language configurable via .env

