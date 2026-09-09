"""Health-aware selection across Overpass mirrors.

Public mirrors vary enormously and unpredictably. Measured within a single
minute: overpass-api.de refused connections outright, kumi.systems answered a
real tile query in 38-176 s. Always starting at the first configured endpoint
means every request pays the cost of whichever mirror happens to be worst.

This keeps a small amount of state per endpoint -- recent latency and a cooldown
after failures -- so requests go to whichever mirror is actually working, and
spreads concurrent work across mirrors instead of queueing on one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class EndpointHealth:
    url: str
    # Seeded optimistically so an untried endpoint is attempted early; the first
    # real measurement replaces most of this.
    latency_s: float = 5.0
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    successes: int = 0
    failures: int = 0

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until


@dataclass
class EndpointPool:
    """Ordering and failure tracking over a fixed set of mirrors."""

    urls: list[str]
    base_cooldown_s: float = 30.0
    max_cooldown_s: float = 600.0
    smoothing: float = 0.4
    # Spread work only across mirrors of comparable speed. Rotating blindly is
    # actively harmful when one mirror is far faster than the rest: most tiles
    # start on a slow one and pay the full hedge delay before reaching the fast
    # one. Measured, that turned a ~17 s search into 180 s.
    peer_factor: float = 3.0
    _health: dict[str, EndpointHealth] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.urls:
            raise ValueError("at least one Overpass endpoint is required")
        self._health = {u: EndpointHealth(u) for u in self.urls}

    def health(self, url: str) -> EndpointHealth:
        return self._health[url]

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "url": h.url,
                "latency_s": round(h.latency_s, 2),
                "available": h.available(now),
                "cooldown_s": max(0.0, round(h.cooldown_until - now, 1)),
                "successes": h.successes,
                "failures": h.failures,
            }
            for h in self._health.values()
        ]

    def ranked(self) -> list[str]:
        """Available endpoints, fastest first, then those in cooldown.

        Endpoints in cooldown are kept as a last resort rather than removed: if
        every mirror is struggling, trying a slow one beats failing outright.
        """
        now = time.monotonic()
        ok = [h for h in self._health.values() if h.available(now)]
        cooling = [h for h in self._health.values() if not h.available(now)]
        ok.sort(key=lambda h: h.latency_s)
        cooling.sort(key=lambda h: h.cooldown_until)
        return [h.url for h in ok] + [h.url for h in cooling]

    def order_for(self, worker: int) -> list[str]:
        """Attempt order for one unit of work, rotated by `worker`.

        Rotation spreads simultaneous requests instead of queueing them all on
        one mirror -- but only across mirrors that are actually competitive.
        A mirror more than `peer_factor` slower than the best is never chosen
        first; it stays in the list as a fallback.
        """
        ranked = self.ranked()
        now = time.monotonic()
        available = [u for u in ranked if self._health[u].available(now)]
        if len(available) <= 1:
            return ranked

        best = self._health[available[0]].latency_s
        peers = [u for u in available if self._health[u].latency_s <= best * self.peer_factor]
        rest = [u for u in ranked if u not in peers]
        if len(peers) <= 1:
            return peers + rest

        shift = worker % len(peers)
        return peers[shift:] + peers[:shift] + rest

    def record_success(self, url: str, latency_s: float) -> None:
        h = self._health[url]
        h.latency_s = (1 - self.smoothing) * h.latency_s + self.smoothing * latency_s
        h.consecutive_failures = 0
        h.cooldown_until = 0.0
        h.successes += 1

    def record_failure(self, url: str, latency_s: float | None = None) -> None:
        h = self._health[url]
        h.consecutive_failures += 1
        h.failures += 1
        # Exponential backoff per endpoint, so a dead mirror is quickly skipped
        # rather than retried on every single tile.
        cooldown = min(
            self.base_cooldown_s * (2 ** (h.consecutive_failures - 1)),
            self.max_cooldown_s,
        )
        h.cooldown_until = time.monotonic() + cooldown
        if latency_s is not None:
            h.latency_s = max(h.latency_s, latency_s)
