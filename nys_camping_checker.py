import argparse
import json
import os
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
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
MIN_MONITOR_INTERVAL_SECONDS = 60
ALERT_SOUND_PATH = "/System/Library/Sounds/Glass.aiff"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ENV_SENDER = "CAMPING_ALERT_EMAIL"
EMAIL_ENV_APP_PASSWORD = "CAMPING_ALERT_APP_PASSWORD"

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


def _print_notification(result, sites=None):
    """Print a formatted alert and booking URL, and ring the terminal bell.

    Prints all of ``result["available_sites"]`` by default; pass ``sites`` to
    alert on a specific subset instead (used by --monitor for new openings).
    """
    if sites is None:
        sites = result["available_sites"]
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
    for site in sites:
        print(f"  Site {site['site']}  (Loop: {site['loop']}, {site['site_type']})")
    print()
    print(f"Book here: {url}")
    print("=" * 60)
    print("\a", end="", flush=True)


def _site_id(site):
    """A key identifying a site, stable across checks, for new-opening tracking."""
    return (site["loop"], site["site"])


def _play_alert_sound():
    """Play the macOS system alert sound, for noticing new openings from afar."""
    try:
        subprocess.run(["afplay", ALERT_SOUND_PATH], check=False)
    except FileNotFoundError:
        print("Warning: could not play alert sound (afplay not found - are you on macOS?)")


def _email_body(result, sites):
    """Build the plaintext body for a new-opening email alert."""
    url = _booking_url(result["park_id"], result["start_date"], result["nights"])
    lines = [f"New opening(s) found at park {result['park_id']}:", ""]
    for site in sites:
        lines.append(f"  Site {site['site']}  (Loop: {site['loop']}, {site['site_type']})")
    lines += [
        "",
        f"Dates: {result['start_date']} to {result['end_date']} "
        f"({result['nights']} night{'s' if result['nights'] != 1 else ''})",
        "",
        f"Book here: {url}",
    ]
    return "\n".join(lines)


def _send_email_alert(sender, app_password, recipient, park_id, body):
    """Email a new-opening alert via Gmail SMTP. Never raises - logs and returns."""
    msg = MIMEText(body)
    msg["Subject"] = f"Campsite opened: Park {park_id}"
    msg["From"] = sender
    msg["To"] = recipient

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=REQUEST_TIMEOUT_SECONDS) as server:
            server.starttls()
            server.login(sender, app_password)
            server.sendmail(sender, [recipient], msg.as_string())
    except Exception as exc:
        print(f"Warning: failed to send email alert: {exc}")


def monitor_availability(
    park_id,
    start_date,
    nights,
    interval,
    sound=False,
    email_sender=None,
    email_app_password=None,
    email_recipient=None,
):
    """Poll check_availability() in a loop, alerting only on newly opened sites.

    Runs until interrupted with Ctrl+C. Sleeps ``interval`` seconds between
    checks; the existing per-request throttle inside check_availability()
    still applies within each check. If ``sound`` is set, also plays the
    macOS alert sound whenever a new opening is found. If ``email_sender`` is
    set, also emails ``email_recipient`` whenever a new opening is found.
    """
    print(
        f"Monitoring park {park_id}: {start_date} for {nights} night"
        f"{'s' if nights != 1 else ''}, checking every {interval}s. "
        "Press Ctrl+C to stop."
    )

    seen_available = set()
    try:
        while True:
            timestamp = datetime.now().strftime("%H:%M:%S")
            try:
                result = check_availability(park_id, start_date, nights)
            except (ConnectionError, RuntimeError) as exc:
                print(f"[{timestamp}] Check failed: {exc}")
            else:
                new_sites = [
                    site
                    for site in result["available_sites"]
                    if _site_id(site) not in seen_available
                ]
                seen_available.update(_site_id(site) for site in result["available_sites"])

                if new_sites:
                    _print_notification(result, sites=new_sites)
                    if sound:
                        _play_alert_sound()
                    if email_sender:
                        _send_email_alert(
                            email_sender,
                            email_app_password,
                            email_recipient,
                            park_id,
                            _email_body(result, new_sites),
                        )
                else:
                    print(f"[{timestamp}] Checked — no new openings")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped monitoring.")


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
    parser.add_argument(
        "--monitor",
        action="store_true",
        help=(
            "Keep checking on a loop and alert only when a NEW site opens up "
            "(use --interval to control how often)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help=(
            f"Seconds to wait between checks in --monitor mode (default: 300, "
            f"minimum: {MIN_MONITOR_INTERVAL_SECONDS})"
        ),
    )
    parser.add_argument(
        "--sound",
        action="store_true",
        help=(
            "In --monitor mode, also play the macOS alert sound on a new "
            "opening (requires --monitor)"
        ),
    )
    parser.add_argument(
        "--email",
        nargs="?",
        const="",
        default=None,
        metavar="ADDRESS",
        help=(
            "In --monitor mode, also email ADDRESS on a new opening (requires "
            "--monitor; defaults to the sender address if ADDRESS is omitted). "
            f"Reads credentials from the {EMAIL_ENV_SENDER} and "
            f"{EMAIL_ENV_APP_PASSWORD} environment variables."
        ),
    )

    args = parser.parse_args()

    if args.sound and not args.monitor:
        parser.error("--sound requires --monitor")

    if args.email is not None and not args.monitor:
        parser.error("--email requires --monitor")

    email_sender = email_app_password = email_recipient = None
    if args.email is not None:
        email_sender = os.environ.get(EMAIL_ENV_SENDER)
        email_app_password = os.environ.get(EMAIL_ENV_APP_PASSWORD)
        if not email_sender or not email_app_password:
            print(
                f"Error: --email requires the {EMAIL_ENV_SENDER} and "
                f"{EMAIL_ENV_APP_PASSWORD} environment variables to be set.\n\n"
                f"  export {EMAIL_ENV_SENDER}=\"you@gmail.com\"\n"
                f"  export {EMAIL_ENV_APP_PASSWORD}=\"xxxxxxxxxxxxxxxx\"\n\n"
                f"{EMAIL_ENV_APP_PASSWORD} is a 16-character Gmail \"App Password\" "
                "(not your regular Gmail password) - create one at "
                "https://myaccount.google.com/apppasswords (requires 2-Step "
                "Verification to be enabled on the account).",
                file=sys.stderr,
            )
            sys.exit(1)
        email_recipient = args.email if args.email else email_sender

    try:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    except ValueError:
        parser.error(f"--start-date must be in YYYY-MM-DD format, got: {args.start_date}")

    if args.nights < 1:
        parser.error("--nights must be at least 1")

    if args.monitor:
        interval = args.interval
        if interval < MIN_MONITOR_INTERVAL_SECONDS:
            print(
                f"Warning: --interval {interval}s is below the minimum of "
                f"{MIN_MONITOR_INTERVAL_SECONDS}s; using {MIN_MONITOR_INTERVAL_SECONDS}s instead."
            )
            interval = MIN_MONITOR_INTERVAL_SECONDS
        monitor_availability(
            args.park_id,
            start_date,
            args.nights,
            interval,
            sound=args.sound,
            email_sender=email_sender,
            email_app_password=email_app_password,
            email_recipient=email_recipient,
        )
        return

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
