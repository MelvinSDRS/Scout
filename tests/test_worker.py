import asyncio
from contextlib import asynccontextmanager

import pytest

from scout import worker
from scout.models import AccessBlocked, SearchResult, SearchSpec, Watch
from scout.providers.facebook import REGIONS
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store


@pytest.fixture
def harness(tmp_path, monkeypatch):
    store = Store(tmp_path / "test.sqlite3")
    searches = Searches(store)
    events = []

    class Provider:
        hook = None

        def __init__(self, settings):
            pass

        async def recover_home(self):
            events.append("recover")

        @asynccontextmanager
        async def scan_session(self):
            events.append("capture")
            try:
                yield self
            finally:
                events.append("restore")

        async def search(self, query, country, anchor):
            events.append((query, country, anchor))
            if Provider.hook:
                await Provider.hook(query, country, anchor)
            return SearchResult([], radius_km=500)

        async def close(self):
            events.append("close")

    async def noop(*args):
        pass

    monkeypatch.setattr(worker, "Facebook", Provider)
    monkeypatch.setattr(worker, "deliver", noop)
    monkeypatch.setattr(worker.asyncio, "sleep", noop)
    return store, searches, Settings(tmp_path), events, Provider


def add_watch(store, query):
    return store.add(Watch(name=query, query=query, countries=["FR"]), REGIONS)


def test_all_watches_share_one_session_and_each_point_is_visited_once(harness):
    store, _, settings, events, _ = harness
    add_watch(store, "Oakley")
    add_watch(store, "Spider-Man")
    asyncio.run(worker.run(store, settings, once=True))
    calls = [event for event in events if isinstance(event, tuple)]
    assert [call[0] for call in calls] == ["Oakley", "Spider-Man", "Oakley", "Spider-Man"]
    assert calls[0][1:] == calls[1][1:]
    assert calls[2][1:] == calls[3][1:]
    assert calls[0][2] != calls[2][2]
    assert events.count("capture") == events.count("restore") == 1
    assert all(row["initialized"] for row in store.health()["scans"])


def test_interactive_and_scheduled_at_same_point_share_session(harness):
    store, searches, settings, events, _ = harness
    add_watch(store, "Scheduled")
    ident = searches.submit(SearchSpec(query="Interactive", countries=["FR"]))
    asyncio.run(worker.run(store, settings, once=True))
    calls = [event for event in events if isinstance(event, tuple)]
    assert [call[0] for call in calls] == ["Interactive", "Scheduled"] * 2
    assert calls[0][1:] == calls[1][1:]
    assert searches.get(ident)["status"] == "complete"
    assert events.count("restore") == 1


def test_batch_revalidates_cancelled_and_disabled_work(harness):
    store, searches, settings, events, provider = harness
    watch_id = add_watch(store, "Scheduled")
    ident = searches.submit(SearchSpec(query="Interactive", countries=["FR"]))

    async def cancel(*args):
        searches.cancel(ident)
        with store.connect() as db:
            db.execute(
                "UPDATE watches SET spec=json_set(spec,'$.enabled',json('false')) WHERE id=?",
                (watch_id,),
            )

    provider.hook = cancel
    asyncio.run(worker.run(store, settings, once=True))
    assert len([event for event in events if isinstance(event, tuple)]) == 1
    assert searches.get(ident)["status"] == "cancelled"
    assert events.count("restore") == 1


@pytest.mark.parametrize("error", [RuntimeError("search failed"), AccessBlocked("login required")])
def test_error_restores_and_access_block_stops_batch(harness, error):
    store, searches, settings, events, provider = harness
    ident = searches.submit(SearchSpec(query="Interactive", countries=["FR"]))

    async def fail(*args):
        raise error

    provider.hook = fail
    asyncio.run(worker.run(store, settings, once=True))
    assert searches.get(ident)["status"] == "failed"
    assert events.count("restore") == 1
    calls = [event for event in events if isinstance(event, tuple)]
    assert len(calls) == (1 if isinstance(error, AccessBlocked) else 2)
    if isinstance(error, AccessBlocked):
        assert worker.source_cooldowns(store)["facebook"] > 0


def test_cancellation_restores_before_closing_browser(harness):
    store, _, settings, events, provider = harness
    add_watch(store, "Scheduled")

    async def cancel(*args):
        raise asyncio.CancelledError()

    provider.hook = cancel
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run(store, settings, once=True))
    assert events[-2:] == ["restore", "close"]


def test_work_arriving_mid_scan_waits_for_next_session(harness):
    store, _, settings, events, provider = harness
    add_watch(store, "First")
    added = False

    async def add(*args):
        nonlocal added
        if not added:
            added = True
            add_watch(store, "Later")

    provider.hook = add
    asyncio.run(worker.run(store, settings, once=True))
    first_restore = events.index("restore")
    assert all(event[0] == "First" for event in events[:first_restore] if isinstance(event, tuple))
    assert events.count("capture") == events.count("restore") == 2


