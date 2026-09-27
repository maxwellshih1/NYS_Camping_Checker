import argparse
import json
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.reserveamerica.com/explore/park/NY/{park_id}/campsites"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
REQUEST_DELAY_SECONDS = 2.5
REQUEST_TIMEOUT_SECONDS = 15
MAX_PAGES = 20  # safety cap; a park would need 400+ sites to exceed this

_last_request_time = None


def _throttle():
    """Sleep as needed so repeated requests stay a few seconds apart."""
    global _last_request_time
    if _last_request_time is not None:
        elapsed = time.monotonic() - _last_request_time
        if elapsed < REQUEST_DELAY_SECONDS:
            time.sleep(REQUEST_DELAY_SECONDS - elapsed)
    _last_request_time = time.monotonic()


def _fetch_search_results(session, park_id, start_date, nights, page_number):
    """Fetch and parse one page of campsite search results for the given stay.

    ReserveAmerica's campsites page is server-rendered: passing availStartDate/
    arrivalDate/lengthOfStay/pageNumber as query params makes the server embed
    the matching search results directly in the page's initialState JSON blob,
    so no JS execution is needed to read them.
    """
    url = BASE_URL.format(park_id=park_id)
    params = {
        "availStartDate": start_date.isoformat(),
        "arrivalDate": start_date.isoformat(),
        "lengthOfStay": nights,
        "pageNumber": page_number,
    }

    _throttle()
    try:
        response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise ConnectionError(f"Failed to reach ReserveAmerica: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"ReserveAmerica returned HTTP {response.status_code} for park {park_id} "
            f"(page {page_number})"
        )

    soup = BeautifulSoup(response.text, "lxml")
    state_tag = soup.find("script", id="initialState")
    if state_tag is None or not state_tag.string:
        raise RuntimeError(
            "Could not find the embedded page state (script#initialState) - "
            "ReserveAmerica may have changed its page structure."
        )

    try:
        state = json.loads(state_tag.string)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Failed to parse ReserveAmerica's embedded page state as JSON") from exc

    try:
        return state["backend"]["productSearch"]["searchResults"]
    except KeyError as exc:
        raise RuntimeError(
            "ReserveAmerica's page state is missing the expected search-results structure"
        ) from exc


def check_availability(park_id, start_date, nights):
    """Check campsite availability on ReserveAmerica for the given park and dates.

    Walks every page of campsite search results for the stay (start_date through
    start_date + nights) and returns a summary plus the specific sites that are
    available for that exact date range.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    end_date = start_date + timedelta(days=nights)

    all_records = []
    total_pages = 1
    page_number = 0
    while page_number < total_pages and page_number < MAX_PAGES:
        results = _fetch_search_results(session, park_id, start_date, nights, page_number)
        total_pages = results.get("totalPages") or 1
        all_records.extend(results.get("records", []))
        page_number += 1

    available_sites = []
    for record in all_records:
        if not record.get("available"):
            continue
        details = record.get("details", {})
        available_sites.append({
            "site": record.get("name"),
            "loop": details.get("loopName"),
            "site_type": record.get("prodGrpName"),
            "booking_status": record.get("bookingStatus"),
        })

    return {
        "park_id": park_id,
        "start_date": start_date,
        "end_date": end_date,
        "nights": nights,
        "total_sites_checked": len(all_records),
        "available_site_count": len(available_sites),
        "available_sites": available_sites,
    }


def _booking_url(park_id, start_date, nights):
    """Build the direct ReserveAmerica URL for this park/dates, for manual booking."""
    params = {
        "availStartDate": start_date.isoformat(),
        "arrivalDate": start_date.isoformat(),
        "lengthOfStay": nights,
    }
    return f"{BASE_URL.format(park_id=park_id)}?{urlencode(params)}"


def _print_notification(result):
    """Print a formatted alert and booking URL, and ring the terminal bell."""
    url = _booking_url(result["park_id"], result["start_date"], result["nights"])
    print()
    print("=" * 60)
    print(f"AVAILABILITY ALERT - Park {result['park_id']}")
    print("=" * 60)
    print(
        f"Dates: {result['start_date']} to {result['end_date']} "
        f"({result['nights']} night{'s' if result['nights'] != 1 else ''})"
    )
    print()
    for site in result["available_sites"]:
        print(f"  Site {site['site']}  (Loop: {site['loop']}, {site['site_type']})")
    print()
    print(f"Book here: {url}")
    print("=" * 60)
    print("\a", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Check NYS campsite availability on ReserveAmerica.",
        epilog="Example: %(prog)s --park-id 297 --start-date 2026-07-04 --nights 3",
    )
    parser.add_argument(
        "--park-id",
        type=int,
        required=True,
        help="Numeric ReserveAmerica park ID (e.g. 297 for Hither Hills)",
    )
    parser.add_argument(
        "--start-date",
        required=True,
        help="Check-in date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--nights",
        type=int,
        required=True,
        help="Number of nights to stay",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "If an available site is found, print a formatted alert, the direct "
            "ReserveAmerica booking URL, and ring the terminal bell"
        ),
    )

    args = parser.parse_args()

    try:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    except ValueError:
        parser.error(f"--start-date must be in YYYY-MM-DD format, got: {args.start_date}")

    if args.nights < 1:
        parser.error("--nights must be at least 1")

    try:
        result = check_availability(args.park_id, start_date, args.nights)
    except (ConnectionError, RuntimeError) as exc:
        parser.exit(1, f"Error checking availability: {exc}\n")

    print(
        f"Park {result['park_id']}: {result['start_date']} to {result['end_date']} "
        f"({result['nights']} night{'s' if result['nights'] != 1 else ''})"
    )
    print(f"{result['available_site_count']} of {result['total_sites_checked']} sites available")
    for site in result["available_sites"]:
        print(f"  {site['site']}  ({site['loop']}, {site['site_type']})")

    if args.notify and result["available_site_count"] > 0:
        _print_notification(result)


if __name__ == "__main__":
    main()
