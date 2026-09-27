# NYS Parks Camping Checker

## What this is

A CLI tool that checks campsite availability at New York State Parks by
querying ReserveAmerica (the booking platform NY State Parks uses), for a
given park, start date, and number of nights. It reports which specific
sites are available for that exact stay.

## How to run it

```bash
pip install -r requirements.txt
python nys_camping_checker.py --park-id 297 --start-date 2026-07-04 --nights 3
```

`--park-id` is Hither Hills State Park's numeric ReserveAmerica ID (297) in
the example above. See "Adding a new park" below for how to find a park's ID.

## No public API — this depends on ReserveAmerica's page structure

ReserveAmerica does not offer a public API. `check_availability()` in
`nys_camping_checker.py` fetches ReserveAmerica's campsite search page and
parses a JSON blob embedded in the HTML (a `<script id="initialState">` tag
containing the page's server-rendered application state). Per-site
availability for the requested dates lives inside that JSON, under
`backend.productSearch.searchResults`.

This means the tool is coupled to ReserveAmerica's internal page structure,
not a stable contract. If ReserveAmerica redesigns the page, renames that
script tag, or restructures the JSON, this will break — most likely as a
`RuntimeError` from `_fetch_search_results()` (e.g. "Could not find the
embedded page state" or "missing the expected search-results structure").
If you hit one of those, the fix is to re-inspect the live page and update
the parsing logic in `_fetch_search_results()`, not to route around it.

## Selenium/Playwright: not used, and not needed

We investigated this in depth before implementing. Short version:

- ReserveAmerica's rendered page is mostly empty HTML — the real data is a
  large inline JSON state blob, so no JS execution is needed to read it.
- There *is* a separate AJAX API (`api.reserveamerica.com/jaxrs-json/...`)
  that the site's own frontend calls for instant, client-side search
  updates. That endpoint sits behind AWS WAF bot protection and rejects
  plain HTTP requests (including WAF cookies copied from a real browser
  session) — it effectively requires a live browser to pass its challenge.
- However, the plain server-rendered page (`www.reserveamerica.com/...`)
  turns out to accept the same search as query params —
  `availStartDate`, `arrivalDate`, `lengthOfStay`, `pageNumber` — and
  returns the matching per-site availability embedded in that page's
  `initialState` JSON. That's a normal HTML response to a normal
  `requests.get()`, no browser required.

So: **do not add Selenium or Playwright to this project.** If a future
change to ReserveAmerica breaks the server-rendered query-param path and
only the WAF-protected AJAX API remains, that would be a real reason to
revisit browser automation — but that isn't the current situation, and
adding that dependency preemptively is unwarranted complexity.

## Rate-limiting etiquette

`check_availability()` paginates through every page of results for a search
(a park can have 150+ sites across 9+ pages of 20), and `_throttle()`
enforces a minimum `REQUEST_DELAY_SECONDS` (2.5s) between consecutive
requests to ReserveAmerica, including between pages within a single
`check_availability()` call.

**Never remove or shrink this delay**, and don't add concurrency (parallel
requests, threads, `asyncio`) that would defeat it. This is someone else's
production site with no public API — the delay is what keeps this tool from
looking like abusive traffic. If a task seems to call for checking many
parks or date ranges quickly, that's a sign to slow down the overall
workflow, not to shrink `REQUEST_DELAY_SECONDS`.

## Adding a new NY state park

You only need the park's numeric ReserveAmerica park ID — pass it as
`--park-id`. The URL slug in `BASE_URL`
(`https://www.reserveamerica.com/explore/park/NY/{park_id}/campsites`) is
hardcoded to the placeholder `park` and deliberately ignored: we confirmed
during development that ReserveAmerica's server only looks at the state
code (`NY`) and numeric ID, not the slug text, so no per-park slug needs to
be stored anywhere in this codebase.

To find a park's ID: search for the park on reserveamerica.com and look at
its campsites page URL — the numeric segment right before `/campsites` is
the park ID (e.g. `.../hither-hills-state-park/NY/297/campsites` → `297`).