def test_no_due_work_does_not_capture_location(harness):
    store, _, settings, events, _ = harness
    asyncio.run(worker.run(store, settings, once=True))
    assert "capture" not in events


@pytest.mark.parametrize(
    "error", [RuntimeError("Could not restore home"), AccessBlocked("Could not restore home")]
)
def test_restoration_failure_is_visible_after_last_result(harness, error):
    store, searches, settings, events, provider = harness
    ident = searches.submit(SearchSpec(query="Interactive", countries=["FR"]))
    (settings.data_dir / "facebook-home.json").touch()

    @asynccontextmanager
    async def failed_restore(self):
        events.append("capture")
        yield self
        events.append("restore")
        raise error

    provider.scan_session = failed_restore
    asyncio.run(worker.run(store, settings, once=True))
    result = searches.get(ident)
    assert result["status"] == "complete"
    recovery = store.health()["home_recovery"]
    assert recovery["pending"] is True
    assert recovery["error"] == str(error)
    assert recovery["retry_at"] > worker.time.time()


def test_future_watch_is_not_pulled_forward_to_join_region(harness):
    store, _, settings, events, _ = harness
    add_watch(store, "Due")
    future = add_watch(store, "Future")
    with store.connect() as db:
        db.execute(
            "UPDATE scans SET next_run=? WHERE watch_id=?", (worker.time.time() + 3600, future)
        )
    asyncio.run(worker.run(store, settings, once=True))
    assert {event[0] for event in events if isinstance(event, tuple)} == {"Due"}


def test_home_recovery_runs_even_without_due_work(harness):
    store, _, settings, events, _ = harness
    asyncio.run(worker.run(store, settings, once=True))
    assert events == ["recover", "close"]


def test_failed_home_recovery_keeps_all_work_queued(harness):
    store, searches, settings, events, provider = harness
    add_watch(store, "Scheduled")
    ident = searches.submit(SearchSpec(query="Interactive", countries=["FR"]))

    async def fail_recovery():
        events.append("recover")
        raise RuntimeError("Could not restore home")

    provider.recover_home = fail_recovery
    asyncio.run(worker.run(store, settings, once=True))

    assert not [event for event in events if isinstance(event, tuple)]
    assert searches.get(ident)["status"] == "running"
    assert all(row["failures"] == 0 for row in store.health()["scans"])
    assert store.health()["home_recovery"]["pending"] is True


def test_home_recovery_backoff_skips_retry_and_scan(harness):
    store, searches, settings, events, provider = harness
    ident = searches.submit(SearchSpec(query="Interactive", countries=["FR"]))
    now = worker.time.time()
    store.home_recovery_failed("Could not restore home", now + 300)

    assert asyncio.run(worker.recover_home(store, settings, provider)) is False
    assert events == []
    assert searches.get(ident)["status"] == "running"


def test_disabled_home_restore_clears_recovery_gate_without_provider_call(harness):
    store, _, settings, events, provider = harness
    settings.facebook_restore_home = False
    store.home_recovery_failed("stale recovery", worker.time.time() + 300)

    assert asyncio.run(worker.recover_home(store, settings, provider(settings))) is True
    assert events == []
    assert store.health()["home_recovery"]["pending"] is False


def test_disabled_home_restore_ignores_stale_snapshot_in_failure_classification(harness):
    store, _, settings, _, _ = harness
    settings.facebook_restore_home = False
    (settings.data_dir / "facebook-home.json").write_text("stale")

    assert worker.recovery_failure(settings, RuntimeError("session failed")) is False


def test_session_boundary_failure_is_global_and_does_not_claim_work(harness):
    store, searches, settings, events, provider = harness
    add_watch(store, "Scheduled")
    ident = searches.submit(SearchSpec(query="Interactive", countries=["FR"]))

    @asynccontextmanager
    async def broken_session(self):
        events.append("capture")
        raise RuntimeError("browser capture failed")
        yield self

    provider.scan_session = broken_session
    asyncio.run(worker.run(store, settings, once=True))

    assert not [event for event in events if isinstance(event, tuple)]
    assert searches.get(ident)["status"] == "running"
    recovery = store.health()["home_recovery"]
    assert recovery["pending"] is True
    assert recovery["error"] == worker.GENERIC_SESSION_ERROR
    assert all(row["failures"] == 0 for row in store.health()["scans"])


