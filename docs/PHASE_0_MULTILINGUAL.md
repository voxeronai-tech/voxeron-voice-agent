# Phase 0: Multilingual Implementation Guide

## 🎯 Goal
Support Hindi-English (Hinglish), Dutch, and Hindi with seamless language switching.

---

## 📋 Implementation Steps

### Step 0.1: Add Language Configuration

**File:** `src/agent/voice_agent.py`

Add language definitions:
```python
LANGUAGE_CONFIG = {
    'hinglish': {
        'code': 'hi-en',
        'voice_id': 'wlmwDR77ptH6bKHZui0l',
        'greeting': (
            "Namaste! Welcome to Taj Mahal restaurant. "
            "For English, say 'English'. "
            "Voor Nederlands, zeg 'Nederlands'. "
            "हिंदी के लिए, 'हिंदी' कहें."
        ),
        'system_prompt': (
            "You are Maya, speaking Hindi-English mix (Hinglish). "
            "Use phrases like 'bahut accha', 'kya chahiye', mixed naturally. "
            "Keep responses warm and Indian."
        )
    },
    'dutch': {
        'code': 'nl',
        'voice_id': '[TO_CONFIGURE]',
        'greeting': (
            "Welkom bij Taj Mahal restaurant! "
            "Wat wilt u bestellen?"
        ),
        'system_prompt': (
            "Je bent Maya, een vriendelijke medewerker van Taj Mahal. "
            "Spreek in natuurlijk Nederlands. "
            "Blijf behulpzaam en professioneel."
        )
    },
    'hindi': {
        'code': 'hi',
        'voice_id': '[TO_CONFIGURE]',
        'greeting': (
            "नमस्ते! ताज महल रेस्तरां में आपका स्वागत है। "
            "आप क्या ऑर्डर करना चाहेंगे?"
        ),
        'system_prompt': (
            "आप माया हैं, ताज महल रेस्तरां की मित्रवत कर्मचारी। "
            "हिंदी में स्वाभाविक रूप से बात करें। "
            "सहायक और पेशेवर रहें।"
        )
    }
}
```

### Step 0.2: Language Detection

Add method to detect language:
```python
def detect_language(self, text: str) -> str:
    """Detect language from keywords"""
    text_lower = text.lower()
    
    if 'english' in text_lower:
        return 'hinglish'
    elif 'nederlands' in text_lower or 'dutch' in text_lower:
        return 'dutch'
    elif 'hindi' in text_lower or 'हिंदी' in text_lower:
        return 'hindi'
    
    return None  # No language detected
```

### Step 0.3: Session Language Management
```python
def __init__(self):
    # ... existing code ...
    self.session_language = {}  # Track language per session
    self.default_language = os.getenv('DEFAULT_LANGUAGE', 'hinglish')

def set_session_language(self, session_id: str, language: str):
    """Set language for session"""
    if language in LANGUAGE_CONFIG:
        self.session_language[session_id] = language
        logger.info(f"Session {session_id} language: {language}")

def get_session_language(self, session_id: str) -> str:
    """Get session language (default if not set)"""
    return self.session_language.get(session_id, self.default_language)
```

### Step 0.4: Update TTS to Use Language-Specific Voice
```python
async def synthesize_tts(self, text: str, session_id: str) -> bytes:
    """Generate TTS with language-specific voice"""
    language = self.get_session_language(session_id)
    voice_id = LANGUAGE_CONFIG[language]['voice_id']
    
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            # ... rest of implementation
        )
```

### Step 0.5: Update Chat Response with Language-Specific System Prompt
```python
async def generate_chat_response(self, session_id: str, text: str) -> str:
    language = self.get_session_language(session_id)
    system_prompt = LANGUAGE_CONFIG[language]['system_prompt']
    
    messages = [
        {"role": "system", "content": system_prompt}
    ] + self.history.get(session_id, [])[-8:]
    
    # ... rest of implementation
```

---

## 🧪 Testing Checklist

- [ ] Default greeting offers 3 language options
- [ ] "English" switches to Hinglish
- [ ] "Nederlands" switches to Dutch
- [ ] "हिंदी" switches to Hindi
- [ ] Subsequent responses in chosen language
- [ ] Voice changes per language
- [ ] Can switch mid-conversation
- [ ] Language persists through session
- [ ] DEFAULT_LANGUAGE env var works

---

## 🚀 Deployment Checklist

- [ ] Configure Dutch voice in ElevenLabs
- [ ] Configure Hindi voice in ElevenLabs
- [ ] Add voice IDs to .env
- [ ] Test all 3 languages end-to-end
- [ ] Update menu with Hindi translations (Phase 2)

