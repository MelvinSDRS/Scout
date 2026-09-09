import asyncio
import fcntl
import time
from contextlib import contextmanager

from .image_filter import PhotoFilter
from .models import AccessBlocked, SearchSpec
from .notify import deliver
from .providers.facebook import Facebook
from .searches import Searches


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


async def run(store, settings, once=False):
    provider = Facebook(settings)
    with worker_lock(settings.data_dir):
        photos = PhotoFilter(store, settings)
        searches = Searches(store)
        searches.recover()
        interactive_streak = 0
        try:
            while True:
                store.heartbeat()
                await deliver(store, settings)
                with store.connect() as db:
                    cooldowns = {
                        row["key"].removeprefix("cooldown:"): float(row["value"])
                        for row in db.execute("SELECT * FROM meta WHERE key LIKE 'cooldown:%'")
                    }
                due = [
                    scan
                    for scan in store.due(time.time())
                    if cooldowns.get(scan["source"], 0) <= time.time()
                ]
                job = searches.claim() if interactive_streak < 2 or not due else None
                interactive = job is not None
                if interactive:
                    interactive_streak += 1
                elif due:
                    job = due[0]
                    interactive_streak = 0
                else:
                    if once:
                        return
                    await asyncio.sleep(5)
                    continue
                if cooldowns.get(job["source"], 0) > time.time():
                    searches.fail(
                        job, "Source temporarily paused after an access error; retry later.", True
                    )
                    continue
                spec = SearchSpec.model_validate_json(job["spec"])
                try:
                    async with asyncio.timeout(180):
                        result = await provider.search(spec.query, job["country"], job["anchor"])
                    if interactive:
                        searches.record(job, result)
                    else:
                        filtered = await photos.filter(job, spec, result)
                        store.record(job, filtered, time.time())
                except Exception as exc:
                    if interactive:
                        searches.fail(job, safe_error(exc), isinstance(exc, AccessBlocked))
                    else:
                        store.failed(job, safe_error(exc), time.time())
                    if isinstance(exc, AccessBlocked):
                        with store.connect() as db:
                            db.execute(
                                "INSERT OR REPLACE INTO meta VALUES (?,?)",
                                (f"cooldown:{job['source']}", str(time.time() + 3600)),
                            )
                store.heartbeat()
                await deliver(store, settings)
                await asyncio.sleep(settings.scan_delay)
        finally:
            await provider.close()
