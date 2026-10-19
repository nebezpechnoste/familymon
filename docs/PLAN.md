# familymon — Plan (v2)

> This is the original design plan, kept for its reasoning. Parts of it were
> superseded during implementation (see the notes inline). The README
> describes what is actually built.

## Context

The target is a Linux laptop used by a teenager. Instead of the conventional "block specific apps" parental-control approach (mainstream tools such as Qustodio don't support Linux anyway), familymon is a **monitor-and-react** system: capture screenshots + window list every ~60 s, classify the activity locally so private content never leaves the device, cross-reference with the school schedule to surface conflicts, and (in a later phase) drive blocking rules from that intelligence.

**Two-machine development model.** familymon is built and dogfooded on the parent's own dev machine during normal work, validating the capture/heuristic/labeling pipeline against the parent's usage. When the app is mature, the teen's laptop is upgraded and cleaned, familymon is installed there as the production target, and the dev machine goes back to being a plain dev machine.

The dogfood phase is a big de-risk: the parent is the first labeler, the first dataset, and the first canary. The model isn't trained on the kid's data; it's tuned on the parent's, then recalibrated after a couple of weeks of real kid usage.

**Hard constraints:**
- Free in the long run. A bootstrap phase uses an existing **Claude Code subscription** to label batches of captures — **no direct Anthropic API billing**, which is billed separately from the subscription.
- Analysis must not consume resources during "allowed play" time on the kid's laptop. The same gating applies on the dev machine during games / focused work — familymon must not steal CPU.
- Screenshots stay on the laptop they were captured on. Anything shipped to a model for labeling goes via Claude Code, driven interactively by the parent.
- MVP is monitoring + reports only. Blocking is later; the design accommodates it.

---

## Machines

**Dev / dogfood machine — primary host through phase 3.** A recent AMD laptop (12 cores, plenty of RAM, Radeon iGPU — no NVIDIA, no CUDA), Ubuntu 26.04, **Plasma 6 / Wayland**, Python 3.14. Ollama runs on AMD via ROCm, but for VLM inference the CPU alone is more than sufficient. Tooling at phase 0: `spectacle`, `tesseract`, `uv`, `ollama`, and the Python deps via `uv`.

**Production / target machine (the kid's laptop) — handover at phase 4.** An older 8-core laptop with an NVIDIA GTX 1660 Ti (ollama via CUDA is an option), LUKS-encrypted disk, no swap (no leak path for screenshot bytes). Upgraded from Ubuntu 24.04 / Plasma 5 / X11 to 26.04 / Plasma 6 / Wayland at phase 4.

Hardware on both ends supports **MiniCPM-V 4.6 Q4_K_M on CPU** with massive headroom. The dev machine is overkill — keep that in mind when shaping the resource-gating story; what feels fine there can still be too much for the production target.

**KWin D-Bus paths to use (Plasma 6 Wayland), verified intent:**
- Screenshots: `org.kde.KWin` service, path `/org/kde/KWin/ScreenShot2`, interface `org.kde.KWin.ScreenShot2` (methods `CaptureScreen`, `CaptureWindow`, `CaptureActiveScreen`, `CaptureActiveWindow`). First call prompts user consent in Plasma 6; subsequent calls within the session are silent.
- Scripting: `org.kde.KWin` service, path `/Scripting`, interface `org.kde.kwin.Scripting` (`loadScript()` + `start()`) — runs a JS snippet that can return `workspace.windowList()` for window enumeration.
- Fallback (portal): `org.freedesktop.portal.Desktop` is up; ScreenCast via `xdg-desktop-portal-kde` is wired in. Confirmed both `impl.portal.desktop.kde` and the user-side `portal.Desktop` are running.

---

## Base setup (applied identically to whichever machine is "live target" at the time)

| | Choice / detail |
|---|---|
| Session | Wayland default on Plasma 6. Keep X11 session selectable in SDDM during dev as escape hatch. Lock SDDM session selector to Wayland for the kid's user via `/etc/sddm.conf.d/familymon.conf` at phase 4. |
| Users (phase 4) | `parent` (sudo) + the kid (regular). All familymon daemons run as a `familymon` system user with scoped privileges. During phases 0–3 the dev machine has only the parent, running as themselves for fast iteration. |
| Boot (phase 4) | GRUB password, BIOS lockdown, secure boot. |
| Swap | Stays off — RAM is plenty; no leak path for screenshot bytes. |
| DNS | Local **Unbound** resolver, `/etc/resolv.conf` immutable (`chattr +i`), query log enabled. Set up on dev machine too so the URL-capture path is real-data tested. |
| Browser policies | `/etc/firefox/policies/policies.json` and `/etc/opt/chrome/policies/managed/familymon.json`. Disable DoH (`DNSOverHTTPS.Enabled=false`, `DnsOverHttpsMode: "off"`), disable ECH (`DisableEncryptedClientHello: true`, `EncryptedClientHelloEnabled: false`), force-install the URL-reporter extension. Without this, ECH renders network-layer URL capture impossible. |
| Tray indicator | Always-visible "monitoring active" widget. Even during dogfood — useful for noticing when capture has crashed. At phase 4, sit down with the kid and walk them through what's captured and what isn't; disclosure is more ethical *and* more effective. |

---

## Pre-LLM filtering pipeline (the crux of the cost story)

The single most important architectural lever is **avoiding LLM calls** through cheap local pre-processing. Order, with the fall-through condition for each step:

1. **Same active window as last tick?** (same `WM_CLASS` + same `_NET_WM_NAME`) → reuse previous classification verbatim. The kid reading one article generates ~20 identical captures; we classify once.
2. **Perceptual hash dedup.** Compute pHash (or dHash via `imagehash`) of the screenshot; compare with previous N pHashes in a small ring buffer. Hamming distance < 4 → "no meaningful visual change" → reuse classification.
3. **Heuristic match on window class.** Look up the `WM_CLASS` against a pre-built table from `.desktop` Categories + AppStream metadata + Steam `appmanifest_*.acf` + Lutris `games.db`. Hit with high confidence → classify and done. Most native apps resolve here.
4. **URL extraction for browser windows.** Two sources, used together: (a) Unbound query log correlated by timestamp; (b) AT-SPI URL-bar read on the focused browser window. Look up the resulting domain in the merged Toulouse + v2fly domain-category table. Hit → classify.
5. **Local OCR (Tesseract).** Run on the foreground window crop only. Yields the text content; feed it into a simple TF-IDF / keyword classifier seeded from earlier-labeled examples. Cheap, fast (<200 ms), fully local.
6. **Local VLM** (last resort). Only invoked for captures unresolved above. Even then, the captured screenshot is queued — the VLM runs lazily, throttled, off the critical path.

**Expected resolution rates (rough):** steps 1–2 absorb most of the volume (probably 60–80 %); steps 3–5 absorb most of the rest; the VLM sees maybe 5–15 % of raw captures. At 1440 captures/day, that's a working set of ~100–200 VLM calls/day — easily absorbed by overnight CPU inference, or by a weekly batched Claude Code labeling session during bootstrap.

---

## LLM strategy — bootstrap (cheap) → local (free)

**Phase A: bootstrap labeling via Claude Code, on an existing subscription.**

The kid's laptop accumulates a queue of "needs labeling" captures (those that fell through to step 6 above) along with their metadata. Screenshots are retained on disk for up to 14 days during this phase.

Periodically — say weekly — the parent runs a Claude Code session that:
1. Rsyncs a batch directory (screenshots + a `manifest.jsonl` of metadata) from the kid's laptop to the parent's labeling workstation.
2. Walks Claude Code through the batch with a fixed system prompt and the Pydantic output schema (`schema.py`).
3. Emits `labels.jsonl` with the structured `Classification` for each capture.
4. Rsyncs labels back; `bootstrap_import.py` ingests them into the kid's SQLite.

This uses Claude Code's vision capability, **which is covered by the Claude subscription**, not by per-call API billing. The labeling workstation can be: the kid's laptop itself before handover; any other machine the parent owns with Claude Code installed; or the kid's laptop accessed over SSH after handover (the kid never has a Claude Code login).

**Important Claude policy note.** Claude API/Code outputs may not be used to **fine-tune** other models. We use the labels as: (a) ground truth for evaluating the local pipeline, (b) few-shot examples in the local VLM prompt, (c) seeds for the TF-IDF/keyword classifier in step 5. We do **not** train a model on them.

**Phase B: local VLM takes over.**

Install **Ollama** + **MiniCPM-V 4.6 Q4_K_M** (~2 GB resident, 3–8 s per image on CPU, no GPU competition with games). The local pipeline replaces the bootstrap workflow for new captures. Bootstrap labels remain as the eval set: any time we change a prompt or model, we re-run the pipeline against the labeled set and check category-level F1 didn't regress.

Optional later: when the GPU is otherwise idle and there's no game running, allow the analyze worker to schedule batches through the GTX 1660 Ti for speed. Gated on the trusted-game-detection signal (see the Architecture note on the reverted GameMode approach).

---

## Retention & privacy — revised for the longer bootstrap window

| Phase | Screenshot retention | Why |
|---|---|---|
| Bootstrap | **14 days** | A batch worth labeling in one Claude Code session. |
| Steady-state | **24 hours** | Local pipeline keeps up; longer retention has no upside. |

**Messaging-app blocklist on capture.** This is non-negotiable given the extended retention. The capture daemon maintains a list of window classes for messaging apps (Discord, Telegram Desktop, Signal Desktop, WhatsApp Web in browser, Element, Slack used personally, etc.). When the foreground window matches, **the capture stores window metadata only — no screenshot bytes**. The list is editable by both parent and kid; treat it as a privacy contract the kid can extend.

**Leak paths to close explicitly (in README):**
- `/var/lib/familymon/screenshots/` — mode 0700, owned by `familymon:familymon`, auto-purged.
- SQLite WAL/journal — `VACUUM` nightly; never store image blobs in DB, paths only.
- Swap — stays off (RAM is plenty).
- `systemd-journal` — log capture IDs, never filenames or window titles of messaging apps.
- Backups — exclude `screenshots/` directory from any backup.
- Claude Code session — when screenshots are rsynced to the labeling machine for a batch session, those bytes go to Anthropic via Claude Code. Same Anthropic data handling as any Claude Code use. **Disclose this to the kid.**
- Dashboard — LAN-only, basic auth. For remote access prefer Tailscale over public exposure.

---

## Architecture

> **Update (post-implementation):** the GameMode D-Bus gating shown below and
> in the component list / repo layout was **reverted**. `gamemoderun` is a
> user-forgeable opt-in, so a monitored teen could disable analysis by
> launching anything under it — a trust-resistant-signals violation. It was
> replaced by **trusted game detection** from the installed Steam library +
> a curated list (`familymon/capture/games.py`), which the teen can't trivially
> forge. Resource-gating the analyze/VLM worker during play (pausing analysis,
> not capture) remains a deferred phase-3 item, keyed off that `game_running`
> signal rather than GameMode. Read the diagram's "GameMode D-Bus listener"
> box and the `gamemode.py`/SIGSTOP references accordingly.

```
                            ┌─────────────────────┐
                            │  schedule + policy  │  YAML in /etc/familymon
                            └──────────┬──────────┘
                                       ▼
┌────────────────────┐  enqueue  ┌──────────────────┐   suspend/resume   ┌────────────────────┐
│ familymon-capture  │──────────▶│  capture queue   │◀───────────────────│ GameMode D-Bus     │
│ (user session,     │           │ (SQLite + files) │                    │ listener           │
│  Wayland-aware)    │           └────────┬─────────┘                    └────────────────────┘
└────────────────────┘                    │
        60 s loop                          │ drain only when:
                                           │  – no game registered
                                           │  – not in "allowed play" window
                                           ▼
                                  ┌──────────────────┐
                                  │ familymon-       │   pre-filter → OCR → heuristic →
                                  │   analyze        │   (last resort) Ollama / queued for
                                  │ (root system     │   Claude Code batch
                                  │  service)        │
                                  └────────┬─────────┘
                                           ▼
                                  ┌──────────────────┐
                                  │ familymon-       │   FastAPI, LAN-only, basic auth
                                  │ dashboard        │
                                  └──────────────────┘
```

**Component placement.**
- `familymon-capture` runs as a **systemd user service in the kid's session.** Required: on Wayland, the screenshot APIs (`org.kde.KWin.ScreenShot2`, `xdg-desktop-portal-kde` ScreenCast) need user-session D-Bus access. A separate **root-level heartbeat watcher** notices if capture stops and surfaces it on the dashboard (this is the realistic-friction tamper signal — not prevention, but detection).
- `familymon-analyze` runs as a **root system service** under the `familymon` user, accessing the queue read-only. Wakes on signal, drains, sleeps.
- `familymon-dashboard` runs as a root system service, bound to the LAN IP.
- ~~`gamemode-listener` (small Python helper inside `familymon-analyze`) subscribes to `com.feralinteractive.GameMode` on the user bus and sends `SIGSTOP`/`SIGCONT` to the analyze process on `GameRegistered`/`GameUnregistered`.~~ **Reverted** (see the Architecture note): trusted game detection (`capture/games.py`, installed Steam library) supplies the `game_running` signal instead; using it to pause the analyze worker is a deferred phase-3 task.

**Capture abstraction (the Wayland bit):**
```python
# familymon/capture/backend.py
class CaptureBackend(Protocol):
    def screenshot(self) -> bytes: ...
    def active_window(self) -> WindowInfo: ...
    def window_list(self) -> list[WindowInfo]: ...
```
- `X11Backend`: `mss` for screenshots, `python-xlib` for windows. Used when `XDG_SESSION_TYPE=x11`.
- `KWinWaylandBackend`: `org.kde.KWin.ScreenShot2` D-Bus for screenshots (one-time consent grant by the parent during setup, persistent token in user keyring); `org.kde.KWin.Scripting` runs a one-line script returning `workspace.windowList()` for window enumeration. Used when `XDG_SESSION_TYPE=wayland` on Plasma.
- Backend selected at startup. Both backends share the same `WindowInfo` shape so the rest of the pipeline doesn't care.

---

## Repository layout

```
familymon/
├── familymon/
│   ├── capture/
│   │   ├── loop.py              # 60 s tick, dedup ring buffer
│   │   ├── backend.py           # CaptureBackend protocol
│   │   ├── x11.py
│   │   ├── kwin_wayland.py
│   │   ├── messaging_skip.py    # window-class blocklist for messaging apps
│   │   └── atspi.py             # URL-bar fallback reader
│   ├── analyze/
│   │   ├── worker.py            # queue drain, throttled, niced
│   │   ├── dedup.py             # pHash ring buffer
│   │   ├── heuristics.py        # .desktop/AppStream/Steam/Lutris + domain lookups
│   │   ├── ocr.py               # tesseract wrapper, foreground-crop only
│   │   ├── keyword_classifier.py # TF-IDF seeded from labeled set
│   │   ├── local_vlm.py         # Ollama / MiniCPM-V 4.6 client
│   │   ├── bootstrap_export.py  # build a labeling batch for Claude Code
│   │   ├── bootstrap_import.py  # ingest labels.jsonl back
│   │   │                        # (gamemode.py reverted; trusted game detection
│   │   │                        #  lives in capture/games.py — see Architecture note)
│   │   └── schema.py            # Pydantic — the labeling contract
│   ├── categorize/
│   │   ├── domains.py           # nightly Toulouse + v2fly merge
│   │   ├── apps.py              # build from .desktop + AppStream + Steam + Lutris
│   │   └── categories.py        # canonical enum
│   ├── policy/
│   │   ├── schedule.py          # YAML/iCal schedule lookup
│   │   └── engine.py            # stub for MVP; phase 2 enforcement
│   ├── storage/
│   │   ├── models.py            # SQLAlchemy: Capture, Classification, Event
│   │   ├── purge.py             # phase-aware retention (14d bootstrap / 24h steady)
│   │   └── migrate.py
│   ├── dashboard/
│   │   ├── app.py               # FastAPI
│   │   ├── routes.py            # timeline, conflicts, schedule editor, queue health
│   │   └── templates/           # Jinja2 + HTMX
│   └── cli.py                   # familymonctl: status, batch export/import, replay
├── deploy/
│   ├── policies/{firefox,chrome}/
│   ├── extension/               # the force-installed URL reporter
│   ├── systemd/
│   │   ├── familymon-capture.service   (user)
│   │   ├── familymon-analyze.service
│   │   ├── familymon-dashboard.service
│   │   ├── familymon-heartbeat.service
│   │   └── familymon-purge.timer
│   ├── unbound/familymon.conf
│   ├── sddm/familymon.conf
│   └── install.sh
├── claude_code_label_pack/      # the parent's labeling workspace template
│   ├── CLAUDE.md                # task prompt for the labeling session
│   └── schema.py                # mirrors familymon/analyze/schema.py
└── README.md                    # MUST include Privacy & Threat Model
```

---

## Labeling schema (Pydantic — set in stone before any model code)

```python
class Category(StrEnum):
    SCHOOL = "school"
    HOMEWORK = "homework"
    GAMES = "games"
    SOCIAL = "social"
    MESSAGING = "messaging"
    VIDEO = "video"
    CODE = "code"
    PRODUCTIVITY = "productivity"
    BROWSING_OTHER = "browsing_other"
    IDLE = "idle"
    UNKNOWN = "unknown"

class Classification(BaseModel):
    capture_id: int
    category: Category
    confidence: float
    evidence_text: str
    source: Literal["same_window", "phash", "heuristic", "domain", "ocr_kw",
                    "local_vlm", "claude_code_bootstrap"]
    schema_version: int
```

The `source` field is what makes the cost story auditable. Look at the histogram and you immediately see where calls are going.

---

## Phasing

All of phases 0–3 happen on **the parent's dev machine**, with the parent as the sole user, dogfooding against real work.

### Phase 0 — Scaffold + schema (1–2 days)
- Install runtime prerequisites: `uv` (Python package manager), `ollama` (ROCm-enabled for the 890M, though we'll start CPU-only), and either `kdotool` or roll a thin D-Bus helper for window operations.
  - **Install-method principle:** for each package, research the current recommended install path on Ubuntu 26.04 first (apt repo? official upstream installer? snap? cargo? pipx?). When more than one credible option exists, surface the choice for explicit decision instead of picking silently. This applies to every dependency added later, too — not just phase-0 ones.
- Repo scaffolding: `pyproject.toml`, package skeleton, pre-commit, pytest.
- Write `familymon/storage/models.py` and `familymon/analyze/schema.py` *first* — the schema and labeling contract anchor everything else.
- Stand up Unbound + browser policies on the dev machine, so URL capture is exercised against real browsing from day one.
- Smoke-test the KWin D-Bus screenshot path with one-shot `qdbus6 org.kde.KWin /org/kde/KWin/ScreenShot2 org.kde.KWin.ScreenShot2.CaptureActiveScreen …` before writing the Python wrapper, so we know the path works before we wire anything.

### Phase 1 — Capture + dashboard, no LLM (~1 week)
- Wayland (KWin) and X11 capture backends behind the `CaptureBackend` protocol. Wayland is primary; X11 backend stays for the phase-4 transition window and for testing.
- 60 s capture loop with same-window dedup, pHash ring buffer, messaging-app skip list.
- Heuristic-only classification (`.desktop` + AppStream + domain table from Toulouse + v2fly).
- FastAPI dashboard, LAN-only, basic auth.
- **Verify (the parent, on themselves):** 24 h soak; ~1440 capture rows; timeline of the parent's own workday is recognisable purely from heuristics; messaging-app windows recorded with no screenshot bytes on disk; pHash dedup drops the post-heuristic queue by ≥50 %.

### Phase 2 — trusted game detection + schedule + conflicts (~1 week) — DONE
- ~~GameMode D-Bus listener with `SIGSTOP` / `SIGCONT`~~ **reverted** (forgeable; see the Architecture note). Instead: **trusted game detection** — scan the installed Steam library + a curated list and match window PIDs, yielding a `game_running` signal the teen can't trivially forge.
- `schedule.yaml` parser + school timetable import (first importer: Kings InterHigh's student API).
- Dashboard "conflicts" view (leisure/streaming during a lesson).
- Heartbeat watcher.
- **Verify:** launch an installed game on the dev machine → its window classifies as `games`. Import the schedule; a distraction during a lesson block surfaces as a conflict on the dashboard and via `familymonctl conflicts`. *(Pausing the analyze/VLM worker during play is deferred to phase 3 — capture keeps running regardless.)*

### Phase 3 — Local VLM + parent-as-bootstrap-labeler (~2–3 weeks of calendar time, low daily effort)
- Install Ollama, pull MiniCPM-V 4.6 Q4_K_M.
- Wire OCR (Tesseract on foreground crop) and TF-IDF keyword classifier.
- Build `bootstrap_export.py` / `bootstrap_import.py` + `claude_code_label_pack/`. Run one real batch labeling session in Claude Code so the workflow is real, not theoretical.
- Aim for **~1000 labeled captures from the parent's own usage** before declaring graduation. Adult work patterns differ from a teen's, so expect that some categories will need re-tuning post-handover — the value here is validating the pipeline plumbing and getting the prompt + few-shot examples to a known-good baseline.
- Lock in few-shot examples for the local VLM. Eval harness reports macro-F1 against the held-out bootstrap labels.
- **Graduation criterion:** local pipeline ≥ 0.85 macro-F1 on held-out *and* `source` histogram shows >80 % of captures resolving without the VLM.

### Phase 4 — Swap & deploy on the kid's laptop (1–2 days)
- On the kid's laptop: `do-release-upgrade` 24.04 → 26.04, reboot, NVIDIA driver realigns. Clean up the disk. Set GRUB password. Create `parent` and kid users, the kid's non-sudo. Apply SDDM session lock.
- Install familymon. Apply the Unbound + browser-policies setup. Force-install the URL extension. Drop tray indicator into the kid's autostart.
- Have the disclosure conversation with the kid: what's captured, what isn't (messaging apps, blur-list), what's done with the data.
- Switch dogfood off the dev machine — it goes back to being a plain dev box, no familymon services running, and becomes the place to SSH from when a fresh batch needs labeling.

### Phase 5 — Recalibration on real kid data (~2 weeks calendar, light-touch)
- Let the kid use the laptop normally. Screenshots retained 14 d for the first labeling cycle (same as bootstrap).
- Run one or two Claude Code labeling sessions per week against the kid's data. The schema is identical; the eval set grows. Expect category F1 to dip initially (the kid's apps and sites overlap the parent-usage labels only partially), recover after one or two label cycles.
- Once F1 recovers on a kid-data held-out set, drop retention to 24 h.

### Phase 6 — Policy + blocking (later, out of scope here)
- Promote `policy/engine.py` from stub to enforcement.
- Enforcement layers: Unbound NXDOMAIN by category during schedule windows, process killer for known game executables, force-installed browser extension URL filter.
- Manual rule editing in the dashboard.

---

## Critical files to create first

Order matters; this is the dependency chain:

1. `familymon/storage/models.py` — schema first
2. `familymon/analyze/schema.py` — labeling contract second
3. `familymon/capture/backend.py` + `capture/x11.py` + `capture/kwin_wayland.py`
4. `familymon/capture/loop.py` with dedup + messaging-skip
5. `familymon/analyze/heuristics.py` + `categorize/domains.py`
6. `familymon/dashboard/app.py`
7. `deploy/systemd/*.service`, `deploy/policies/firefox/policies.json`, `deploy/policies/chrome/managed/familymon.json`, `deploy/unbound/familymon.conf`, `deploy/sddm/familymon.conf`

Phase 3 adds: `analyze/dedup.py`, `analyze/ocr.py`, `analyze/keyword_classifier.py`, `analyze/local_vlm.py`, `analyze/bootstrap_*.py`, `claude_code_label_pack/`.

---

## Open questions / parent-side decisions

Confirmed:
- Repo lives on GitHub (private during development; open-sourced in 2026).

Still open, to answer by the indicated phase:
- **By phase 3:** rhythm for batch labeling. Weekly 30-min Claude Code session is a reasonable default.
- **Before phase 4:** where does Claude Code live post-handover for ongoing labeling? Easiest: keep it on the dev machine, SSH-rsync batches from the kid's machine on a `familymon-batch.timer` or pull manually.
- **By phase 5:** conflict-alert threshold for the dashboard (start lenient; tighten with data). F1 graduation bar — 0.85 macro-F1 is a starting suggestion.
