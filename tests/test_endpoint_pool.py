"""Health tracking, ordering, and hedging across Overpass mirrors."""

import asyncio
import time

import pytest

from app import config, overpass
from app.endpoints import EndpointPool

A, B, C = "https://a.test", "https://b.test", "https://c.test"


class TestOrdering:
    def test_fastest_first(self):
        pool = EndpointPool([A, B, C])
        pool.record_success(A, 9.0)
        pool.record_success(B, 0.5)
        pool.record_success(C, 4.0)
        assert pool.ranked()[0] == B

    def test_failed_endpoint_goes_to_the_back_not_away(self):
        """A struggling mirror is a last resort — better than failing outright."""
        pool = EndpointPool([A, B])
        pool.record_failure(A)
        order = pool.ranked()
        assert order[-1] == A
        assert set(order) == {A, B}

    def test_repeated_failures_lengthen_the_cooldown(self):
        pool = EndpointPool([A, B], base_cooldown_s=10.0)
        pool.record_failure(A)
        first = pool.health(A).cooldown_until
        pool.record_failure(A)
        assert pool.health(A).cooldown_until > first

    def test_cooldown_is_capped(self):
        pool = EndpointPool([A], base_cooldown_s=10.0, max_cooldown_s=25.0)
        for _ in range(10):
            pool.record_failure(A)
        assert pool.health(A).cooldown_until - time.monotonic() <= 25.5

    def test_success_clears_a_cooldown(self):
        pool = EndpointPool([A, B])
        pool.record_failure(A)
        assert not pool.health(A).available(time.monotonic())
        pool.record_success(A, 1.0)
        assert pool.health(A).available(time.monotonic())

    def test_latency_is_smoothed_not_replaced(self):
        """One slow reply should not permanently condemn a good mirror."""
        pool = EndpointPool([A])
        pool.record_success(A, 1.0)
        settled = pool.health(A).latency_s
        pool.record_success(A, 100.0)
        assert settled < pool.health(A).latency_s < 100.0


class TestLoadSpreading:
    def test_workers_start_on_different_mirrors(self):
        """Concurrent tiles must not all queue behind the single fastest one."""
        pool = EndpointPool([A, B, C])
        for u in (A, B, C):
            pool.record_success(u, 1.0)
        firsts = {pool.order_for(i)[0] for i in range(3)}
        assert len(firsts) == 3

    def test_every_mirror_still_reachable_from_any_worker(self):
        pool = EndpointPool([A, B, C])
        for i in range(3):
            assert set(pool.order_for(i)) == {A, B, C}

    def test_a_much_slower_mirror_is_never_tried_first(self):
        """Rotating onto a slow mirror costs the full hedge delay per request."""
        pool = EndpointPool([A, B, C])
        pool.record_success(A, 1.0)
        pool.record_success(B, 1.2)
        pool.record_success(C, 60.0)  # far slower than its peers
        firsts = {pool.order_for(i)[0] for i in range(6)}
        assert firsts == {A, B}
        assert all(C in pool.order_for(i) for i in range(6)), "still a fallback"

    def test_single_mirror_is_not_rotated(self):
        pool = EndpointPool([A])
        assert pool.order_for(5) == [A]


class TestHedging:
    @pytest.fixture(autouse=True)
    def fast_hedge(self, monkeypatch):
        monkeypatch.setattr(config, "OVERPASS_HEDGE_AFTER_S", 0.05)
        # Real backoff would make this suite take minutes.
        monkeypatch.setattr(config, "OVERPASS_BACKOFF_SECONDS", [0.0, 0.0, 0.0, 0.0, 0.0])
        monkeypatch.setattr(overpass, "POOL", EndpointPool([A, B]))
        self.monkeypatch = monkeypatch
        yield

    def _patch_post(self, fn):
        """Patch via monkeypatch so it is undone between tests."""
        self.monkeypatch.setattr(overpass, "_post", fn)

    @pytest.mark.asyncio
    async def test_a_stalled_mirror_is_overtaken_by_the_hedge(self):
        """The slow mirror is not waited out; the second reply wins."""
        async def post(client, url, query):
            if url == A:
                await asyncio.sleep(30.0)
                return [{"from": "slow"}]
            await asyncio.sleep(0.01)
            return [{"from": "fast"}]

        self._patch_post(post)
        overpass.POOL.record_success(A, 0.1)  # A looks best, but stalls
        overpass.POOL.record_success(B, 0.2)
        started = time.monotonic()
        elements, url = await overpass._post_hedged(None, "q", worker=0)
        assert elements == [{"from": "fast"}]
        assert url == B
        assert time.monotonic() - started < 2.0

    @pytest.mark.asyncio
    async def test_failure_falls_through_to_the_next_mirror(self):
        async def post(client, url, query):
            if url == A:
                raise overpass.OverpassError("HTTP 429")
            return [{"ok": True}]

        self._patch_post(post)
        elements, url = await overpass._post_hedged(None, "q", worker=0)
        assert elements == [{"ok": True}]
        assert url == B
        assert overpass.POOL.health(A).failures == 1

    @pytest.mark.asyncio
    async def test_all_mirrors_failing_raises(self):
        async def post(client, url, query):
            raise overpass.OverpassError("HTTP 504")

        self._patch_post(post)
        with pytest.raises(overpass.OverpassError):
            await overpass._post_hedged(None, "q", worker=0)

    @pytest.mark.asyncio
    async def test_success_is_recorded_against_the_winning_mirror(self):
        async def post(client, url, query):
            return []

        self._patch_post(post)
        _, url = await overpass._post_hedged(None, "q", worker=0)
        assert overpass.POOL.health(url).successes == 1
