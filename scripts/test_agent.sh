#!/bin/bash

echo "🧪 Testing Voxeron Voice Agent..."
echo "=================================="

cd ~/projects/voxeron-voice-agent
source venv/bin/activate

# Test OpenAI key
python3 -c "
import os
import sys
sys.path.insert(0, '.')

# Load from .env file directly
with open('.env') as f:
    for line in f:
        if line.startswith('OPENAI_API_KEY'):
            key = line.split('=')[1].strip()
            print(f'✅ API key found: {key[:15]}...')
            break
"

# Test imports
python3 -c "
print('✅ Testing imports...')
from openai import OpenAI
print('✅ OpenAI imported')
from fastapi import FastAPI  
print('✅ FastAPI imported')
print('🎉 All tests passed!')
"

echo ""
echo "✅ Ready to start agent!"
