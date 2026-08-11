#!/usr/bin/env python3
"""Book, inspect and watch appointments at any salon on the Optios platform.

Stdlib only, no third-party dependencies. Every write action requires --confirm.
Which salon a command acts on comes from the config, --salon, or OPTIOS_SALON.
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

CONFIG_PATH = os.path.expanduser(os.environ.get("OPTIOS_CONFIG", "~/.config/optios/config.json"))
CACHE_ROOT = os.path.expanduser("~/.cache/optios")
MENU_TTL = 24 * 3600

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


class OptiosError(Exception):
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
    req.add_header("User-Agent", "optios-cli/1.0")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise OptiosError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OptiosError(f"{method} {path} could not reach the server: {exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OptiosError(f"{method} {path} returned non-JSON: {raw[:200]}") from exc


# ---------------------------------------------------------------- config


def load_config():
    if not os.path.exists(CONFIG_PATH):
        raise OptiosError(
            f"No config at {CONFIG_PATH}. Run: optios.py init --first-name X --last-name Y "
            "--email a@b.c --phone +32..."
        )
    with open(CONFIG_PATH) as handle:
        config = json.load(handle)
    if "salons" not in config and ("watch" in config or "token" in config):
        raise OptiosError(
            f"{CONFIG_PATH} still uses the old single-salon layout. Move 'token' and 'watch' "
            'under salons.<key>, give that salon an "id", and set "default_salon". '
            "See config.example.json."
        )
    return config


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def require_identity(config):
    missing = [k for k in ("first_name", "last_name", "email", "phone") if not config.get(k)]
    if missing:
        raise OptiosError(f"Config {CONFIG_PATH} is missing: {', '.join(missing)}")


# ---------------------------------------------------------------- salons


class Salon:
    """One establishment, plus the cache and watcher state that belong to it."""

    def __init__(self, key, establishment_id, label=None, token=None, watch=None,
                 default_person=None):
        self.key = key
        self.id = str(establishment_id)
        self.label = label or key
        self.token = token
        self.watch = watch or []
        self.default_person = default_person

    @property
    def cache_dir(self):
        return os.path.join(CACHE_ROOT, self.key)

    @property
    def menu_cache(self):
        return os.path.join(self.cache_dir, "menu.json")

    @property
    def seen_path(self):
        return os.path.join(self.cache_dir, "seen.json")

    @property
    def log_path(self):
        return os.path.join(self.cache_dir, "watch.log")

    @property
    def plist_label(self):
        return f"net.optios.watch.{self.key}"

    @property
    def plist_path(self):
        return os.path.expanduser(f"~/Library/LaunchAgents/{self.plist_label}.plist")

    def __str__(self):
        if self.label.endswith(self.id):
            return self.label
        return f"{self.label} (establishment {self.id})"


def resolve_salon(config, wanted=None):
    """Pick the salon to act on: --salon, then OPTIOS_SALON, then the configured default."""
    salons = config.get("salons") or {}
    wanted = wanted or os.environ.get("OPTIOS_SALON") or config.get("default_salon")

    # A bare establishment id works without any config, for one-off lookups.
    if wanted and wanted not in salons and str(wanted).isdigit():
        return Salon(key=str(wanted), establishment_id=wanted, label=f"establishment {wanted}")

    if not salons:
        raise OptiosError(
            f"No salons configured in {CONFIG_PATH}. Run: optios.py add-salon --key mysalon "
            "--establishment 3600 --default"
        )
    if not wanted:
        if len(salons) > 1:
            raise OptiosError(
                f"Several salons configured and no default. Pass --salon, or set "
                f'"default_salon". Known: {", ".join(sorted(salons))}'
            )
        wanted = next(iter(salons))
    if wanted not in salons:
        raise OptiosError(f"Unknown salon {wanted!r}. Known: {', '.join(sorted(salons))}")

    entry = salons[wanted] or {}
    if not entry.get("id"):
        raise OptiosError(f'Salon {wanted!r} has no "id" (the establishment id) in {CONFIG_PATH}.')
    return Salon(
        key=wanted,
        establishment_id=entry["id"],
        label=entry.get("label"),
        token=entry.get("token"),
        watch=entry.get("watch"),
        default_person=entry.get("default_person"),
    )


def select_salon(args):
    """Load whatever config exists and resolve the salon this invocation targets."""
    config = load_config() if os.path.exists(CONFIG_PATH) else {}
    return config, resolve_salon(config, getattr(args, "salon", None))


def save_token(config, salon, token):
    """Persist a customer token against the salon it belongs to."""
    entry = config.setdefault("salons", {}).setdefault(salon.key, {})
    entry.setdefault("id", salon.id)
    entry.setdefault("label", salon.label)
    entry["token"] = token
    save_config(config)


# ---------------------------------------------------------------- menu


def load_menu(salon, refresh=False):
    os.makedirs(salon.cache_dir, exist_ok=True)
    cache = salon.menu_cache
    fresh = os.path.exists(cache) and time.time() - os.path.getmtime(cache) < MENU_TTL
    if fresh and not refresh:
        with open(cache) as handle:
            return json.load(handle)
    menu = request("GET", "activity-menu", params={"establishment_id": salon.id})
    if not isinstance(menu, list):
        raise OptiosError("activity-menu returned an unexpected shape; the API may have changed")
    with open(cache, "w") as handle:
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
        raise OptiosError(f"No bookable service with menu id {query}")

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
        raise OptiosError(f"No service matches {query!r}. Run: optios.py services")
    if len(matches) > 1:
        listing = "\n".join(f"  {p}  (id {l['id']})" for p, l in matches[:15])
        raise OptiosError(f"{query!r} is ambiguous, {len(matches)} matches:\n{listing}")
    return matches[0]


def blocks_for(leaf, person_id):
    return [
        {"activity_id": a["id"], "category_id": leaf["id"], "person_id": person_id}
        for a in leaf["activities"]
    ]


# ---------------------------------------------------------------- persons


def load_persons(salon):
    data = request("GET", "persons", params={"establishment_id": salon.id})
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


def resolve_person(salon, name):
    if name is None:
        return None
    if str(name).lower() in ("any", "none", "geen", "geen voorkeur"):
        return None
    persons = load_persons(salon)
    if str(name).isdigit():
        return int(name)
    matches = [pid for pid, pname in persons.items() if name.lower() in pname.lower()]
    if not matches:
        listing = ", ".join(sorted(persons.values()))
        raise OptiosError(f"No stylist matches {name!r}. Known: {listing}")
    if len(matches) > 1:
        listing = ", ".join(persons[p] for p in matches)
        raise OptiosError(f"{name!r} is ambiguous: {listing}")
    return matches[0]


def preferred_person(salon, requested):
    """The stylist to use: an explicit request, else the salon's default.

    Returns (name, came_from_default) so callers can say which one they applied.
    Pass --person any to ignore the default and search every stylist.
    """
    if requested is not None:
        return requested, False
    return salon.default_person, salon.default_person is not None


# ---------------------------------------------------------------- availability


def find_slots(salon, leaf, person_id, start, end):
    """Query the free-moment finder week by week and return a flat, sorted slot list."""
    customers = [{"token": salon.token, "blocks": blocks_for(leaf, person_id)}]
    payload = {"establishment_id": salon.id, "customers": customers}

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
            raise OptiosError(f"Unknown weekday {part!r}. Use mon,tue,wed,thu,fri,sat,sun")
        days.add(WEEKDAYS[part])
    return days


def parse_date(value, default):
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise OptiosError(f"Bad date {value!r}, expected YYYY-MM-DD") from exc


# ---------------------------------------------------------------- setup commands


def cmd_init(args):
    config = load_config() if os.path.exists(CONFIG_PATH) else {}
    for field in ("first_name", "last_name", "email", "phone"):
        value = getattr(args, field)
        if value:
            config[field] = value
    config.setdefault("salons", {})
    config.setdefault("default_salon", None)
    save_config(config)
    require_identity(config)
    print(f"Wrote {CONFIG_PATH}")
    if not config["salons"]:
        print("Next: optios.py add-salon --key mysalon --establishment 3600 --default")


def cmd_add_salon(args):
    config = load_config() if os.path.exists(CONFIG_PATH) else {}
    salons = config.setdefault("salons", {})
    entry = salons.setdefault(args.key, {})
    entry["id"] = str(args.establishment)
    if args.label:
        entry["label"] = args.label
    if args.default_person:
        entry["default_person"] = None if args.default_person.lower() == "any" else args.default_person
    entry.setdefault("label", args.key)
    entry.setdefault("token", None)
    entry.setdefault("default_person", None)
    entry.setdefault("watch", [])
    if args.default or not config.get("default_salon"):
        config["default_salon"] = args.key
    save_config(config)
    print(f"Saved salon {args.key!r} (establishment {entry['id']}) to {CONFIG_PATH}")
    if config["default_salon"] == args.key:
        print(f"{args.key!r} is now the default salon.")


def cmd_salons(args):
    config = load_config()
    salons = config.get("salons") or {}
    if not salons:
        print(f"No salons configured in {CONFIG_PATH}.")
        return
    if args.json:
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return
    default = config.get("default_salon")
    width = max(len(k) for k in salons)
    for key in sorted(salons):
        entry = salons[key] or {}
        mark = "*" if key == default else " "
        rules = len(entry.get("watch") or [])
        token = "token" if entry.get("token") else "no token"
        stylist = entry.get("default_person") or "any stylist"
        print(
            f" {mark} {key:<{width}}  id {str(entry.get('id')):<8}  "
            f"{entry.get('label') or key}  ({stylist}, {rules} watch rule(s), {token})"
        )
    if default:
        print("\n* = default. Override per command with --salon or OPTIOS_SALON.")


# ---------------------------------------------------------------- commands


def cmd_services(args):
    _, salon = select_salon(args)
    menu = load_menu(salon, refresh=args.refresh)
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
    _, salon = select_salon(args)
    menu = load_menu(salon)
    path, leaf = resolve_service(menu, args.service)
    person, from_default = preferred_person(salon, args.person)
    person_id = resolve_person(salon, person)
    start = parse_date(args.since, date.today())
    end = parse_date(args.until, start + timedelta(days=args.days))
    slots = find_slots(salon, leaf, person_id, start, end)
    slots = filter_slots(slots, args.after, args.before, parse_weekdays(args.weekday))
    if args.limit:
        slots = slots[: args.limit]

    if args.json:
        print(json.dumps({"salon": salon.key, "service": path, "slots": slots}, indent=2, ensure_ascii=False))
        return
    print(f"{salon.label}")
    print(f"{path}  (EUR {service_price(leaf)}, {service_duration(leaf)} min)")
    stylist = ""
    if person:
        stylist = f", stylist filter: {person}" + (" (salon default)" if from_default else "")
    print(f"{start} to {end}{stylist}")
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
    _, salon = select_salon(args)
    data = request("GET", "last-minutes", params={"establishment_id": salon.id})
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
    salon = resolve_salon(config, args.salon)
    menu = load_menu(salon)
    path, leaf = resolve_service(menu, args.service)
    person, from_default = preferred_person(salon, args.person)
    person_id = resolve_person(salon, person)

    try:
        moment = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise OptiosError(f"Bad --at {args.at!r}, expected 'YYYY-MM-DD HH:MM'") from exc

    if not args.no_verify:
        day = moment.date()
        available = find_slots(salon, leaf, person_id, day, day)
        offered = {s["datetime"] for s in available}
        if args.at not in offered:
            nearby = ", ".join(sorted(s["time"] for s in available)) or "none"
            raise OptiosError(
                f"{args.at} is not offered for this service. Free that day: {nearby}\n"
                "Re-run with --no-verify only if you are certain."
            )

    payload = {
        "establishment_id": salon.id,
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
            "token": salon.token,
        },
        "customers": [{"blocks": blocks_for(leaf, person_id)}],
        "giftcard_barcode": None,
        "customer_pays_full_amount": False,
    }

    print("Booking summary")
    print(f"  salon     {salon}")
    print(f"  service   {path}")
    print(f"  when      {moment.strftime('%A %d %B %Y at %H:%M')}")
    print(
        f"  stylist   {person or 'no preference'}"
        + (" (salon default)" if from_default else "")
    )
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
        if token and not salon.token:
            save_token(config, salon, token)
            print(f"\nSaved customer token for {salon.key!r} to {CONFIG_PATH}.")


def cmd_appointments(args):
    config = load_config()
    require_identity(config)
    salon = resolve_salon(config, args.salon)
    data = request(
        "GET",
        "calendar/appointments",
        params={
            "establishment_id": salon.id,
            "email": config["email"],
            "token": salon.token or "",
        },
    )
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return
    items = data.get("appointments", data) if isinstance(data, dict) else data
    if not items:
        if not salon.token:
            # The API answers 200 [] to any request without a token, even one with no
            # email at all, so an empty list here says nothing either way.
            print(
                f"Nothing returned for {salon.label}, but no customer token is set for "
                f"this salon. Without one the API returns an empty list for every "
                f"request, so this means unknown, not none."
            )
        else:
            print(f"No appointments found for this email at {salon.label}.")
        return
    for item in items:
        names, persons = [], []
        for activity in item.get("appointment_activities") or []:
            names.append(activity.get("name"))
            for person in activity.get("persons") or []:
                persons.append(
                    " ".join(filter(None, [person.get("first_name"), person.get("last_name")]))
                )
        names = [n for n in names if n] or list(item.get("activities") or [])
        when = item.get("date")
        stamp = (
            datetime.strptime(when, "%Y-%m-%d %H:%M:%S").strftime("%a %d %b %Y  %H:%M")
            if when
            else "date unknown"
        )
        who = ", ".join(sorted({p for p in persons if p})) or "no preference"
        print(f"  {stamp}   {', '.join(names) or 'unknown service'}   {who}")
        print(f"    id={item.get('id')}  uuid={item.get('uuid')}   (pass both to cancel)")


def cmd_cancel(args):
    config = load_config()
    salon = resolve_salon(config, args.salon)
    print(f"Cancel appointment id={args.id} uuid={args.uuid} at {salon}")
    if not args.confirm:
        print("DRY RUN. Nothing was sent. Re-run with --confirm to actually cancel.")
        return
    result = request(
        "DELETE",
        f"calendar/appointments/{args.id}/{args.uuid}",
        body={"establishment_id": salon.id},
    )
    print("Cancelled.", json.dumps(result, ensure_ascii=False) if result else "")


def cmd_watch(args):
    config = load_config()
    salon = resolve_salon(config, args.salon)
    rules = salon.watch
    if not rules:
        raise OptiosError(
            f"No watch rules for salon {salon.key!r}. Add a \"watch\" array under "
            f"salons.{salon.key} in {CONFIG_PATH}."
        )

    menu = load_menu(salon)
    seen = {}
    if os.path.exists(salon.seen_path):
        with open(salon.seen_path) as handle:
            seen = json.load(handle)

    announcements = []
    for rule in rules:
        if rule.get("enabled") is False:
            continue
        name = rule.get("name") or rule["service"]
        path, leaf = resolve_service(menu, rule["service"])
        # A rule with no "person" key inherits the salon default; an explicit null means any.
        person_id = resolve_person(salon, rule.get("person", salon.default_person))
        start = date.today()
        end = start + timedelta(days=int(rule.get("within_days", 21)))
        slots = find_slots(salon, leaf, person_id, start, end)
        slots = filter_slots(
            slots, rule.get("after"), rule.get("before"), parse_weekdays(rule.get("weekday"))
        )
        current = {s["datetime"] for s in slots}
        previous = set(seen.get(name, []))
        fresh = sorted(current - previous)
        seen[name] = sorted(current)
        if fresh and not args.prime:
            announcements.append((name, path, fresh))

    os.makedirs(salon.cache_dir, exist_ok=True)
    with open(salon.seen_path, "w") as handle:
        json.dump(seen, handle, indent=2)

    if args.prime:
        print(f"Primed baseline for {len(rules)} rule(s) at {salon.label}.")
        return

    if not announcements:
        if not args.quiet:
            print(f"No new slots at {salon.label}.")
        return

    for name, path, fresh in announcements:
        pretty = [
            datetime.strptime(f, "%Y-%m-%d %H:%M").strftime("%a %d %b %H:%M") for f in fresh[:4]
        ]
        message = ", ".join(pretty) + (f" (+{len(fresh) - 4} more)" if len(fresh) > 4 else "")
        print(f"[{name}] {len(fresh)} new: {message}")
        if args.notify:
            notify(f"{salon.label}: {name}", message)


def notify(title, message):
    script = (
        f"display notification {json.dumps(message)} with title {json.dumps(title)} "
        'sound name "Ping"'
    )
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def cmd_install_watcher(args):
    config = load_config()
    salon = resolve_salon(config, args.salon)
    script = os.path.abspath(__file__)
    os.makedirs(salon.cache_dir, exist_ok=True)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{salon.plist_label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
    <string>watch</string>
    <string>--salon</string>
    <string>{salon.key}</string>
    <string>--notify</string>
    <string>--quiet</string>
  </array>
  <key>StartInterval</key><integer>{args.interval}</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{salon.log_path}</string>
  <key>StandardErrorPath</key><string>{salon.log_path}</string>
</dict>
</plist>
"""
    os.makedirs(os.path.dirname(salon.plist_path), exist_ok=True)
    with open(salon.plist_path, "w") as handle:
        handle.write(plist)
    subprocess.run(["launchctl", "unload", salon.plist_path], check=False, capture_output=True)
    loaded = subprocess.run(
        ["launchctl", "load", salon.plist_path], check=False, capture_output=True
    )
    if loaded.returncode != 0:
        raise OptiosError(f"launchctl load failed: {loaded.stderr.decode().strip()}")
    print(f"Installed {salon.plist_path}, watching {salon.label} every {args.interval}s.")
    print(f"Logs: {salon.log_path}")
    print(f"Remove with: optios.py uninstall-watcher --salon {salon.key}")


