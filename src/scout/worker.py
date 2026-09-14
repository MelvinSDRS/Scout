import asyncio
import fcntl
import logging
import time
from contextlib import contextmanager, suppress

from .image_filter import PhotoFilter
from .models import AccessBlocked, SearchSpec
from .notify import deliver
from .providers.facebook import Facebook
from .searches import Searches

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    worker_handler = logging.StreamHandler()
    worker_handler.setFormatter(logging.Formatter("%(levelname)s scout.worker: %(message)s"))
    logger.addHandler(worker_handler)
    logger.propagate = False
HOME_RECOVERY_RETRY_SECONDS = 300
GENERIC_SESSION_ERROR = "Marketplace location could not be prepared or restored; retrying."
PHOTO_QUEUE_SIZE = 2
_PHOTO_QUEUE_STOP = object()


@contextmanager
def worker_lock(data_dir):
    with (data_dir / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(
                "Another worker or login process is already running. Stop scout serve/worker "
                "(Ctrl+C), or run systemctl --user stop scout, then retry."
            ) from None
        yield


def safe_error(exc):
    # Known, local messages are safe; third-party exceptions can expose credentials/URLs.
    return str(exc)[:300] if type(exc) in (AccessBlocked, RuntimeError) else type(exc).__name__


def scan_batch(due, pending):
    """Freeze ready work, interleave queue types, then visit each point only once."""
    seeds = []
    scheduled = iter(due)
    for offset in range(0, len(pending), 2):
        seeds.extend(pending[offset : offset + 2])
        job = next(scheduled, None)
        if job is not None:
            seeds.append(job)
    seeds.extend(scheduled)
    groups = {}
    for job in seeds:
        groups.setdefault((job["source"], job["country"], job["anchor"]), []).append(job)
    return [job for group in groups.values() for job in group]


def source_cooldowns(store):
    with store.connect() as db:
        return {
            row["key"].removeprefix("cooldown:"): float(row["value"])
            for row in db.execute("SELECT * FROM meta WHERE key LIKE 'cooldown:%'")
        }


def fail_job(store, searches, job, exc, completed=False):
    if "search_id" in job:
        searches.fail(job, safe_error(exc), isinstance(exc, AccessBlocked), completed=completed)
    else:
        store.failed(job, safe_error(exc), time.time())
    if isinstance(exc, AccessBlocked):
        with store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?)",
                (f"cooldown:{job['source']}", str(time.time() + 3600)),
            )


def recovery_failure(settings, exc):
    """Identify a failed session restore without logging provider details."""
    if not settings.facebook_restore_home:
        return False
    snapshot = settings.data_dir / "facebook-home.json"
    if snapshot.exists():
        return True
    return any("restore" in message.casefold() for message in (getattr(exc, "__notes__", ()) or ()))


async def recover_home(store, settings, provider, initial=False):
    """Restore the durable home snapshot before allowing any scan to claim work."""
    if not settings.facebook_restore_home:
        store.home_recovery_clear()
        return True
    status = store.home_recovery()
    if not initial and not status["pending"]:
        return True
    now = time.time()
    if status["pending"] and status["retry_at"] and status["retry_at"] > now:
        return False
    store.home_recovery_attempt(now)
    try:
        await provider.recover_home()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        retry_at = time.time() + HOME_RECOVERY_RETRY_SECONDS
        store.home_recovery_failed(safe_error(exc), retry_at)
        logger.warning(
            "Facebook home recovery failed; retry_at=%.0f error_type=%s",
            retry_at,
            type(exc).__name__,
        )
        return False
    else:
        store.home_recovery_clear()
        return True


async def _put_photo_work(queue, item, consumer):
    """Put work without hanging forever if the single consumer has stopped."""
    putter = asyncio.create_task(queue.put(item))
    try:
        done, _ = await asyncio.wait((putter, consumer), return_when=asyncio.FIRST_COMPLETED)
        if consumer in done:
            consumer.result()
        await putter
    finally:
        if not putter.done():
            putter.cancel()
            with suppress(asyncio.CancelledError):
                await putter


async def _drain_photo_queue(queue, stop_putter, consumer):
    """Close and drain the queue while detecting an unexpectedly dead worker."""
    done, _ = await asyncio.wait((stop_putter, consumer), return_when=asyncio.FIRST_COMPLETED)
    if consumer in done:
        consumer.result()
    await stop_putter

    joiner = asyncio.create_task(queue.join())
    try:
        done, _ = await asyncio.wait((joiner, consumer), return_when=asyncio.FIRST_COMPLETED)
        if consumer in done:
            consumer.result()
        await joiner
        await consumer
    finally:
        if not joiner.done():
            joiner.cancel()
            with suppress(asyncio.CancelledError):
                await joiner


