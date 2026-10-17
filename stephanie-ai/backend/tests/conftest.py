import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Tests never talk to real providers.
for var in ("ELEVENLABS_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
            "STEPHANIE_HUMAN_APPROVAL_TOKEN", "STEPHANIE_ADMIN_TOKEN"):
    os.environ.pop(var, None)
os.environ["STEPHANIE_SAMPLE_RATE"] = "16000"   # keep synthesis fast in tests


@pytest.fixture
def config(tmp_path):
    from config import StephanieConfig

    cfg = StephanieConfig()
    cfg.SAMPLE_RATE = 16000
    cfg.DATA_DIR = str(tmp_path / "data")
    return cfg


@pytest.fixture
def engine(tmp_path):
    from models.text_processor import TextProcessor
    from models.voice_engine import StephanieVoiceEngine

    return StephanieVoiceEngine(sample_rate=16000, text_processor=TextProcessor(),
                                library_path=str(tmp_path / "library.json"))


@pytest.fixture
def stephanie(config):
    from services.stephanie_ai import StephanieAI

    return StephanieAI(config)


@pytest.fixture
def client(config):
    from fastapi.testclient import TestClient

    import main
    from services.stephanie_ai import StephanieAI

    main.app.state.stephanie = StephanieAI(config)
    with TestClient(main.app) as c:
        yield c