def cmd_uninstall_watcher(args):
    config = load_config()
    salon = resolve_salon(config, args.salon)
    subprocess.run(["launchctl", "unload", salon.plist_path], check=False, capture_output=True)
    if os.path.exists(salon.plist_path):
        os.remove(salon.plist_path)
    print(f"Watcher for {salon.key!r} removed.")


# ---------------------------------------------------------------- status


def probe(name, check):
    """Run one health check, capturing its outcome, latency and detail line."""
    started = time.perf_counter()
    try:
        ok, message = True, check()
    except OptiosError as exc:
        ok, message = False, str(exc)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        ok, message = False, f"unexpected payload: {exc}"
    return {
        "check": name,
        "ok": ok,
        "ms": round((time.perf_counter() - started) * 1000),
        "detail": message,
    }


def cmd_status(args):
    """Check every endpoint the CLI depends on, and that its payload still parses."""
    _, salon = select_salon(args)
    state = {}

    def check_menu():
        menu = request("GET", "activity-menu", params={"establishment_id": salon.id})
        if not isinstance(menu, list) or not menu:
            raise OptiosError("expected a non-empty list of root categories")
        services = list(iter_services(menu))
        if not services:
            raise OptiosError("no bookable leaves in the menu tree")
        state["services"] = services
        return f"{len(menu)} root categories, {len(services)} bookable services"

    def check_persons():
        persons = load_persons(salon)
        if not persons:
            raise OptiosError("no stylists returned")
        return f"{len(persons)} stylists"

    def check_last_minutes():
        data = request("GET", "last-minutes", params={"establishment_id": salon.id})
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise OptiosError("expected an object with a 'data' list")
        rows = data["data"]
        free = [r for r in rows if not (r.get("attributes") or {}).get("is_booked")]
        return f"{len(rows)} offers, {len(free)} open"

    def check_finder():
        if not state.get("services"):
            raise OptiosError("skipped, the activity-menu check failed")
        path, leaf = state["services"][0]
        start = date.today()
        end = start + timedelta(days=6)
        data = request(
            "POST",
            f"calendar/free-moment-finder/{start.isoformat()}/{end.isoformat()}",
            body={
                "establishment_id": salon.id,
                "customers": [{"token": None, "blocks": blocks_for(leaf, None)}],
            },
        )
        # find_slots quietly skips a non-dict response, so assert the shape here instead.
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            raise OptiosError("expected an object with a 'days' map")
        moments = sum(
            len(bucket)
            for day in data["days"].values()
            for bucket in (day.get("free_moments") or {}).values()
        )
        return f"{moments} slots in 7 days for {path}"

    results = [
        probe("activity-menu", check_menu),
        probe("persons", check_persons),
        probe("last-minutes", check_last_minutes),
        probe("free-moment-finder", check_finder),
    ]
    failed = [r for r in results if not r["ok"]]

    if args.json:
        report = {"ok": not failed, "api": API, "salon": salon.key, "establishment": salon.id}
        report["checks"] = results
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"{API}\n{salon}\n")
        for result in results:
            mark = "ok  " if result["ok"] else "FAIL"
            print(f"  {mark}  {result['check']:<20}{result['ms']:>6} ms  {result['detail']}")
        print(f"\n{len(results) - len(failed)}/{len(results)} checks passed.")
    if failed:
        sys.exit(1)