async def _photo_consumer(store, settings, searches, photos, queue, stats):
    """Filter and record scheduled results in order, with one photo worker."""
    while True:
        item = await queue.get()
        try:
            if item is _PHOTO_QUEUE_STOP:
                return
            job, spec, result = item
            started = time.monotonic()
            try:
                filtered = await photos.filter(job, spec, result)
                store.record(job, filtered, time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fail_job(store, searches, job, exc)
                stats["failed"] += 1
            else:
                stats["completed"] += 1
            finally:
                stats["photo_elapsed"] += time.monotonic() - started
                store.heartbeat()
            try:
                await deliver(store, settings)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # deliver() normally records transport failures itself. Keep a
                # filtering worker alive if its client/setup fails unexpectedly;
                # the next worker pass will retry the outbox.
                logger.warning(
                    "Facebook scan notification delivery failed; error_type=%s",
                    type(exc).__name__,
                )
        finally:
            queue.task_done()


async def run_batch(store, settings, searches, photos, provider, jobs):
    # Validate immediately before execution: watches can be disabled/deleted and
    # interactive jobs cancelled while a batch is in flight.
    handled_error = False
    started = time.monotonic()
    stats = {
        "completed": 0,
        "failed": 0,
        "photo_elapsed": 0.0,
        "search_elapsed": 0.0,
    }
    photo_queue = asyncio.Queue(maxsize=PHOTO_QUEUE_SIZE)
    photo_consumer = asyncio.create_task(
        _photo_consumer(store, settings, searches, photos, photo_queue, stats),
        name="scout-photo-consumer",
    )
    stop_putter = None
    logger.info("Facebook scan batch started jobs=%d", len(jobs))
    try:
        async with provider.scan_session() as session:
            for position, candidate in enumerate(jobs):
                if "search_id" in candidate:
                    job = searches.claim(candidate["id"])
                else:
                    job = next(
                        (
                            row
                            for row in store.due(time.time())
                            if store.key(row) == store.key(candidate)
                        ),
                        None,
                    )
                if job is None:
                    continue
                spec = SearchSpec.model_validate_json(job["spec"])
                try:
                    search_started = time.monotonic()
                    try:
                        async with asyncio.timeout(180):
                            result = await session.search(spec.query, job["country"], job["anchor"])
                    finally:
                        stats["search_elapsed"] += time.monotonic() - search_started
                    if "search_id" in job:
                        searches.record(job, result)
                        stats["completed"] += 1
                    else:
                        await _put_photo_work(photo_queue, (job, spec, result), photo_consumer)
                except Exception as exc:
                    fail_job(store, searches, job, exc)
                    stats["failed"] += 1
                    if isinstance(exc, AccessBlocked):
                        handled_error = True
                        raise  # Restore now; do not keep scanning a blocked account.
                store.heartbeat()
                # The last pause is outside the session: return home immediately.
                if position != len(jobs) - 1:
                    await asyncio.sleep(settings.scan_delay)
            # Do not wait for photo work here: the browser can restore its home
            # location while the bounded queue drains in the background.
            stop_putter = asyncio.create_task(photo_queue.put(_PHOTO_QUEUE_STOP))
        await _drain_photo_queue(photo_queue, stop_putter, photo_consumer)
        stop_putter = None
    except Exception as exc:
        known_recovery_failure = recovery_failure(settings, exc)
        if known_recovery_failure or not handled_error:
            retry_at = time.time() + HOME_RECOVERY_RETRY_SECONDS
            error = safe_error(exc) if known_recovery_failure else GENERIC_SESSION_ERROR
            store.home_recovery_failed(error, retry_at)
            logger.warning(
                "Facebook scan batch session boundary failed; retry_at=%.0f error_type=%s",
                retry_at,
                type(exc).__name__,
            )
    finally:
        if stop_putter is not None and not stop_putter.done():
            stop_putter.cancel()
            try:
                await stop_putter
            except asyncio.CancelledError:
                pass
        if not photo_consumer.done():
            photo_consumer.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await photo_consumer
        store.heartbeat()
        logger.info(
            "Facebook scan batch finished jobs=%d completed=%d failed=%d "
            "search_elapsed=%.1fs photo_elapsed=%.1fs elapsed=%.1fs",
            len(jobs),
            stats["completed"],
            stats["failed"],
            stats["search_elapsed"],
            stats["photo_elapsed"],
            time.monotonic() - started,
        )


async def run(store, settings, once=False):
    provider = Facebook(settings)
    with worker_lock(settings.data_dir):
        photos = PhotoFilter(store, settings)
        searches = Searches(store)
        searches.recover()
        recovery_checked = False
        try:
            while True:
                store.heartbeat()
                await deliver(store, settings)
                if not await recover_home(store, settings, provider, initial=not recovery_checked):
                    recovery_checked = True
                    if once:
                        return
                    await asyncio.sleep(5)
                    continue
                recovery_checked = True
                cooldowns = source_cooldowns(store)
                now = time.time()
                due = [job for job in store.due(now) if cooldowns.get(job["source"], 0) <= now]
                pending = []
                for candidate in searches.pending():
                    if cooldowns.get(candidate["source"], 0) > now:
                        job = searches.claim(candidate["id"])
                        if job:
                            searches.fail(
                                job,
                                "Source temporarily paused after an access error; retry later.",
                                True,
                            )
                    else:
                        pending.append(candidate)
                jobs = scan_batch(due, pending)
                if not jobs:
                    if once:
                        return
                    await asyncio.sleep(5)
                    continue
                await run_batch(store, settings, searches, photos, provider, jobs)
                await deliver(store, settings)
                await asyncio.sleep(settings.scan_delay)
        finally:
            await provider.close()
