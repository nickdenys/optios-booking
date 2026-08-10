---
name: optios-booking
description: Check availability, book, cancel and watch for appointments at KÖP hairdresser in Wetteren. Activates when the user mentions KÖP, the hairdresser, a haircut, barbier, snit, baardtrim, booking an appointment at the salon, or asks what slots are free.
---

# KÖP appointment booking

Wraps the Optios booking API for KÖP Wetteren (establishment 3600) behind a CLI.
All commands go through one script:

```bash
python3 ~/.claude/skills/optios-booking/koep.py <command> [options]
```

Identity lives in `~/.config/koep/config.json` (name, email, phone, customer token,
watch rules). The script reads it; never inline personal details into a command.

## Rules

1. **Never pass `--confirm` unless the user has approved that exact booking in this
   conversation.** Run the dry run first, show the summary, wait for a yes. A booking
   takes a real slot from a real salon and cannot be cancelled within 24 hours.
2. When the user asks for a time, show them the matching free slots and let them pick.
   Do not choose a slot on their behalf and book it in one step.
3. `--no-verify` disables the availability pre-check. Do not use it.
4. If a command fails, report the error as-is. This is a private, undocumented API and
   a schema change should surface loudly, not be worked around.

## Commands

```bash
# What can be booked, with prices and durations
koep.py services [--search barbier] [--json]

# Free slots. --service takes a menu path fragment or a numeric menu id.
koep.py slots --service "BARBIER > SNIT > SNIT" [--person Robbe] \
              [--since 2026-09-01] [--until 2026-09-30] [--days 21] \
              [--after 16:00] [--before 19:00] [--weekday sat,fri] [--limit 10] [--json]

# Last-minute offers the salon has released
koep.py lastminutes [--json]

# Book. Prints a summary and stops unless --confirm is given.
koep.py book --service 2992137 --at "2026-08-18 15:30" [--person Robbe] [--remark "..."] [--confirm]

# Existing appointments for the configured email
koep.py appointments [--json]

# Cancel. Dry run unless --confirm. Get id and uuid from `appointments`.
koep.py cancel --id 12345 --uuid abc-def [--confirm]

# Cancellation watcher
koep.py watch [--notify] [--quiet] [--prime]
koep.py install-watcher [--interval 1800]
koep.py uninstall-watcher
```

## Service names

Services are a menu tree, so `--service` matches on the path. A trailing fragment is
enough when unique: `"BARBIER > SNIT > SNIT"` resolves, `"snit"` does not and the error
lists the candidates. Numeric menu ids are exact and stable, prefer them once known.

Combination services (`SNIT + BAARDTRIM`) are a single menu leaf holding several
activities. The script sends them as one appointment; do not book the parts separately.

## Watch rules

Edit the `watch` array in `~/.config/koep/config.json`:

```json
{"name": "saturday cut", "service": "HEREN > BARBIER > SNIT > SNIT",
 "person": "Robbe", "within_days": 21, "after": "14:00", "weekday": "sat",
 "enabled": true}
```

`watch` reports only slots that are new since the previous run, using a baseline in
`~/.cache/koep/seen.json`. Run `watch --prime` after editing rules so the first real run
does not announce everything at once. The LaunchAgent runs it every 30 minutes and posts
a macOS notification; logs go to `~/.cache/koep/watch.log`.

Keep the interval at 15 minutes or more. This is someone else's server.

## API notes

Undocumented endpoints under `https://client.optios.net/api/anonymous`, no
authentication. Two things that are easy to get wrong and were confirmed by capturing
the real requests:

- `category_id` in a booking block is the **menu leaf id**, not the activity's own
  `category_id` field.
- `establishment_id` is a **string** in POST bodies, an integer elsewhere. Sending an
  integer to the free-moment finder returns HTTP 500.
