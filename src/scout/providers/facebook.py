"""Read rendered Marketplace search data; stop at login/checkpoint walls."""

import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from importlib.resources import files
from unicodedata import normalize
from urllib.parse import parse_qs, urlencode

from ..geography import distance_km
from ..models import AccessBlocked, SearchResult, matches
from .facebook_data import (
    applied_radius,
    applied_search_center,
    choose_radius,
    confirmed_empty,
    extract_listings,
    listing_coordinates,
    more_results,
    objects,
    search_feeds,
)
from .facebook_location import (
    HomeLocation,
    capture_home,
    configure_partner_selection,
    open_location,
    restore_home,
    save_home,
)

REGIONS = json.loads(files("scout").joinpath("regions.json").read_text())
CITY_ANCHOR = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
MAX_RADIUS_KM = 805

# A batch session is entered outside the worker's per-query deadline. Keep browser
# startup, persisted recovery, and the initial preference capture finite on their own.
SESSION_START_TIMEOUT = 180

# Facebook's GraphQL responses usually arrive quickly, but a slow account or a
# throttled request must not leave a scan hanging forever. These are upper bounds
# for condition-based waits, not pacing delays.
SEARCH_RESPONSE_TIMEOUT = 12
PAGINATION_REQUEST_TIMEOUT = 1.5
PAGINATION_RESPONSE_TIMEOUT = 5

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s scout.facebook: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


def _validated_custom_radius(radius_km):
    if isinstance(radius_km, bool) or not isinstance(radius_km, int):
        raise ValueError("Facebook radius_km must be an integer")
    if not 1 <= radius_km <= MAX_RADIUS_KM:
        raise ValueError(f"Facebook radius_km must be between 1 and {MAX_RADIUS_KM}")
    return radius_km


def _validated_anchor(anchor):
    if not isinstance(anchor, str) or not CITY_ANCHOR.fullmatch(anchor):
        raise ValueError("Facebook anchor must be a city slug or numeric city id")
    return anchor


def _city_slug(value):
    value = normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value)).strip("-")


def _search_city_identities(documents):
    identities = {}
    for obj in objects(documents):
        stories = obj.get("marketplace_feed_stories")
        if not isinstance(stories, dict):
            continue
        selected = stories.get("buy_location")
        if not isinstance(selected, dict):
            continue
        city_id = selected.get("id")
        display_name = selected.get("display_name")
        if (
            isinstance(city_id, (str, int))
            and re.fullmatch(r"[0-9]+", str(city_id))
            and isinstance(display_name, str)
            and display_name.strip()
        ):
            key = (str(city_id), display_name.strip())
            canonical_slug = selected.get("slug")
            identities.setdefault(key, set())
            if isinstance(canonical_slug, str) and canonical_slug.strip():
                identities[key].add(canonical_slug.strip())
    return [(*key, tuple(slugs)) for key, slugs in identities.items()]


def _verify_custom_anchor(documents, anchor):
    identities = _search_city_identities(documents)
    if len(identities) != 1:
        raise RuntimeError("Could not verify Facebook search city")
    city_id, display_name, canonical_slugs = next(iter(identities))
    if anchor.isdigit():
        matches = city_id == anchor
    else:
        names = [display_name]
        if "," in display_name:
            names.append(display_name.split(",", 1)[0])
        names.extend(canonical_slugs)
        matches = any(_city_slug(name) == anchor for name in names)
    if not matches:
        raise RuntimeError("Could not verify Facebook search city")
    return city_id, display_name


def search_url(query, country, city, radius_km=None):
    if radius_km is None:
        region = next(r for r in REGIONS[country] if r["city"] == city)
        radius = region["radius"]
    else:
        _validated_anchor(city)
        radius = _validated_custom_radius(radius_km)
    # Newest-first can bury exact model matches beneath Facebook's relaxed
    # suggestions. Use its default relevance order; alerts still deduplicate IDs.
    return f"https://www.facebook.com/marketplace/{city}/search?" + urlencode(
        {
            "query": query,
            "radius": radius,
            "exact": "true",
        }
    )


def json_documents(text):
    text = re.sub(r"^for\s*\(;;\);", "", text.strip())
    try:
        return [json.loads(text)]
    except (ValueError, TypeError):
        docs = []
        for line in text.splitlines():
            try:
                docs.append(json.loads(line))
            except ValueError:
                pass
        return docs


