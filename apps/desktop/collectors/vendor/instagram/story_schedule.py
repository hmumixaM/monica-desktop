"""Bounded startup jitter that never clears collection protection markers."""
from pathlib import Path
import random
import time


def collection_blocked(state: Path) -> bool:
    if (state / "COOLDOWN_REQUIRED").exists():
        return True
    pause = state / "COLLECTION_PAUSE_UNTIL"
    return pause.exists() and float(pause.read_text().split("\t", 1)[0]) > time.time()


def wait_for_story_run(state: Path, minimum: float, maximum: float) -> bool:
    if not 0 <= minimum <= maximum <= 3600:
        raise ValueError("Story startup delay must satisfy 0 <= min <= max <= 3600")
    if collection_blocked(state):
        return False
    delay = random.uniform(minimum, maximum)
    print(f"STORY_START_DELAY seconds={delay:.1f}", flush=True)
    deadline = time.monotonic() + delay
    while True:
        if collection_blocked(state):
            print("Story startup cancelled: collection protection is active.", flush=True)
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(remaining, 30))
