# Architecture and limits

Scout runs independently with a FastAPI dashboard, a single Playwright worker and SQLite
state. Telegram is optional and outbound-only. It never calls `getUpdates` or changes another
bot's commands. Dashboard data routes require a browser session or API bearer token and reject cross-origin requests.
One-use launcher links create revocable browser sessions without copying the API token.

## Regional definitions and attribution

`src/scout/regions.json` comes from
[gmoz22/facebook-marketplace-nationwide](https://github.com/gmoz22/facebook-marketplace-nationwide),
`config/site.ts` at commit `d5f02ea075e89e6f8e2460cb218f1077dcaee2e8`.
Its MIT notice is retained in `docs/nationwide-LICENSE.txt` and packaged with Scout's license.
USA uses 13 anchors including Alaska/Hawaii, Canada uses 9, and mainland France uses 2.
The original project opens search tabs; Scout's collector and scheduling code are independent.

## Data flow

1. Search/watch definitions expand into regional jobs. Interactive countries are visited
   round-robin. At most two interactive regions run before a due scheduled region.
2. A single process lock protects the browser profile. The worker delays checks by 30 seconds
   by default and gives each provider call a three-minute deadline.
3. Facebook data comes from rendered structured listings and search responses. The collector
   stops at login/checkpoint walls. It does not solve CAPTCHAs, rotate proxies or bypass access.
4. Title phrases, exclusions, alternative groups and per-currency price limits filter records.
   Optional local photo checks gate scheduled notifications, not interactive search results.
5. First observation and alert enqueue share one SQLite transaction. Listing IDs deduplicate
   across regions and restarts for each watch. Separate watches can each alert for one item.
6. Telegram deliveries retry with backoff. Delivery is at-least-once: a crash after acceptance
   but before recording success can produce a duplicate.

Initial scans queue at most ten title-only matches per watch. Photo-enabled watches queue
strong visual matches and retain uncertain candidates for manual review. Delivered IDs are
never reconsidered by photo-cache changes. See [photo filtering](photo-filter.md).

Interactive browsing does not enqueue alerts. Cancellation retains completed results and
ignores an in-flight result. Restart recovery requeues unfinished jobs. Watch conversion is
transactional and idempotent: capacity validation, watch creation, matched-ID baselines and
completed scan times are committed together. Pending regions retain first-pass behavior.

## Local price checks

A search with `spec.pricing` creates one city/radius job in the same queue. It cannot become
a notification watch. The worker enables sold/pending record collection only for these
checks; ordinary searches and watches retain their active-listing behavior. The pricing
endpoint uses all matching records, independent of display pagination, and computes paid,
active asking-price statistics in the selected country's currency without conversion.
The median is the suggested price with at least three comparables; quartiles are alternative
starting prices, not sale-speed predictions. Outliers remain visible in the minimum/mean/maximum.

`price_observations` stores the latest listing payload and observed active/sold timestamps,
keyed by query/filter/area scope and provider listing ID. Recording observations and completing
the job share the search-result transaction; cancelled in-flight results cannot update history.
Old observations expire after 90 days without a sighting, independently of recent-search
retention. Reports include sold sightings from the last 90 days. A missing listing does not
change its status. An explicitly active relisting starts a new lifecycle. Repeated scans are
manual; there is no background sold-item tracker. The graph pairs the last observed active
asking price with elapsed time from first active sighting to first sold sighting. Neither
transaction prices nor actual listing/sale dates are inferred.

Local pricing applies a strict distance filter after title matching. A listing is usable only
when its provider coordinates yield a finite nonnegative distance from the verified search
center and that distance is no greater than the requested radius. Missing, malformed and
outside-radius distances are reported separately for current matches and do not enter active
statistics, comparables or newly recorded history. Existing observations are filtered again at
report time, so legacy payloads without a distance cannot create an estimate; the report asks
the operator to rescan the area. Search cards often omit seller coordinates, so the provider
checks at most 24 matching active or sold listing detail pages within a 60-second budget.
Distances are approximate great-circle values; detail lookups that fail or return no coordinates
remain unknown and are excluded.

## Capacity and coverage

Watches and interactive searches each have a local 100-region/hour admission budget.
This is not an upstream-approved quota or a timing guarantee. Browser latency, backlog,
access blocks and Telegram failures affect delivery time. The latest 20 searches are visible;
younger history is retained internally so cleanup cannot reset the hourly search budget.

Facebook's default relevance order is used because forced newest-first can hide model-specific
matches. Exact-query requests do not make returned results exhaustive. The collector scrolls
a bounded number of times and may miss listings outside that window. Facebook can include
recommendations across borders, cap results, change locale/currency behavior, ignore radius
settings or change its page structure. Applied radius and known uncertainty are recorded.
Country tabs identify where a search was centered, not verified seller nationality/location.
France's anchors do not cover overseas territories. Unknown layouts fail explicitly; only
an explicit search-scoped no-results record counts as a confirmed empty result.

There are no completeness, price-drop, sold-state or delivery-time guarantees. Regional
access and unattended reliability require testing against the operator's own permitted use.

## Access and data handling

[Meta's explanation](https://about.fb.com/news/2021/04/how-we-combat-scraping/) states that
using automation to collect Facebook data without permission violates its terms. A working
login or Scout's MIT license does not supply that permission.

Browser sessions, tokens, source responses, listing snapshots and reference images are
operator data, excluded from releases. Photos are optional and can expire; the dashboard
accepts only HTTPS Facebook/CDN links and builds listing text with text nodes. The application
is intended for one trusted operator, not multiple tenants or an untrusted public sign-up flow.
