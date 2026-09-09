# Optional reference-photo checks

Photo checks compare listing thumbnails to reference JPEGs you supply. They are a conservative
notification gate, not product identification or an accuracy guarantee. Interactive results
remain broad. No reference or seller images are distributed with Scout.

## Configure a profile

Create a subdirectory under `data/image-references`, for example:

```text
data/image-references/example-camera/front.jpg
data/image-references/example-camera/back.jpg
```

Use your own images or images you have permission to use. Each profile must have 1–5 valid
`.jpg` JPEG files, each at most 2 MB and four million pixels. Folder names are lowercase
letters/digits, hyphens or underscores, at most 64 characters, starting with a letter/digit.
The folder name is the stable profile ID; labels are derived from it in the dashboard.
Set `SCOUT_IMAGE_REFERENCES` to use another root directory and restart after changing that
setting. Image files themselves are rediscovered as they change.

Only available profiles appear under **Refine search → Notification photo check**. The field
is hidden when none are available. Requests selecting a missing/invalid profile are rejected.
An existing watch whose references disappear keeps its photo gate: candidates remain quiet
and reviewable instead of silently switching to title-only alerts. Reusing a saved search
with unavailable references requires choosing a configured profile or explicitly selecting
**Title filters only**. Absolute reference paths are not returned by the API.

## Matching, caching and review

One isolated process uses OpenCV SIFT with at most 400 features on a 320×320 image and bounded
RANSAC matching. A strong match needs at least 12 consistent features covering 8% of the
candidate. This can miss obscured objects, unusual viewpoints or poor images; low scores
mean uncertain, not a confirmed non-match.

Decisions are cached by watch/listing, stable thumbnail path, profile ID and a content
fingerprint of the reference images. Changing references invalidates old comparisons while
preserving delivered/baselined IDs and queued alerts. Uncertain comparisons retry after seven
days; transient errors retry after an hour. Deferred work retries subject to the shared budget.

Uncertain candidates appear under **My watches → Review photos**. **Send this to Telegram**
explicitly queues one candidate idempotently; its watch must be enabled and title filters
must still match. Approval does not train the model. Photo matches bypass the title-only
first-ten cap; there is no automatic alert for missing or unreadable references.

## Resource limits

- One image subprocess, one native CPU thread, low process priority.
- CPU soft/hard limits: 2/3 seconds; subprocess wall timeout: 4 seconds; memory limit: 768 MiB.
- At most 8 new comparisons per scan and 60 per hour across photo-enabled watches.
- HTTPS Facebook CDN downloads only; redirects disabled; 2 MB and four million pixel limits.
- Six-second HTTP timeout, twelve-second overall photo check deadline.
- Live candidate image bytes are not retained. Review metadata is capped at 2,000 rows/watch.

The optional `tools/benchmark_photo_filter.py ARCHIVE REFERENCES` operates only on an
operator-supplied private archive. It expects `updated-watch.json`, `alerts.json`, and
`<alert>.jpg` images (where `alert` is each record's archive ID). It writes `replay.json` in
that archive and does not contact Facebook or Telegram. Never commit these private fixtures.
