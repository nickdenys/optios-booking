---
name: optios-booking
description: Check availability, book, cancel and watch for appointments at any salon on the Optios booking platform. Activates when the user asks about salon or hairdresser appointments, free slots, booking a haircut, or cancelling an appointment.
---

# Optios appointment booking

Wraps the Optios booking API behind a CLI. All commands go through one script:

```bash
python3 ~/.claude/skills/optios-booking/optios.py <command> [options]
```

Identity and salons live in `~/.config/optios/config.json` (name, email, phone, plus
one entry per salon holding its establishment id, customer token and watch rules).
The script reads it; never inline personal details into a command.

## Rules

1. **Never pass `--confirm` unless the user has approved that exact booking in this
   conversation.** Run the dry run first, show the summary, wait for a yes. A booking
   takes a real slot from a real salon and cannot be cancelled within 24 hours.
2. When the user asks for a time, show them the matching free slots and let them pick.
   Do not guess a slot on their behalf.
3. `--no-verify` disables the availability pre-check. Do not use it.
4. If a command fails, report the error as-is. This is a private, undocumented API and
   a schema change should surface loudly, not be worked around.

## Choosing a salon

Commands act on the default salon unless told otherwise. Pass `--salon <key>` to target
another configured one, or `--salon <establishment id>` for a salon that is not in the
config. Run `optios.py salons` to see what is configured, and which is the default.

If the user names a salon you do not recognise, check `salons` first rather than
guessing an establishment id.

## Commands

```bash
# Configured salons, and which one is the default
optios.py salons

# Is the API up, and do its payloads still parse? Exits non-zero if not.
optios.py status [--salon KEY] [--json]

# What can be booked, with prices and durations
optios.py services [--salon KEY] [--search barbier] [--json]

# Free slots. --service takes a menu path fragment or a numeric menu id.
optios.py slots --service "BARBIER > SNIT > SNIT" [--salon KEY] [--person Robbe] \
                [--since 2026-09-01] [--until 2026-09-30] [--days 21] \
                [--after 16:00] [--before 19:00] [--weekday sat,fri] [--limit 10] [--json]

# Last-minute offers the salon has released
optios.py lastminutes [--salon KEY] [--json]

# Book. Prints a summary and stops unless --confirm is given.
optios.py book --service 2992137 --at "2026-08-18 15:30" [--salon KEY] \
               [--person Robbe] [--remark "..."] [--confirm]

# Existing appointments for the configured email
optios.py appointments [--salon KEY] [--json]

# Cancel. Dry run unless --confirm. Get id and uuid from `appointments`.
optios.py cancel --id 12345 --uuid abc-def [--salon KEY] [--confirm]

# Cancellation watcher, one per salon
optios.py watch [--salon KEY] [--notify] [--quiet] [--prime]
optios.py install-watcher [--salon KEY] [--interval 1800]
optios.py uninstall-watcher [--salon KEY]

# Setup
optios.py init --first-name X --last-name Y --email a@b.c --phone +32...
optios.py add-salon --key mysalon --establishment 3600 --label "My Salon" --default
```

## Service names

Services are a menu tree, so `--service` matches on the path. A trailing fragment is
enough when unique: `"BARBIER > SNIT > SNIT"` resolves, `"snit"` does not and the error
lists the candidates. Numeric menu ids are exact and stable, prefer them once known.

Menu trees differ per salon, so a menu id from one salon is meaningless at another.

Combination services (`SNIT + BAARDTRIM`) are a single menu leaf holding several
activities. The script sends them as one appointment; do not book the parts separately.

## Watch rules

Edit the `watch` array under the salon's entry in `~/.config/optios/config.json`:

```json
{"name": "saturday cut", "service": "HEREN > BARBIER > SNIT > SNIT",
 "person": "Robbe", "within_days": 21, "after": "14:00", "weekday": "sat",
 "enabled": true}
```

`watch` reports only slots that are new since the previous run, using a baseline in
`~/.cache/optios/<salon>/seen.json`. Run `watch --prime` after editing rules so the
first real run does not announce everything at once. The LaunchAgent runs it every 30
minutes and posts a macOS notification; logs go to `~/.cache/optios/<salon>/watch.log`.

Keep the interval at 15 minutes or more. This is someone else's server.

## API notes

Undocumented endpoints under `https://client.optios.net/api/anonymous`, no
authentication. Two things that are easy to get wrong and were confirmed by capturing
the real requests:

- `category_id` in a booking block is the **menu leaf id**, not the activity's own
  `category_id` field.
- `establishment_id` is a **string** in POST bodies, an integer elsewhere. Sending an
  integer to the free-moment finder returns HTTP 500.
