"""Stephanie.ai Voice — desktop client entry point.

    cd stephanie-ai/frontend
    pip install -r requirements.txt
    python main.py [--api http://localhost:8000]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui.app import StephanieAIApp  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Stephanie.ai Voice desktop client")
    parser.add_argument("--api", default=None, help="Backend base URL (default: saved setting or http://localhost:8000)")
    args = parser.parse_args()
    app = StephanieAIApp(api_base_url=args.api)
    app.run()


if __name__ == "__main__":
    main()