class Facebook:
    def __init__(self, settings):
        self.settings = settings
        self.playwright = None
        self.context = None
        self.browser = None
        self._search_lock = asyncio.Lock()

    async def start(self, headed=False):
        if self.context:
            return
        from playwright.async_api import async_playwright

        startup = asyncio.create_task(async_playwright().start())
        try:
            self.playwright = await asyncio.shield(startup)
        except asyncio.CancelledError:
            # Cancellation before start() returns otherwise loses the driver handle and
            # leaves asyncio.run waiting on its orphaned transport during SSH teardown.
            runtime = await startup
            await runtime.stop()
            raise
        if self.settings.facebook_cdp:
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.settings.facebook_cdp
            )
            self.context = self.browser.contexts[0]
        else:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.settings.data_dir / "facebook-profile"),
                headless=not headed,
                locale="en-US",
                viewport={"width": 1440, "height": 1000},
            )

    async def close(self):
        if self.context and not self.settings.facebook_cdp:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.context = self.playwright = self.browser = None

    async def search(
        self, query, country, anchor, *, radius_km=None, include_sold=False, pricing_spec=None
    ):
        """Run one search with the same durable preference safety as a batch."""
        async with self.scan_session() as session:
            if radius_km is None and not include_sold:
                return await session.search(query, country, anchor)
            return await session.search(
                query,
                country,
                anchor,
                radius_km=radius_km,
                include_sold=include_sold,
                **({"pricing_spec": pricing_spec} if pricing_spec is not None else {}),
            )

    def scan_session(self):
        """Return a context manager for several serialized searches."""
        return _FacebookScanSession(self)

    async def recover_home(self):
        """Restore a persisted pre-scan location after an interrupted process.

        Recovery deliberately does not capture a new snapshot: the file is the
        original preference that must be restored. A subsequent scan session can
        capture the current home once it has taken ownership of the browser.
        """
        if not self.settings.facebook_restore_home:
            return True
        snapshot = self.settings.data_dir / "facebook-home.json"
        page = None
        acquired = False
        recovered = False
        try:
            async with asyncio.timeout(SESSION_START_TIMEOUT):
                await self._search_lock.acquire()
                acquired = True
                if not snapshot.exists():
                    pass
                else:
                    await self.start()
                    page = await self.context.new_page()
                    await self._recover_snapshot(page, snapshot)
                    recovered = True
        except BaseException as exc:
            if page is not None:
                try:
                    await asyncio.wait_for(page.close(), timeout=10)
                except Exception as close_error:
                    exc.add_note(f"Could not close Facebook recovery page: {close_error}")
            raise
        else:
            if page is not None:
                await asyncio.wait_for(page.close(), timeout=10)
        finally:
            if acquired:
                self._search_lock.release()
        return recovered

    async def _recover_snapshot(self, page, snapshot):
        if not snapshot.exists():
            return None
        home = HomeLocation(**json.loads(snapshot.read_text(encoding="utf-8")))
        await self._restore_home(page, home, snapshot)
        return home

    async def _restore_home(self, page, home, snapshot):
        if not self.settings.facebook_restore_home:
            return None
        try:
            async with asyncio.timeout(90):
                await restore_home(page, home, check_access)
            snapshot.unlink()
        except Exception as exc:
            reason = (
                str(exc)[:300] if type(exc) in (AccessBlocked, RuntimeError) else type(exc).__name__
            )
            logger.error(
                "Facebook home restoration failed (%s); original settings retained for retry",
                reason,
            )
            error_type = AccessBlocked if isinstance(exc, AccessBlocked) else RuntimeError
            raise error_type(
                "Could not restore the original Facebook location and radius; "
                f"saved settings retained for retry ({reason})"
            ) from exc

    async def _search(
        self, query, country, anchor, *, radius_km=None, include_sold=False, pricing_spec=None
    ):
        if radius_km is None:
            desired = next(r["radius"] for r in REGIONS[country] if r["city"] == anchor)
        else:
            _validated_anchor(anchor)
            desired = _validated_custom_radius(radius_km)
        await self.start()
        page = await self.context.new_page()
        started = time.monotonic()
        documents = []
        requests = []
        pending = set()
        response_event = asyncio.Event()
        graphql_request_event = asyncio.Event()
        graphql_request_count = 0
        search_request_count = 0
        search_response_count = 0
        search_requests_seen = set()
        stale_search_requests = set()
        pagination_responses = 0
        pagination_timeouts = 0
        pagination_request_timeouts = 0

        async def capture(response):
            nonlocal search_response_count
            if "/api/graphql" in response.url:
                response_request = response.request
                try:
                    captured = json_documents(await response.text())
                    if response_request not in stale_search_requests:
                        documents.extend(captured)
                    if response_request not in stale_search_requests and search_feeds(captured):
                        search_response_count += 1
                except Exception:
                    pass
                finally:
                    response_event.set()

        def on_response(response):
            task = asyncio.create_task(capture(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

        def on_request(request):
            nonlocal search_request_count
            nonlocal graphql_request_count
            if "/api/graphql" not in request.url:
                return
            graphql_request_count += 1
            graphql_request_event.set()
            if request.post_data:
                try:
                    variables = json.loads(parse_qs(request.post_data).get("variables", ["{}"])[0])
                    search = variables.get("params", {})
                    params = search.get("browse_request_params", {})
                    if (
                        search.get("bqf", {}).get("query") == query
                        and search.get("custom_request_params", {}).get("surface") == "SEARCH"
                        and "filter_radius_km" in params
                    ):
                        search_request_count += 1
                        search_requests_seen.add(request)
                        requests.append(params)
                except (ValueError, AttributeError):
                    pass

        page.on("response", on_response)
        page.on("request", on_request)

        def has_structured_search_data(data):
            return bool(search_feeds(data))

        async def wait_for_search_condition(condition, timeout):
            """Wait for response-driven state, with a finite safety bound."""
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                await check_access(page)
                if condition():
                    return True
                response_event.clear()
                # A response can complete between the condition check and clear().
                if condition():
                    return True
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(response_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
            await check_access(page)
            return bool(condition())

        async def wait_for_search_response_after(previous_count, previous_request_count, timeout):
            # mouse.wheel() can return before the page's scroll handler starts its
            # fetch. Wait briefly for that event, then wait for the structured feed.
            if search_response_count > previous_count:
                return True
            if graphql_request_count <= previous_request_count:
                graphql_request_event.clear()
                if graphql_request_count <= previous_request_count:
                    try:
                        await asyncio.wait_for(
                            graphql_request_event.wait(), timeout=PAGINATION_REQUEST_TIMEOUT
                        )
                    except asyncio.TimeoutError:
                        return False
            return await wait_for_search_condition(
                lambda: search_response_count > previous_count,
                timeout,
            )

        async def read_initial_documents():
            data = []
            for script in await page.locator('script[type="application/json"]').all_text_contents():
                data.extend(json_documents(script))
            return data

        try:
            url = (
                search_url(query, country, anchor)
                if radius_km is None
                else search_url(query, country, anchor, radius_km=radius_km)
            )
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=45000,
            )
            if response and response.status in (401, 403, 429):
                raise AccessBlocked(f"Facebook HTTP {response.status}; login or rate limit")
            await check_access(page)
            initial = await read_initial_documents()
            await wait_for_search_condition(
                lambda: (
                    has_structured_search_data(initial + documents)
                    and (
                        applied_radius(initial + documents) is not None
                        or applied_radius(requests) is not None
                    )
                ),
                SEARCH_RESPONSE_TIMEOUT,
            )
            if radius_km is not None:
                _verify_custom_anchor(initial + documents, anchor)
            actual = applied_radius(initial) or applied_radius(requests)
            if actual != desired:
                dialog = await open_location(page)
                await dialog.get_by_role("combobox").last.click()
                options = await page.get_by_role("option").all_text_contents()
                selected, label = choose_radius(options, desired)
                await page.get_by_role("option", name=label, exact=True).click()
                # Discard the pre-change search; only ingest the radius-adjusted response.
                if pending:
                    await asyncio.gather(*list(pending))
                changed = abs(selected - actual) > 1 if actual is not None else True
                if changed:
                    stale_search_requests.update(search_requests_seen)
                    documents.clear()
                    requests.clear()
                    initial = []
                response_event.clear()
                await dialog.get_by_role("button", name=re.compile(r"^(Appliquer|Apply)$")).click()
                if changed:
                    await wait_for_search_condition(
                        lambda: (
                            (
                                applied_radius(initial + documents) == selected
                                or applied_radius(requests) == selected
                            )
                            and has_structured_search_data(initial + documents)
                        ),
                        SEARCH_RESPONSE_TIMEOUT,
                    )
                actual = applied_radius(requests) or (actual if not changed else None)
                if actual is None or abs(actual - selected) > 1:
                    # Facebook may reuse a cached route and issue no new search request.
                    # Reload the selected URL to verify fresh server search parameters.
                    if pending:
                        await asyncio.gather(*list(pending))
                    stale_search_requests.update(search_requests_seen)
                    documents.clear()
                    requests.clear()
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
                    await check_access(page)
                    initial = await read_initial_documents()
                    await wait_for_search_condition(
                        lambda: (
                            has_structured_search_data(initial + documents)
                            and (
                                applied_radius(initial + documents) is not None
                                or applied_radius(requests) is not None
                            )
                        ),
                        SEARCH_RESPONSE_TIMEOUT,
                    )
                    actual = applied_radius(initial) or applied_radius(requests)
                    if actual is None or abs(actual - selected) > 1:
                        raise RuntimeError("Could not verify Facebook's applied search radius")
            if actual is None:
                raise RuntimeError("Facebook search radius is unverified")
            # Hover once. Re-hovering a tall pane can scroll it back to the top.
            main = page.get_by_role("main").first
            await main.hover(position={"x": 100, "y": 200})
            if pending:
                await asyncio.gather(*list(pending))
            for _ in range(4):
                await check_access(page)
                combined = initial + documents
                feeds = search_feeds(combined)
                if feeds:
                    page_info = feeds[-1].get("page_info")
                    if isinstance(page_info, dict) and page_info.get("has_next_page") is False:
                        break
                previous_search_response_count = search_response_count
                previous_graphql_request_count = graphql_request_count
                await page.mouse.wheel(0, 2000)
                received = await wait_for_search_response_after(
                    previous_search_response_count,
                    previous_graphql_request_count,
                    PAGINATION_RESPONSE_TIMEOUT,
                )
                if received:
                    pagination_responses += 1
                elif graphql_request_count > previous_graphql_request_count:
                    pagination_timeouts += 1
                else:
                    pagination_request_timeouts += 1
            if pending:
                await asyncio.gather(*list(pending))
            combined = initial + documents
            city_note = None
            center = None
            if radius_km is not None:
                # Radius changes/reloads can discard earlier documents. Re-read
                # the rendered city identity before accepting the final sample.
                _, city_name = _verify_custom_anchor(
                    await read_initial_documents() + documents, anchor
                )
                city_note = f"Marketplace city: {city_name}. Confirm this is your intended area."
                center = applied_search_center(requests if requests else combined, actual)
                if center is None:
                    raise RuntimeError(
                        "Could not verify the search center coordinates; price check stopped"
                    )
            listings = extract_listings(
                combined,
                country,
                scoped=True,
                include_sold=include_sold,
                center=center,
            )
            if not listings and not confirmed_empty(combined):
                raise RuntimeError(
                    "No structured search result or confirmed empty result; Facebook layout is unverified"
                )
            if pricing_spec is not None:
                listings = await self._measure_pricing_listings(listings, center, pricing_spec)
            return SearchResult(
                listings,
                saturated=bool(listings) and more_results(combined),
                radius_km=actual,
                coverage_warning=" ".join(
                    filter(
                        None,
                        [
                            city_note,
                            f"Facebook applied {actual} km (requested {desired} km)"
                            if abs(actual - desired) > 1
                            else None,
                        ],
                    )
                )
                or None,
            )
        finally:
            logger.info(
                "Facebook search %r in %s/%s finished in %.1fs; "
                "pagination responses=%d response_timeouts=%d request_timeouts=%d",
                query,
                country,
                anchor,
                time.monotonic() - started,
                pagination_responses,
                pagination_timeouts,
                pagination_request_timeouts,
            )
            await page.close()
            if pending:
                await asyncio.gather(*list(pending), return_exceptions=True)

    async def _measure_pricing_listings(self, listings, center, spec):
        """Search cards omit coordinates; inspect a bounded set of matching details."""
        measured = list(listings)
        checked = 0
        try:
            async with asyncio.timeout(60):
                for index, listing in enumerate(listings):
                    if listing.distance_km is not None or not matches(spec, listing):
                        continue
                    if listing.status not in ("active", "sold") or checked >= 24:
                        continue
                    checked += 1
                    try:
                        async with asyncio.timeout(10):
                            point = await self._detail_coordinates(listing)
                    except AccessBlocked:
                        raise
                    except (TimeoutError, RuntimeError):
                        continue  # Unknown stays excluded, never assumed local.
                    measured[index] = replace(listing, distance_km=distance_km(center, point))
        except TimeoutError:
            pass  # The report explicitly counts unmatched distance evidence.
        return measured

    async def _detail_coordinates(self, listing):
        page = await self.context.new_page()
        try:
            # Construct the canonical URL from the parsed numeric ID, never a seller URL.
            response = await page.goto(
                f"https://www.facebook.com/marketplace/item/{listing.id}/",
                wait_until="domcontentloaded",
                timeout=8000,
            )
            if response and response.status in (401, 403, 429):
                raise AccessBlocked(f"Facebook HTTP {response.status}; login or rate limit")
            await check_access(page)
            for attempt in range(4):
                documents = []
                for script in await page.locator(
                    'script[type="application/json"]'
                ).all_text_contents():
                    documents.extend(json_documents(script))
                point = listing_coordinates(documents, listing.id)
                if point is not None:
                    return point
                if attempt < 3:
                    await asyncio.sleep(0.25)
            return None
        finally:
            await page.close()


async def check_access(page):
    if any(marker in page.url for marker in ("/login", "/checkpoint", "/captcha")):
        raise AccessBlocked("Facebook requires interactive login/checkpoint resolution")
    if await page.locator('input[type="password"]').count():
        raise AccessBlocked("Facebook login wall; run scout login")
    text = (await page.locator("body").inner_text()).casefold()
    if any(
        marker in text
        for marker in (
            "you're temporarily blocked",
            "you’re temporarily blocked",
            "confirm you're human",
            "marketplace isn't available to you",
            "marketplace isn’t available to you",
        )
    ):
        raise AccessBlocked("Facebook access restricted; inspect the browser")
    await configure_partner_selection(page)


class _FacebookScanSession:
    """Own the provider lock and preference snapshot for one batch of searches."""

    def __init__(self, provider):
        self.provider = provider
        self.snapshot = provider.settings.data_dir / "facebook-home.json"
        self.page = None
        self.home = None
        self._entered = False
        self._closed = False
        self._lock_acquired = False
        self._query_lock = asyncio.Lock()

    async def __aenter__(self):
        try:
            async with asyncio.timeout(SESSION_START_TIMEOUT):
                await self.provider._search_lock.acquire()
                self._lock_acquired = True
                await self.provider.start()
                if self.provider.settings.facebook_restore_home:
                    self.page = await self.provider.context.new_page()
                    await self.provider._recover_snapshot(self.page, self.snapshot)
                    self.home = await capture_home(self.page, check_access)
                    save_home(self.snapshot, self.home)
                self._entered = True
                return self
        except BaseException as exc:
            if self.page is not None:
                try:
                    await asyncio.wait_for(self.page.close(), timeout=10)
                except Exception as close_error:
                    exc.add_note(f"Could not close Facebook session page: {close_error}")
            self._release_lock()
            raise

    async def __aexit__(self, exc_type, exc, traceback):
        if not self._entered or self._closed:
            return False
        self._closed = True
        scan_error = exc
        try:
            cleanup = asyncio.create_task(self._wait_for_queries_and_restore())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as cancelled:
                try:
                    await cleanup
                except Exception as restore_error:
                    cancelled.add_note(str(restore_error))
                    raise cancelled from restore_error
                raise
            except Exception as restore_error:
                if isinstance(scan_error, (asyncio.CancelledError, AccessBlocked)):
                    scan_error.add_note(str(restore_error))
                    raise scan_error from restore_error
                raise
        finally:
            try:
                if self.page is not None:
                    await self.page.close()
            finally:
                self._release_lock()
        return False

    async def search(
        self, query, country, anchor, *, radius_km=None, include_sold=False, pricing_spec=None
    ):
        # _search changes account-wide Marketplace radius when Facebook does not
        # apply the requested region. Serialize queries even within this session.
        async with self._query_lock:
            if not self._entered or self._closed:
                raise RuntimeError("Facebook scan session is not active")
            if radius_km is None and not include_sold:
                return await self.provider._search(query, country, anchor)
            return await self.provider._search(
                query,
                country,
                anchor,
                radius_km=radius_km,
                include_sold=include_sold,
                **({"pricing_spec": pricing_spec} if pricing_spec is not None else {}),
            )

    async def _wait_for_queries_and_restore(self):
        # Drain an in-flight query before restoring account-wide preferences. Marking
        # the session closed first makes queued callers fail after the current query.
        async with self._query_lock:
            if self.provider.settings.facebook_restore_home:
                await self.provider._restore_home(self.page, self.home, self.snapshot)

    def _release_lock(self):
        if self._lock_acquired:
            self.provider._search_lock.release()
            self._lock_acquired = False