def test_photo_work_overlaps_searches_but_has_one_consumer(harness):
    store, searches, settings, events, provider = harness
    add_watch(store, "Scheduled")
    settings.scan_delay = 0
    jobs = store.due(worker.time.time())
    first_photo_started = asyncio.Event()
    second_search_done = asyncio.Event()
    photo_active = 0
    photo_peak = 0
    saw_search_during_photo = False

    async def search_hook(*args):
        nonlocal saw_search_during_photo
        if photo_active:
            saw_search_during_photo = True
        if len([event for event in events if isinstance(event, tuple)]) == 2:
            second_search_done.set()

    provider.hook = search_hook

    class Photos:
        async def filter(self, job, spec, result):
            nonlocal photo_active, photo_peak
            photo_active += 1
            photo_peak = max(photo_peak, photo_active)
            first_photo_started.set()
            await second_search_done.wait()
            photo_active -= 1
            return result

    async def execute():
        task = asyncio.create_task(
            worker.run_batch(store, settings, searches, Photos(), provider(settings), jobs)
        )
        await first_photo_started.wait()
        await task

    asyncio.run(execute())
    assert saw_search_during_photo
    assert photo_peak == 1
    assert all(row["initialized"] for row in store.health()["scans"])


def test_photo_failure_is_recorded_and_later_photo_work_drains(harness):
    store, searches, settings, _, provider = harness
    add_watch(store, "Scheduled")
    settings.scan_delay = 0
    jobs = store.due(worker.time.time())
    failed = False

    class Photos:
        async def filter(self, job, spec, result):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("photo failed")
            return result

    asyncio.run(worker.run_batch(store, settings, searches, Photos(), provider(settings), jobs))
    scans = store.health()["scans"]
    assert sum(row["failures"] == 1 and not row["initialized"] for row in scans) == 1
    assert sum(row["initialized"] for row in scans) == 1


def test_photo_queue_restores_home_before_drain_and_drains_before_return(harness):
    store, searches, settings, events, _ = harness
    add_watch(store, "Scheduled")
    settings.scan_delay = 0
    jobs = store.due(worker.time.time())
    release_photo = asyncio.Event()
    photo_started = asyncio.Event()

    class Provider:
        @asynccontextmanager
        async def scan_session(self):
            events.append("capture")
            try:
                yield self
            finally:
                events.append("restore")
                release_photo.set()

        async def search(self, query, country, anchor):
            events.append((query, country, anchor))
            return SearchResult([])

    class Photos:
        async def filter(self, job, spec, result):
            photo_started.set()
            await release_photo.wait()
            events.append("photo_done")
            return result

    async def execute():
        task = asyncio.create_task(
            worker.run_batch(store, settings, searches, Photos(), Provider(), jobs)
        )
        await photo_started.wait()
        await task

    asyncio.run(execute())
    assert events.index("restore") < events.index("photo_done")
    assert events[-1] == "photo_done"
    assert all(row["initialized"] for row in store.health()["scans"])


def test_photo_queue_cancellation_cleans_consumer_and_leaves_scan_due(harness):
    store, searches, settings, events, _ = harness
    add_watch(store, "Scheduled")
    settings.scan_delay = 0
    jobs = store.due(worker.time.time())
    photo_started = asyncio.Event()
    photo_cancelled = asyncio.Event()

    class Provider:
        @asynccontextmanager
        async def scan_session(self):
            events.append("capture")
            try:
                yield self
            finally:
                events.append("restore")

        async def search(self, query, country, anchor):
            return SearchResult([])

    class Photos:
        async def filter(self, job, spec, result):
            photo_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                photo_cancelled.set()
                raise

    async def execute():
        task = asyncio.create_task(
            worker.run_batch(store, settings, searches, Photos(), Provider(), jobs)
        )
        await photo_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert photo_cancelled.is_set()
        assert not [
            running
            for running in asyncio.all_tasks()
            if running.get_name() == "scout-photo-consumer"
        ]

    asyncio.run(execute())
    assert events.index("restore") < len(events)
    assert all(not row["initialized"] for row in store.health()["scans"])


def test_dead_photo_consumer_does_not_hang_final_drain(harness, monkeypatch):
    store, searches, settings, events, _ = harness
    add_watch(store, "Scheduled")
    settings.scan_delay = 0
    jobs = store.due(worker.time.time())
    release_photo = asyncio.Event()

    class Provider:
        @asynccontextmanager
        async def scan_session(self):
            events.append("capture")
            try:
                yield self
            finally:
                events.append("restore")
                release_photo.set()

        async def search(self, query, country, anchor):
            return SearchResult([])

    class Photos:
        async def filter(self, job, spec, result):
            await release_photo.wait()
            raise RuntimeError("photo failure")

    def broken_fail_job(*args, **kwargs):
        raise RuntimeError("failure handler stopped")

    monkeypatch.setattr(worker, "fail_job", broken_fail_job)

    async def execute():
        await asyncio.wait_for(
            worker.run_batch(store, settings, searches, Photos(), Provider(), jobs),
            timeout=1,
        )
        assert not [
            running
            for running in asyncio.all_tasks()
            if running.get_name() == "scout-photo-consumer"
        ]

    asyncio.run(execute())
    assert events[-1] == "restore"
