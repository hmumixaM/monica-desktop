from __future__ import annotations

import random
import time
from pathlib import Path

import instaloader


class ConservativeRateController(instaloader.RateController):
    """Add a low request rate and stop immediately on HTTP 429."""

    def __init__(
        self,
        context: instaloader.InstaloaderContext,
        minimum_delay: float,
        maximum_delay: float,
        cooldown_file: Path,
    ) -> None:
        super().__init__(context)
        self.minimum_delay = minimum_delay
        self.maximum_delay = maximum_delay
        self.cooldown_file = cooldown_file
        self.last_query_at = 0.0

    def wait_before_query(self, query_type: str) -> None:
        super().wait_before_query(query_type)
        target_delay = random.uniform(self.minimum_delay, self.maximum_delay)
        elapsed = time.monotonic() - self.last_query_at
        remaining = max(0.0, target_delay - elapsed)
        if remaining > 0:
            self._context.log(
                f"Conservative request pause: {remaining:.1f} seconds."
            )
            self.sleep(remaining)
        self.last_query_at = time.monotonic()

    def handle_429(self, query_type: str) -> None:
        self.cooldown_file.parent.mkdir(parents=True, exist_ok=True)
        self.cooldown_file.write_text(
            f"{int(time.time())}\t{query_type}\n",
            encoding="utf-8",
        )
        raise instaloader.TooManyRequestsException(
            "Instagram returned HTTP 429. Stopped without retrying."
        )
