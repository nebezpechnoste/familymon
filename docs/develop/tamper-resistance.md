# Tamper resistance

The monitored child uses the laptop every day and has all day to poke at it. This page records what familymon does so the child **can't switch monitoring off, change its rules, or edit its history**, what is left that they *can* do, and how those leftovers are made visible instead of silent. Read it before changing anything about where code runs or where state lives.

Assumptions: the child has a standard (non-admin) account with no sudo, the firmware and boot loader are password-protected, and the disk is encrypted or the machine is physically supervised. Anything that needs root or physical access is out of scope.

## The rule

**Everything that decides, records or enforces runs as root, and keeps its state where only root can write.** The child's account runs nothing of familymon between capture ticks.

The child can kill any process that runs under their own uid, and can stop or mask any *user* systemd unit, including global ones in `/etc/systemd/user` (a link in `~/.config/systemd/user` takes precedence). They can't stop, mask or edit system units. That one fact decides the design.

| Part | Runs as | Unit | State |
|---|---|---|---|
| Capture loop | root | `familymon-capture.service` | `/var/lib/familymon/captures.db`, `screenshots/` |
| Capture probe (one per tick) | the child, about 1–3 s | started by the loop through `setpriv` | none (stdout only) |
| Heartbeat | root | `familymon-heartbeat.timer` | events in the DB |
| Classify | root | `familymon-classify.timer` | DB |
| Schedule notifications and lesson warnings | root, delivered into the session | `familymon-notify.timer` | `notified.json`, `warned.json` |
| Enforcement, chargeback, web block | root | `familymon-{enforce,chargeback,webblock}.timer` | `policy.yaml`, `grants.json`, `playtime_charges.json`, `known_games.json` |
| Dashboard | `familymon` system user, read-only, sandboxed | `familymon-dashboard.service` | reads the DB and the schedule through group `familymon` |

`/var/lib/familymon` is `root:familymon 0750`. The DB, its journal and `schedule.yaml` are group-readable for the dashboard (`familymon/storage/perms.py`). Everything else there, the screenshots included, is root-only: the dashboard shows whether a screenshot exists, never the image. The child can't even list the directory.

Before this design, capture, heartbeat, classify, notify and the dashboard were the child's user units, and their state lived in `~kid/.local/state/familymon`. The child could stop capture, delete their history, edit `schedule.yaml` to switch lesson blocking off, recreate the DB so chargeback never billed again, and read the dashboard password. `familymonctl migrate-state`, which the package runs on upgrade, moves that state into `/var/lib/familymon` (see `familymon/storage/migrate.py`).

## Why capture still needs the child's uid

KWin serves only its own session's clients:

- **Screenshots** go through KWin's `ScreenShot2` interface. KWin authorizes it by the calling *executable* (`/usr/bin/spectacle` has the right desktop-file entry), not by uid. But root can't act as a client of the child's session at all: run as root against the session's bus and Wayland socket, spectacle reports that KWin "does not seem to be available" (tested on Plasma 6).
- **Window enumeration** uses KWin scripting, whose script calls back on the *session* bus.

A dedicated non-root system user is worse than root: it can't even reach the session bus (`/run/user/UID` is `0700`, and the bus authenticates by uid).

So the loop stays in root and borrows the child's uid for a moment each tick (`familymon/capture/supervisor.py`):

```
root: familymon-capture.service (capture-supervise)
  ├─ logind: does the child have a seated graphical session? active? locked?
  │     none → skip the tick, record nothing (heartbeat reports "idle")
  ├─ write this tick's KWin script, with a fresh nonce, to /run/familymon/kwin/<random>.js
  │     (dir root 0711: no listing; file root 0644: KWin can read it, the child can't change it)
  ├─ setpriv --reuid=kid … --no-new-privs -- env -i <session vars> python -I -m familymon.cli capture-probe
  │     stdin: {suppress, script, nonce}   stdout: CaptureResult JSON, already redacted
  └─ root: game detection (/proc), pHash dedup, write DB + screenshot, events
```

The probe:

- **Starts clean.** `setpriv` drops to the child's uid without a PAM session (so there's no auth-log line every minute) and sets `no_new_privs`. The probe runs root-owned code from `/opt/familymon`. The environment is replaced (`env -i`: no `PYTHONPATH`, no `LD_PRELOAD`, nothing from the child's profile), and Python runs isolated (`-I`: no user site-packages, no current directory on the path).
- **Can't be reached through /proc.** It makes itself non-dumpable first thing (`PR_SET_DUMPABLE=0`), so the child's processes can't open its `/proc/<pid>/fd` (the stdout pipe) or attach to it. With Yama `ptrace_scope=1` the child can't attach anyway, because the probe isn't their descendant.
- **Writes its temporary screenshot file into a /tmp the child can't see.** The unit has `PrivateTmp=yes`, and the probe and spectacle inherit that mount namespace.
- **Redacts before anything leaves it.** The messaging-window redaction runs in the probe, exactly as before. If KWin's window list doesn't arrive, there's no way to know whether a chat is on screen, so no screenshot is taken at all (`window_list_unavailable`).

### The window-list sink

The KWin script answers by calling `org.familymon.WindowSink.Push` on the session bus, and anything in the session can call that too. The sink (`kwin_wayland._WindowSink`) accepts a push only if:

1. **It comes from KWin's own bus connection.** That's KWin's unique name from `GetNameOwner("org.kde.KWin")`. Unique names are assigned by the bus and can't be claimed.
2. **It carries this tick's nonce.** A script the child loads into KWin themselves would pass check 1, because KWin scripting has no authorization. The nonce defends against that.

The first valid push wins. Everything else, whether from the wrong sender, with the wrong nonce, a duplicate or junk, gets the same empty reply (a forger learns nothing) and is counted. The root loop records the count as a `capture_tamper` event. This was tested against a live Plasma session: 54 forged pushes sent during one probe were all rejected, and the genuine list was still accepted.

The KWin script is unloaded after every tick. KWin keeps every loaded instance, so loading without unloading would leak one script per minute into the compositor (earlier releases did this).

## Detection: what the child can still do, and how it shows

Some interference can't be prevented from inside the child's own session, only made visible:

| The child… | Effect | How it shows |
|---|---|---|
| kills each probe, restarts KWin, or squats the `org.familymon.WindowSink` name | that tick fails | `capture_failed` event `{error, session}`; heartbeat **dead while the session is active**; red line on the dashboard |
| feeds forged window lists | rejected | `capture_tamper` event `{sink_rejected}`; red line on the dashboard |
| logs out or locks the screen | nothing to capture (locked: metadata only, no screenshot) | heartbeat `idle` / session `locked`; timekpr still counts time |
| switches off familymon notifications in Plasma | lesson-start notices and warnings not shown | not detected. Notifications are advisory; blocking is enforced by Chrome policy and timekpr |
| logs in on a text console | no capture (no browser or games there) | whether it counts as screen time depends on timekpr-next's session-type settings; check them |

The **heartbeat** (`familymon-heartbeat.timer`, root, every minute) asks logind about the child's session. "No captures" while the child is logged in is the alarm. "No captures" while nobody is logged in is `idle`, not a fault. The dashboard shows the watcher's latest verdict, and flags a watcher that has stopped reporting (`watcher_down`).

Escalating automatically (for example locking the session after N failed ticks) would be possible, but it's a policy decision. It isn't built, and the parent decides whether to have it.

## The other places the child could reach

- **Rules and schedule.** `policy.yaml`, `schedule.yaml`, `grants.json` and the chargeback watermark are in `/var/lib/familymon`. Every schedule command runs as root (`sudo familymonctl schedule …`).
- **Game masks.** PlayTime masks come from the child's Steam library (in their home). Every game directory ever seen installed is remembered in `known_games.json`, so deleting an `appmanifest` doesn't take a game off the budget. Only the parent removes one (`familymonctl enforce games --forget DIR`).
- **Classification.** Application categories come only from system `.desktop` directories. The child's `~/.local/share/applications` is ignored, because it's a forgeable category source (`categorize/apps.py`).
- **Chargeback.** If the DB is ever replaced (crash recovery, migration), the watermark resets for the day instead of hiding every new capture (`chargeback.rewound_watermark`).
- **Personal time.** The window comes from `policy.yaml` and `grants.json`, both root-only, and the root supervisor evaluates it every tick, so the child can't widen it. It eases what the parent sees (no screenshots, no titles on the dashboard), never what is classified, enforced or billed.
- **Dashboard.** It runs as `familymon` with `ProtectSystem=strict`, opens the DB read-only, and gets its credentials from `/etc/familymon/dashboard.env` (`root:root 0600`), which systemd reads before dropping privileges. Rotate the password after upgrading from a release where the file was readable by the child.
- **System logs.** Familymon logs to the journal. Membership of `adm` lets an account read all system logs. Removing the child from `adm` (`sudo gpasswd -d kid adm`) is recommended, and it's the parent's decision.
- **Desktop.** KDE Kiosk (`[$i]` immutable settings) can lock down parts of Plasma's UI. That's cosmetic hardening, not a security boundary, and familymon doesn't need it.

## Changing the code

- New periodic work belongs in a **system** unit running as root. Never add a user unit or anything that stays running in the child's session.
- New state goes in `state_dir()`, secured with `storage/perms.secure()`. Mark it `shared=True` only if the dashboard must read it.
- Anything read from the child's home or session is untrusted input: parse defensively, never follow symlinks as root, and prefer remembering facts over re-deriving them from files the child can delete.
- If a tick can fail because of the child, record an event. Silence must never look like "nothing happened".
