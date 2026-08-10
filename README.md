# optios-booking

A command line tool and Claude Code skill for booking hairdresser appointments at
[KÖP](https://booking.optios.net/3600/menu) in Wetteren, which runs on the Optios
booking platform.

It talks to the same JSON API that the official booking page uses, so it can list
services, find free slots, book, cancel, and watch for cancellations without opening
a browser. Python 3 standard library only, no dependencies.

## Why

The Optios booking page is a React app with a four level menu tree, a stylist picker,
and a calendar. Finding out whether there is a Saturday afternoon slot in the next
three weeks takes a lot of clicking. This does it in one command.

```
$ optios slots --service 2992137 --days 40 --weekday sat --after 14:00

HEREN > BARBIER > SNIT > SNIT  (EUR 45, 30 min)
2026-08-10 to 2026-09-19

  Sat 12 Sep 2026  15:30   Megan Vanneste
  Sat 19 Sep 2026  14:00   Robbe
  Sat 19 Sep 2026  14:15   Megan Vanneste
```

## Install

```bash
git clone https://github.com/nickdenys/optios-booking.git ~/Code/optios-booking
python3 ~/Code/optios-booking/koep.py init \
  --first-name Jane --last-name Doe --email jane@example.com --phone "+32..."
```

To use it as a Claude Code skill, symlink it into your skills directory:

```bash
ln -s ~/Code/optios-booking ~/.claude/skills/optios-booking
```

Claude then picks it up automatically when you ask about the hairdresser.

## Configuration

Identity and watch rules live in `~/.config/koep/config.json`, outside the repository,
so personal details are never committed. See `config.example.json` for the shape.

```json
{
  "first_name": "Jane",
  "last_name": "Doe",
  "email": "jane@example.com",
  "phone": "+32XXXXXXXXX",
  "token": null,
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
```

`token` is filled in automatically after your first successful booking. It is the
customer token the API needs in order to list and cancel your appointments.

## Commands

| Command | What it does |
| --- | --- |
| `services [--search TERM]` | List bookable services with prices and durations |
| `slots --service X` | Find free slots, with stylist, weekday and time filters |
| `lastminutes` | Show last minute offers the salon has released |
| `book --service X --at "..."` | Book an appointment (dry run unless `--confirm`) |
| `appointments` | List your existing appointments |
| `cancel --id X --uuid Y` | Cancel an appointment (dry run unless `--confirm`) |
| `watch` | Report slots that are new since the previous run |
| `install-watcher` | Install a LaunchAgent that runs `watch` periodically |

Run `koep.py <command> --help` for the full option list.

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

`watch` compares current availability against a baseline in `~/.cache/koep/seen.json`
and reports only what is new, which makes it useful on a timer.

```bash
python3 koep.py watch --prime          # record the baseline, report nothing
python3 koep.py install-watcher        # LaunchAgent, every 30 minutes
```

The agent posts a macOS notification when a matching slot opens up. Logs go to
`~/.cache/koep/watch.log`. Remove it with `uninstall-watcher`.

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

## Scope

Hardcoded to establishment 3600 (KÖP Wetteren). Pointing it at another Optios salon
means changing `ESTABLISHMENT_ID` and `ESTABLISHMENT_UUID` at the top of `koep.py`.

Not affiliated with Optios or KÖP.

## License

MIT