# ---------------------------------------------------------------- cli


def build_parser():
    parser = argparse.ArgumentParser(prog="optios.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared by every command that talks to a specific establishment.
    salon_opt = argparse.ArgumentParser(add_help=False)
    salon_opt.add_argument(
        "--salon", help="configured salon key, or a bare establishment id for a one-off"
    )

    p = sub.add_parser("init", help="write your identity to the config file")
    p.add_argument("--first-name")
    p.add_argument("--last-name")
    p.add_argument("--email")
    p.add_argument("--phone")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add-salon", help="add or update a salon in the config")
    p.add_argument("--key", required=True, help="short name you will pass to --salon")
    p.add_argument("--establishment", required=True, help="establishment id, from the booking URL")
    p.add_argument("--label", help="human readable name, shown in output")
    p.add_argument(
        "--default-person", help='preferred stylist for this salon, or "any" to clear it'
    )
    p.add_argument("--default", action="store_true", help="make this the default salon")
    p.set_defaults(func=cmd_add_salon)

    p = sub.add_parser("salons", help="list configured salons")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_salons)

    p = sub.add_parser(
        "status", parents=[salon_opt], help="check the API is up and its payloads still parse"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("services", parents=[salon_opt], help="list bookable services")
    p.add_argument("--search")
    p.add_argument("--refresh", action="store_true", help="bypass the 24h menu cache")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_services)

    p = sub.add_parser("slots", parents=[salon_opt], help="find free slots for a service")
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

    p = sub.add_parser("lastminutes", parents=[salon_opt], help="list last-minute offers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_lastminutes)

    p = sub.add_parser(
        "book", parents=[salon_opt], help="book an appointment (dry run unless --confirm)"
    )
    p.add_argument("--service", required=True)
    p.add_argument("--at", required=True, help="'YYYY-MM-DD HH:MM'")
    p.add_argument("--person")
    p.add_argument("--remark")
    p.add_argument("--no-verify", action="store_true", help="skip the availability pre-check")
    p.add_argument("--confirm", action="store_true", help="actually send the booking")
    p.set_defaults(func=cmd_book)

    p = sub.add_parser(
        "appointments", parents=[salon_opt], help="list appointments for the configured email"
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_appointments)

    p = sub.add_parser(
        "cancel", parents=[salon_opt], help="cancel an appointment (dry run unless --confirm)"
    )
    p.add_argument("--id", required=True)
    p.add_argument("--uuid", required=True)
    p.add_argument("--confirm", action="store_true")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser(
        "watch", parents=[salon_opt], help="report slots that are new since the last run"
    )
    p.add_argument("--notify", action="store_true", help="send a macOS notification")
    p.add_argument("--quiet", action="store_true", help="print nothing when there is no news")
    p.add_argument("--prime", action="store_true", help="record the baseline without reporting")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "install-watcher", parents=[salon_opt], help="install a LaunchAgent for periodic watching"
    )
    p.add_argument("--interval", type=int, default=1800, help="seconds between runs")
    p.set_defaults(func=cmd_install_watcher)

    p = sub.add_parser(
        "uninstall-watcher", parents=[salon_opt], help="remove a salon's LaunchAgent"
    )
    p.set_defaults(func=cmd_uninstall_watcher)

    return parser


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except OptiosError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
