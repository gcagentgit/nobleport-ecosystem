import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for var in ("ELEVENLABS_API_KEY", "STEPHANIE_ELEVENLABS_VOICE_ID", "STEPHANIE_ELEVENLABS_VOICE_NAME", "ELEVENLABS_MODEL_ID"):
    os.environ.pop(var, None)
