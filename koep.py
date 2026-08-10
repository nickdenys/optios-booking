#!/usr/bin/env python3
"""Book, inspect and watch appointments at KÖP (Wetteren) via the Optios API.

Stdlib only, no third-party dependencies. Every write action requires --confirm.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

API = "https://client.optios.net/api/anonymous"
ESTABLISHMENT_ID = "3600"
ESTABLISHMENT_UUID = "907658a7-ec50-11e8-a545-0a4f0303a374"

CONFIG_PATH = os.path.expanduser("~/.config/koep/config.json")
CACHE_DIR = os.path.expanduser("~/.cache/koep")
MENU_CACHE = os.path.join(CACHE_DIR, "menu.json")
SEEN_PATH = os.path.join(CACHE_DIR, "seen.json")
MENU_TTL = 24 * 3600

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class KoepError(Exception):
    """Any failure that should be reported to the caller rather than swallowed."""


# ---------------------------------------------------------------- http


def request(method, path, body=None, params=None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Origin", "https://booking.optios.net")
    req.add_header("Referer", "https://booking.optios.net/")
    req.add_header("User-Agent", "koep-cli/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise KoepError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise KoepError(f"{method} {path} could not reach the server: {exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KoepError(f"{method} {path} returned non-JSON: {raw[:200]}") from exc


# ---------------------------------------------------------------- config


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise KoepError(
            f"No config at {CONFIG_PATH}. Run: koep.py init --first-name X --last-name Y "
            "--email a@b.c --phone +32..."
        )
    with open(CONFIG_PATH) as handle:
        return json.load(handle)


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def require_identity(config):
    missing = [k for k in ("first_name", "last_name", "email", "phone") if not config.get(k)]
    if missing:
        raise KoepError(f"Config {CONFIG_PATH} is missing: {', '.join(missing)}")


# ---------------------------------------------------------------- menu


def load_menu(refresh=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fresh = os.path.exists(MENU_CACHE) and time.time() - os.path.getmtime(MENU_CACHE) < MENU_TTL
    if fresh and not refresh:
        with open(MENU_CACHE) as handle:
            return json.load(handle)
    menu = request("GET", "activity-menu", params={"establishment_id": ESTABLISHMENT_ID})
    if not isinstance(menu, list):
        raise KoepError("activity-menu returned an unexpected shape; the API may have changed")
    with open(MENU_CACHE, "w") as handle:
        json.dump(menu, handle)
    return menu


def iter_services(menu):
    """Yield (path, leaf) for every bookable menu leaf."""

    def walk(nodes, path):
        for node in nodes:
            here = path + [node["name"].strip()]
            if node.get("is_menu") and node.get("activities"):
                if node.get("is_online", True):
                    yield " > ".join(here), node
            yield from walk(node.get("categories") or [], here)

    yield from walk(menu, [])


def service_price(leaf):
    total = 0
    for activity in leaf["activities"]:
        details = activity.get("establishment_details") or [{}]
        price = details[0].get("price")
        if price is None:
            price = activity.get("activity_menu_price") or 0
        total += price
    return total


def service_duration(leaf):
    return sum((a.get("duration") or 0) + (a.get("wait_time") or 0) for a in leaf["activities"])


def resolve_service(menu, query):
    """Resolve a service by menu-leaf id or by a case-insensitive path match."""
    services = list(iter_services(menu))
    if query.isdigit():
        for path, leaf in services:
            if str(leaf["id"]) == query:
                return path, leaf
        raise KoepError(f"No bookable service with menu id {query}")

    needle = query.lower().strip()
    exact = [(p, l) for p, l in services if p.lower() == needle or l["name"].strip().lower() == needle]
    if len(exact) == 1:
        return exact[0]

    def normalise(text):
        return " > ".join(part.strip() for part in text.lower().split(">"))

    suffix = [(p, l) for p, l in services if normalise(p).endswith(normalise(needle))]
    if len(suffix) == 1:
        return suffix[0]

    terms = [t for t in needle.replace(">", " ").split() if t]
    matches = [(p, l) for p, l in services if all(t in p.lower() for t in terms)]
    if not matches:
        raise KoepError(f"No service matches {query!r}. Run: koep.py services")
    if len(matches) > 1:
        listing = "\n".join(f"  {p}  (id {l['id']})" for p, l in matches[:15])
        raise KoepError(f"{query!r} is ambiguous, {len(matches)} matches:\n{listing}")
    return matches[0]


def blocks_for(leaf, person_id):
    return [
        {"activity_id": a["id"], "category_id": leaf["id"], "person_id": person_id}
        for a in leaf["activities"]
    ]


# ---------------------------------------------------------------- persons


def load_persons():
    data = request("GET", "persons", params={"establishment_id": ESTABLISHMENT_ID})
    people = data.get("data", data) if isinstance(data, dict) else data
    out = {}
    for person in people:
        attrs = person.get("attributes", person) if isinstance(person, dict) else {}
        pid = attrs.get("id") or person.get("id")
        name = attrs.get("name") or " ".join(
            filter(None, [attrs.get("first_name"), attrs.get("last_name")])
        )
        if pid and name:
            out[int(pid)] = name.strip()
    return out


def resolve_person(name):
    if name is None:
        return None
    if str(name).lower() in ("any", "none", "geen", "geen voorkeur"):
        return None
    persons = load_persons()
    if str(name).isdigit():
        return int(name)
    matches = [pid for pid, pname in persons.items() if name.lower() in pname.lower()]
    if not matches:
        listing = ", ".join(sorted(persons.values()))
        raise KoepError(f"No stylist matches {name!r}. Known: {listing}")
    if len(matches) > 1:
        listing = ", ".join(persons[p] for p in matches)
        raise KoepError(f"{name!r} is ambiguous: {listing}")
    return matches[0]


# ---------------------------------------------------------------- availability


def find_slots(leaf, person_id, start, end, token=None):
    """Query the free-moment finder week by week and return a flat, sorted slot list."""
    customers = [{"token": token, "blocks": blocks_for(leaf, person_id)}]
    payload = {"establishment_id": ESTABLISHMENT_ID, "customers": customers}

    slots, names = [], {}
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=6), end)
        data = request(
            "POST",
            f"calendar/free-moment-finder/{cursor.isoformat()}/{window_end.isoformat()}",
            body=payload,
        )
        cursor = window_end + timedelta(days=1)
        if not isinstance(data, dict):
            continue
        for key, person in (data.get("persons") or {}).items():
            full = " ".join(filter(None, [person.get("first_name"), person.get("last_name")]))
            names[int(person["id"])] = full.strip()
        for day, info in (data.get("days") or {}).items():
            for bucket, moments in (info.get("free_moments") or {}).items():
                score = int(bucket.replace("score_", "")) if bucket.startswith("score_") else 0
                for when, detail in moments.items():
                    combo = detail.get("best_combo") or {}
                    pids = sorted(
                        {p for ids in (combo.get("activity_persons") or {}).values() for p in ids}
                    )
                    slots.append(
                        {
                            "datetime": when,
                            "date": day,
                            "time": when.split(" ")[-1],
                            "score": score,
                            "person_ids": pids,
                        }
                    )
    for slot in slots:
        slot["persons"] = [names.get(p, str(p)) for p in slot["person_ids"]]
    slots.sort(key=lambda s: s["datetime"])
    return slots


def filter_slots(slots, after=None, before=None, weekdays=None):
    out = []
    for slot in slots:
        moment = datetime.strptime(slot["datetime"], "%Y-%m-%d %H:%M")
        if after and moment.time() < datetime.strptime(after, "%H:%M").time():
            continue
        if before and moment.time() > datetime.strptime(before, "%H:%M").time():
            continue
        if weekdays and moment.weekday() not in weekdays:
            continue
        out.append(slot)
    return out


def parse_weekdays(value):
    if not value:
        return None
    days = set()
    for part in value.split(","):
        part = part.strip().lower()[:3]
        if part not in WEEKDAYS:
            raise KoepError(f"Unknown weekday {part!r}. Use mon,tue,wed,thu,fri,sat,sun")
        days.add(WEEKDAYS[part])
    return days


def parse_date(value, default):
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise KoepError(f"Bad date {value!r}, expected YYYY-MM-DD") from exc


# ---------------------------------------------------------------- commands


def cmd_init(args):
    config = {}
    if os.path.exists(CONFIG_PATH):
        config = load_config()
    for field in ("first_name", "last_name", "email", "phone"):
        value = getattr(args, field)
        if value:
            config[field] = value
    config.setdefault("token", None)
    config.setdefault("watch", [])
    save_config(config)
    require_identity(config)
    print(f"Wrote {CONFIG_PATH}")


def cmd_services(args):
    menu = load_menu(refresh=args.refresh)
    rows = []
    for path, leaf in iter_services(menu):
        if args.search and args.search.lower() not in path.lower():
            continue
        rows.append(
            {
                "id": leaf["id"],
                "path": path,
                "price": service_price(leaf),
                "duration": service_duration(leaf),
                "activities": [a["name"] for a in leaf["activities"]],
            }
        )
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("No services matched.")
        return
    width = max(len(r["path"]) for r in rows)
    for row in rows:
        print(f"{row['path']:<{width}}  {row['id']:>8}  EUR {row['price']:>4}  {row['duration']:>3} min")


def cmd_slots(args):
    config = load_config() if os.path.exists(CONFIG_PATH) else {}
    menu = load_menu()
    path, leaf = resolve_service(menu, args.service)
    person_id = resolve_person(args.person)
    start = parse_date(args.since, date.today())
    end = parse_date(args.until, start + timedelta(days=args.days))
    slots = find_slots(leaf, person_id, start, end, token=config.get("token"))
    slots = filter_slots(slots, args.after, args.before, parse_weekdays(args.weekday))
    if args.limit:
        slots = slots[: args.limit]

    if args.json:
        print(json.dumps({"service": path, "slots": slots}, indent=2, ensure_ascii=False))
        return
    print(f"{path}  (EUR {service_price(leaf)}, {service_duration(leaf)} min)")
    print(f"{start} to {end}" + (f", stylist filter: {args.person}" if args.person else ""))
    if not slots:
        print("\nNo free slots in that window.")
        return
    print()
    for slot in slots:
        who = ", ".join(slot["persons"]) or "unassigned"
        moment = datetime.strptime(slot["datetime"], "%Y-%m-%d %H:%M")
        print(f"  {moment.strftime('%a %d %b %Y  %H:%M')}   {who}")
    print(f"\n{len(slots)} slot(s).")


def cmd_lastminutes(args):
    data = request("GET", "last-minutes", params={"establishment_id": ESTABLISHMENT_ID})
    rows = []
    for item in data.get("data", []):
        attrs = item["attributes"]
        if attrs.get("is_booked"):
            continue
        for activity in attrs["activities"]:
            rows.append(
                {
                    "id": attrs["id"],
                    "starts_at": activity["starts_at"],
                    "name": activity["name"],
                    "price": activity.get("price"),
                    "persons": [p["name"] for p in activity.get("persons") or []],
                }
            )
    rows.sort(key=lambda r: r["starts_at"])
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("No last-minute slots right now.")
        return
    for row in rows:
        moment = datetime.strptime(row["starts_at"], "%Y-%m-%d %H:%M:%S")
        who = ", ".join(row["persons"]) or "unassigned"
        print(f"  {moment.strftime('%a %d %b %Y  %H:%M')}   {row['name']}  EUR {row['price']}  {who}")


def cmd_book(args):
    config = load_config()
    require_identity(config)
    menu = load_menu()
    path, leaf = resolve_service(menu, args.service)
    person_id = resolve_person(args.person)

    try:
        moment = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise KoepError(f"Bad --at {args.at!r}, expected 'YYYY-MM-DD HH:MM'") from exc

    if not args.no_verify:
        day = moment.date()
        available = find_slots(leaf, person_id, day, day, token=config.get("token"))
        offered = {s["datetime"] for s in available}
        if args.at not in offered:
            nearby = ", ".join(sorted(s["time"] for s in available)) or "none"
            raise KoepError(
                f"{args.at} is not offered for this service. Free that day: {nearby}\n"
                "Re-run with --no-verify only if you are certain."
            )

    payload = {
        "establishment_id": ESTABLISHMENT_ID,
        "remark": args.remark or "",
        "date": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "customer_data": {
            "first_name": config["first_name"],
            "last_name": config["last_name"],
            "phone": config["phone"],
            "email": config["email"],
            "send_email": True,
            "street": None,
            "city": None,
            "zip": None,
            "token": config.get("token"),
        },
        "customers": [{"blocks": blocks_for(leaf, person_id)}],
        "giftcard_barcode": None,
        "customer_pays_full_amount": False,
    }

    print("Booking summary")
    print(f"  service   {path}")
    print(f"  when      {moment.strftime('%A %d %B %Y at %H:%M')}")
    print(f"  stylist   {args.person or 'no preference'}")
    print(f"  price     EUR {service_price(leaf)}  ({service_duration(leaf)} min)")
    print(f"  name      {config['first_name']} {config['last_name']}")
    print(f"  email     {config['email']}")
    print(f"  phone     {config['phone']}")
    if args.remark:
        print(f"  remark    {args.remark}")

    if not args.confirm:
        print("\nDRY RUN. Nothing was sent. Re-run with --confirm to actually book.")
        return

    result = request("POST", "appointments", body=payload)
    print("\nBooked. Server response:")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:1500])
    if isinstance(result, dict):
        token = (result.get("customer") or {}).get("token") or result.get("token")
        if token and not config.get("token"):
            config["token"] = token
            save_config(config)
            print(f"\nSaved customer token to {CONFIG_PATH} (needed to list and cancel).")


def cmd_appointments(args):
    config = load_config()
    require_identity(config)
    data = request(
        "GET",
        "calendar/appointments",
        params={
            "establishment_id": ESTABLISHMENT_ID,
            "email": config["email"],
            "token": config.get("token") or "",
        },
    )
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    items = data.get("appointments", data) if isinstance(data, dict) else data
    if not items:
        print("No appointments found for this email.")
        return
    for item in items:
        names = [b.get("activity", {}).get("name") for b in item.get("blocks") or []]
        print(
            f"  id={item.get('id')} uuid={item.get('uuid')}  {item.get('starts_at')}  "
            + ", ".join(filter(None, names))
        )


def cmd_cancel(args):
    config = load_config()
    print(f"Cancel appointment id={args.id} uuid={args.uuid}")
    if not args.confirm:
        print("DRY RUN. Nothing was sent. Re-run with --confirm to actually cancel.")
        return
    result = request(
        "DELETE",
        f"calendar/appointments/{args.id}/{args.uuid}",
        body={"establishment_id": ESTABLISHMENT_ID},
    )
    print("Cancelled.", json.dumps(result, ensure_ascii=False) if result else "")


def cmd_watch(args):
    config = load_config()
    rules = config.get("watch") or []
    if not rules:
        raise KoepError(f'No watch rules in {CONFIG_PATH}. Add a "watch" array.')

    menu = load_menu()
    seen = {}
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH) as handle:
            seen = json.load(handle)

    announcements = []
    for rule in rules:
        if rule.get("enabled") is False:
            continue
        name = rule.get("name") or rule["service"]
        path, leaf = resolve_service(menu, rule["service"])
        person_id = resolve_person(rule.get("person"))
        start = date.today()
        end = start + timedelta(days=int(rule.get("within_days", 21)))
        slots = find_slots(leaf, person_id, start, end, token=config.get("token"))
        slots = filter_slots(
            slots, rule.get("after"), rule.get("before"), parse_weekdays(rule.get("weekday"))
        )
        current = {s["datetime"] for s in slots}
        previous = set(seen.get(name, []))
        fresh = sorted(current - previous)
        seen[name] = sorted(current)
        if fresh and not args.prime:
            announcements.append((name, path, fresh))

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(SEEN_PATH, "w") as handle:
        json.dump(seen, handle, indent=2)

    if args.prime:
        print(f"Primed baseline for {len(rules)} rule(s). Future runs report only new slots.")
        return

    if not announcements:
        if not args.quiet:
            print("No new slots.")
        return

    for name, path, fresh in announcements:
        pretty = [
            datetime.strptime(f, "%Y-%m-%d %H:%M").strftime("%a %d %b %H:%M") for f in fresh[:4]
        ]
        message = ", ".join(pretty) + (f" (+{len(fresh) - 4} more)" if len(fresh) > 4 else "")
        print(f"[{name}] {len(fresh)} new: {message}")
        if args.notify:
            notify(f"KÖP: {name}", message)


def notify(title, message):
    script = (
        f"display notification {json.dumps(message)} with title {json.dumps(title)} "
        'sound name "Ping"'
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


PLIST_LABEL = "net.optios.koep.watch"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{PLIST_LABEL}.plist")


def cmd_install_watcher(args):
    script = os.path.abspath(__file__)
    log = os.path.join(CACHE_DIR, "watch.log")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
    <string>watch</string>
    <string>--notify</string>
    <string>--quiet</string>
  </array>
  <key>StartInterval</key><integer>{args.interval}</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w") as handle:
        handle.write(plist)
    subprocess.run(["launchctl", "unload", PLIST_PATH], check=False, capture_output=True)
    loaded = subprocess.run(["launchctl", "load", PLIST_PATH], check=False, capture_output=True)
    if loaded.returncode != 0:
        raise KoepError(f"launchctl load failed: {loaded.stderr.decode().strip()}")
    print(f"Installed {PLIST_PATH}, running every {args.interval}s.")
    print(f"Logs: {log}")
    print("Remove with: koep.py uninstall-watcher")


def cmd_uninstall_watcher(args):
    subprocess.run(["launchctl", "unload", PLIST_PATH], check=False, capture_output=True)
    if os.path.exists(PLIST_PATH):
        os.remove(PLIST_PATH)
    print("Watcher removed.")


# ---------------------------------------------------------------- cli


def build_parser():
    parser = argparse.ArgumentParser(prog="koep.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write identity to the config file")
    p.add_argument("--first-name")
    p.add_argument("--last-name")
    p.add_argument("--email")
    p.add_argument("--phone")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("services", help="list bookable services")
    p.add_argument("--search")
    p.add_argument("--refresh", action="store_true", help="bypass the 24h menu cache")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_services)

    p = sub.add_parser("slots", help="find free slots for a service")
    p.add_argument("--service", required=True, help="menu path fragment or menu id")
    p.add_argument("--person", help="stylist name, or omit for no preference")
    p.add_argument("--since", help="YYYY-MM-DD, default today")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--days", type=int, default=21, help="window length when --until is absent")
    p.add_argument("--after", help="earliest time of day, HH:MM")
    p.add_argument("--before", help="latest time of day, HH:MM")
    p.add_argument("--weekday", help="comma list, e.g. sat,fri")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_slots)

    p = sub.add_parser("lastminutes", help="list last-minute offers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_lastminutes)

    p = sub.add_parser("book", help="book an appointment (dry run unless --confirm)")
    p.add_argument("--service", required=True)
    p.add_argument("--at", required=True, help="'YYYY-MM-DD HH:MM'")
    p.add_argument("--person")
    p.add_argument("--remark")
    p.add_argument("--no-verify", action="store_true", help="skip the availability pre-check")
    p.add_argument("--confirm", action="store_true", help="actually send the booking")
    p.set_defaults(func=cmd_book)

    p = sub.add_parser("appointments", help="list appointments for the configured email")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_appointments)

    p = sub.add_parser("cancel", help="cancel an appointment (dry run unless --confirm)")
    p.add_argument("--id", required=True)
    p.add_argument("--uuid", required=True)
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("watch", help="report slots that are new since the last run")
    p.add_argument("--notify", action="store_true", help="send a macOS notification")
    p.add_argument("--quiet", action="store_true", help="print nothing when there is no news")
    p.add_argument("--prime", action="store_true", help="record the baseline without reporting")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("install-watcher", help="install the LaunchAgent for periodic watching")
    p.add_argument("--interval", type=int, default=1800, help="seconds between runs")
    p.set_defaults(func=cmd_install_watcher)

    p = sub.add_parser("uninstall-watcher", help="remove the LaunchAgent")
    p.set_defaults(func=cmd_uninstall_watcher)

    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KoepError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
