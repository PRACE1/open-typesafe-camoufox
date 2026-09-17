"""Google fact demo: the EXACT command shape to see the full decision feed.

  uv run python -m src.run.run \
    --url https://www.google.com \
    --task "Go to Google. In the search box type ONE surprising fact you would like to know about the moon, press Enter, read the top result from PAGE TEXT, put the fact in note, and mark done." \
    --budget 240 --max-steps 20

Run this file for the same demo without typing it:
  uv run python run_fact.py
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.runner import run_decide_session

TASK = (
    "Go to Google. In the search box type ONE surprising fact you would like to "
    "know about the moon, press Enter, read the top result from PAGE TEXT, put the "
    "fact in note, and mark done."
)

if __name__ == "__main__":
    result = asyncio.run(
        run_decide_session(
            start_url="https://www.google.com",
            task=TASK,
            budget_s=240,
            max_steps=20,
            headless=False,
        )
    )
    print("RESULT:", {k: v for k, v in result.items() if k not in ("deps", "cursor")})