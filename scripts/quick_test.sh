#!/bin/bash
cd ~/projects/voxeron-voice-agent
source venv/bin/activate

echo "🎤 Recording in 3 seconds..."
echo "Say: 'Ik wil graag reserveren'"
sleep 3
echo "🔴 SPEAK NOW!"

arecord -D plughw:CARD=Audio,DEV=0 -d 5 -f S16_LE -r 16000 -c 1 data/recordings/test.wav

echo ""
echo "📞 Processing..."
curl -s -X POST http://localhost:8000/process-call \
  -F "audio=@data/recordings/test.wav" \
  -o data/audio/response.mp3

echo "✅ Done!"
echo "🔊 Playing..."
mpg123 data/audio/response.mp3
