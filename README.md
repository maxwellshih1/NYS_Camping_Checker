# NYS Parks Camping Checker

A CLI tool that checks campsite availability at New York State Parks by
querying ReserveAmerica (the booking platform NY State Parks uses), for a
given park, start date, and number of nights. It reports which specific
sites are available for that exact stay, and can optionally alert you
(with a printed notice, terminal bell, and direct booking link) when a
site opens up.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
python nys_camping_checker.py --park-id 297 --start-date 2026-07-04 --nights 3
```

- `--park-id` is the park's numeric ReserveAmerica ID (297 = Hither Hills
  State Park in the example above). To find a different park's ID, search
  for it on reserveamerica.com and look at its campsites page URL — the
  numeric segment right before `/campsites` is the park ID.
- `--start-date` is the check-in date, in `YYYY-MM-DD` format.
- `--nights` is the length of stay.
- `--notify` (optional) prints a formatted alert, rings the terminal bell,
  and includes a direct booking URL if any site is available for the
  requested stay.

## How it works, and a disclaimer

ReserveAmerica doesn't offer a public API, so this tool works by fetching
ReserveAmerica's server-rendered campsite search page and parsing the
availability data embedded in it. That means it's coupled to
ReserveAmerica's page structure rather than a stable, documented contract
— it can break if ReserveAmerica changes their site. It's intended for
personal/educational use; please use it responsibly and be mindful of
ReserveAmerica's terms of service and request load on their site.
