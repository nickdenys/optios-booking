# optios-booking

A command line tool and Claude Code skill for booking salon appointments on the
[Optios](https://booking.optios.net) booking platform.

It talks to the same JSON API that the official booking pages use, so it can list
services, find free slots, book, cancel, and watch for cancellations without opening
a browser. Python 3 standard library only, no dependencies.

Any salon on the platform works. You point the tool at one by its establishment id,
which is the number in its booking URL (`booking.optios.net/3600/menu` is 3600).

## Why

An Optios booking page is a React app with a four level menu tree, a stylist picker,
and a calendar. Finding out whether there is a Saturday afternoon slot in the next
three weeks takes a lot of clicking. This does it in one command.

```
$ optios slots --service 2992137 --days 40 --weekday sat --after 14:00

My Salon (establishment 3600)
HEREN > BARBIER > SNIT > SNIT  (EUR 45, 30 min)
2026-08-11 to 2026-09-20

  Sat 12 Sep 2026  15:30   Megan Vanneste
  Sat 19 Sep 2026  14:00   Robbe
  Sat 19 Sep 2026  14:15   Megan Vanneste
```

## Install

```bash
git clone https://github.com/nickdenys/optios-booking.git ~/Code/optios-booking

# who you are, shared across every salon
python3 ~/Code/optios-booking/optios.py init \
  --first-name Jane --last-name Doe --email jane@example.com --phone "+32..."

# where you book, one entry per salon
python3 ~/Code/optios-booking/optios.py add-salon \
  --key mysalon --establishment 3600 --label "My Salon, Sometown" --default
```

To use it as a Claude Code skill, symlink it into your skills directory:

```bash
ln -s ~/Code/optios-booking ~/.claude/skills/optios-booking
```

## Configuration

Identity, salons and watch rules live in `~/.config/optios/config.json`, outside the
repository, so personal details are never committed. See `config.example.json`.

```json
{
  "first_name": "Jane",
  "last_name": "Doe",
  "email": "jane@example.com",
  "phone": "+32XXXXXXXXX",
  "default_salon": "mysalon",
  "salons": {
    "mysalon": {
      "id": "3600",
      "label": "My Salon, Sometown",
      "token": null,
      "default_person": "Robbe",
      "watch": [
        {
          "name": "saturday cut",
          "service": "HEREN > BARBIER > SNIT > SNIT",
          "person": "Robbe",
          "within_days": 21,
          "after": "14:00",
          "weekday": "sat",
          "enabled": true
        }
      ]
    }
  }
}
```

Identity is global. Everything tied to a specific salon (its establishment id, your
customer token, and your watch rules) lives under that salon's entry, because a token
issued by one salon means nothing at another.

`token` is filled in automatically after your first successful booking there. It is
the customer token the API needs in order to list and cancel your appointments.

### Preferred stylist

`default_person` is the stylist `slots` and `book` use when you do not pass
`--person`. Set it with `add-salon --key mysalon --establishment 3600
--default-person Robbe`, and clear it again with `--default-person any`.

An explicit `--person` always wins, and `--person any` searches every stylist for that
one command. Watch rules inherit the default only when the rule has no `person` key;
a rule with `"person": null` means any stylist, and stays that way.

### Choosing a salon

Every command that talks to a salon accepts `--salon`. Resolution order:

1. `--salon <key>` on the command
2. `OPTIOS_SALON` in the environment
3. `default_salon` in the config
4. the only configured salon, if there is exactly one

`--salon` also accepts a bare establishment id, which skips the config entirely and is
handy for looking at a salon you have not set up:

```bash
optios slots --salon 4210 --service "snit"   # one-off, nothing written to config
optios salons                                # list what is configured
```

Each salon keeps its own menu cache, watch baseline and LaunchAgent, under
`~/.cache/optios/<key>/`.

## Commands

| Command | What it does |
| --- | --- |
| `init` | Write your identity to the config file |
| `add-salon --key K --establishment ID` | Add or update a salon |
| `salons` | List configured salons and which is default |
| `status` | Check the API is up and its payloads still parse |
| `services [--search TERM]` | List bookable services with prices and durations |
| `slots --service X` | Find free slots, with stylist, weekday and time filters |
| `lastminutes` | Show last minute offers the salon has released |
| `book --service X --at "..."` | Book an appointment (dry run unless `--confirm`) |
| `appointments` | List your existing appointments |
| `cancel --id X --uuid Y` | Cancel an appointment (dry run unless `--confirm`) |
| `watch` | Report slots that are new since the previous run |
| `install-watcher` | Install a LaunchAgent that runs `watch` periodically |

Run `optios.py <command> --help` for the full option list.

### Booking safety

`book` and `cancel` print a summary and do nothing unless you pass `--confirm`.
Before booking, the tool re-queries availability and refuses if the requested slot is
not actually on offer:

```
$ optios book --service 2992137 --at "2026-08-18 12:00"
error: 2026-08-18 12:00 is not offered for this service.
       Free that day: 15:30, 16:30, 17:00
```

### Cancellation watcher

`watch` compares current availability against a baseline in
`~/.cache/optios/<salon>/seen.json` and reports only what is new, which makes it
useful on a timer.

```bash
python3 optios.py watch --prime               # record the baseline, report nothing
python3 optios.py install-watcher             # LaunchAgent, every 30 minutes
```

The agent posts a macOS notification when a matching slot opens up. Watchers are per
salon (label `net.optios.watch.<key>`), so you can run one for each. Remove one with
`uninstall-watcher --salon <key>`.

Please keep the interval at 15 minutes or more. This is someone else's server.

## API notes

The endpoints live under `https://client.optios.net/api/anonymous` and require no
authentication. They are undocumented, so they can change without warning. The tool
raises on unexpected shapes rather than guessing.

Two details that are easy to get wrong, both confirmed by capturing the requests the
official booking page actually sends:

* `category_id` inside a booking block is the **menu leaf id**, not the `category_id`
  field that appears on the activity itself.
* `establishment_id` is a **string** in POST bodies and an integer elsewhere. Sending
  an integer to the free moment finder returns HTTP 500.

Relevant endpoints:

| Purpose | Endpoint |
| --- | --- |
| Settings | `GET /online-booking-info` |
| Menu tree | `GET /activity-menu` |
| Staff | `GET /persons` |
| Last minutes | `GET /last-minutes` |
| Free slots | `POST /calendar/free-moment-finder/{from}/{to}` |
| Book | `POST /appointments` |
| List mine | `GET /calendar/appointments` |
| Reschedule | `POST /appointments/{id}/move` |
| Cancel | `DELETE /calendar/appointments/{id}/{uuid}` |

### Checking the API

Because the endpoints are undocumented, `status` probes the four read-only ones and
verifies the payloads still have the shape the code parses. It exits non-zero if any
check fails, so it works in a cron job or a pre-flight check.

```
$ optios status

https://client.optios.net/api/anonymous
My Salon (establishment 3600)

  ok    activity-menu          637 ms  2 root categories, 50 bookable services
  ok    persons                537 ms  9 stylists
  ok    last-minutes           487 ms  11 offers, 11 open
  ok    free-moment-finder     693 ms  1 slots in 7 days for DAMES > BALAYAGE ...

4/4 checks passed.
```

A green run means more than "the server answered". A 200 carrying a renamed field
still fails the check that depends on it. The free moment finder is verified directly
rather than through `find_slots`, which skips a malformed response quietly and would
otherwise report a shape change as zero slots.

`status` never writes, and it bypasses the menu cache so it always hits the network.
Booking and cancellation are not probed, for obvious reasons.

## Scope

Read and write access to appointments for one customer identity across any number of
Optios salons. Not affiliated with Optios.

## License

MIT
