# Gravity Gate — Design Plan

A modern, Raspberry Pi–hosted pinewood derby race manager that keeps every
function of the pack's current Windows software (most likely **DerbyMaster**
by Enterprising Ideas — see "Current software" below) and adds phone spectator
views, Google Sheets rosters, and photo finishes.

## 1. Goals

| # | Goal | Notes |
|---|------|-------|
| G1 | Runs on a small Raspberry Pi (Pi 4/5, 2 GB+) with no internet at the event | Python + SQLite, no build step, no cloud dependency on race day |
| G2 | Serial gantry timer support | Micro Wizard Fast Track, Champ, SmartLine/eTek, NewBold, PDT/DerbyMagic-style "generic" output, plus simulator |
| G3 | Easy Google Sheets roster sync | Zero-credential "publish to web CSV" import; optional service-account two-way sync |
| G4 | Dens stay together in rankings, races randomize across all dens | Chart heats are drawn from the whole pack, standings/awards are computed per den *and* pack-wide |
| G5 | Keep **double elimination** | Generalized N-lane elimination engine: single, double, triple elimination, with Bracket A/B/C like the old software |
| G6 | Feature parity with current software | Race, Standings, Awards, Manual Entry, Rerun Last Race, Go to Next Race, Sound On/Off, Cancel Display, on-deck list, Round/Race numbering |
| G7 | Enhanced mode: photo finish | Bottom-of-track Pi with camera; when the margin between places is under a threshold (default 10 ms) the display shows the still and a slit-scan photo-finish strip |
| G8 | Two-Pi topology | Top-of-track "hub" Pi (race control, projector, Wi-Fi), bottom-of-track "node" Pi (timer serial + camera) — either can run alone |
| G9 | Phone spectator view | Live race, on-deck, "follow my Scout", den leaderboards, bracket, lane stats, scale speed, photo finishes |

## 2. Current software

The screenshot (blue field, yellow-bordered black results panel, "Bracket A",
"Round 1 Race 3", place/time/car/name rows, an on-deck table with Round/Race/
Lane columns, small *Rerun Last Race* / *Go to Next Race* / *Sound On* buttons
and a bottom bar *Cancel Display | Race | Standings | Awards | Manual Entry*)
matches **DerbyMaster** (Enterprising Ideas, Windows shareware, ~$50). It is
one of the few packages that still offers Stearns charts, lane rotation,
single *and* double elimination, time or points scoring, 1–24 lanes and a
full-screen projector display. GrandPrix Race Manager, the most common
alternative, explicitly does not support elimination formats.

Confidence: high on the visual match and feature set, not verified from the
binary. To confirm (and to learn its exact timer settings and elimination
rules), install the program on a Windows machine and run REA's
`inspect_managed_artifact` / `open_binary` against `DerbyMaster.exe` — see §11.

## 3. Hardware topology

```
                 ┌──────────── TOP OF TRACK ───────────┐
 Start gate ──► │ Hub Pi  (gravitygate serve --role hub)    │──HDMI──► Projector (/display)
 (optional GPIO)│  • SQLite DB, web server, Wi-Fi AP    │
                 │  • race controller, sounds            │◄── Wi-Fi ── phones (/s), laptop (/admin)
                 └───────────────▲──────────────────────┘
                                 │ WebSocket over Ethernet/Wi-Fi
                 ┌───────────────┴──────────────────────┐
                 │ Node Pi (gravitygate node --hub ...)      │
 Timer ──USB/RS232►  • serial timer driver               │
 gantry          │  • camera ring buffer (Pi Camera 3 / │
 Camera ─CSI────►│    Global Shutter) → photo finish     │
                 └───────────── BOTTOM OF TRACK ────────┘
```

* **Single-Pi mode**: the hub opens the serial timer itself (`timer.source=serial`).
* **Two-Pi mode**: the node owns the timer + camera; it forwards results and
  images to the hub. The serial cable stays short and the camera sits at the
  finish line.
* The hub can run its own access point (NetworkManager hotspot) so the event
  needs no building Wi-Fi. `deploy/setup-hotspot.sh` configures it.

## 4. Software stack

* Python 3.11+, FastAPI + Uvicorn, WebSockets for live push.
* SQLite (stdlib `sqlite3`, WAL mode) — a single file you can back up to USB.
* `pyserial` for timers; `picamera2` + `numpy` + `Pillow` for photo finish (node only).
* Front end: plain HTML/CSS/JS served by the hub — no build tools, works on
  any phone browser, and is editable on the Pi.

## 5. Data model

```
racers(id, car_number, first_name, last_name, den, rank, checked_in, passed_inspection, notes)
sessions(id, name, kind[chart|elimination], config_json, status, created_at)
session_racers(session_id, racer_id, seed)
heats(id, session_id, seq, round, race_in_round, bracket, group_key, status)
heat_lanes(heat_id, lane, racer_id)
results(id, heat_id, attempt, lane, racer_id, time, place, dnf, source, valid, created_at)
photos(id, heat_id, attempt, kind, path, margin_ms, created_at)
settings(key, value)
```

Results are append-only per attempt; *Rerun Last Race* marks the attempt
invalid rather than deleting it, so nothing is ever lost.

## 6. Race formats

### 6.1 Qualifiers (round robin)

* **Lane round robin (default)**: every car runs every lane `runs_per_lane`
  times (Settings). 3 lanes × 2 runs = 6 races per car. Pool = all
  checked-in racers of the selected dens, shuffled and den-interleaved.
* With `C` cars and `N` active lanes, heat `h` puts car `(h + offset[l]) mod C`
  in lane `l`. Offsets are searched so pairwise differences are distinct (a
  modular Golomb ruler): most distinct opponents, no back-to-back heats.
* **Full round robin (option)**: every car meets every other car at least once,
  within each den (den heats alternate on the track) or across the pack.
  Greedy covering design + balanced lane assignment + spacing pass.
* Fewer cars than lanes, or a dead lane, is handled with byes / lane masks.
* Late arrivals get make-up heats (one per lane per run).
* **Standings (Settings toggle, live)**: average run time (default) or heat
  points (fastest = number of lanes, slowest = 1, DNF = 0). Drop runs: none,
  slowest, or fastest + slowest ("drop 1st and last": 6 runs → middle 4).
* Standings are always computed **per den and pack-wide**; awards default to
  top 3 per den plus top 3 overall. The champions bracket is seeded from the
  top K of each den.

### 6.2 Elimination (single / double / triple)

Generalized for multi-lane tracks, matching the old "Bracket A" display:

* Each racer accumulates losses; **Bracket A = 0 losses, Bracket B = 1 loss,
  Bracket C = 2** (triple). Reaching `max_losses` eliminates the racer.
* Each round, each bracket is split into balanced heats of ≤ N cars
  (7 cars on 3 lanes → 3, 2, 2). A lone car gets a bye; byes are spread.
* In each heat the bottom `k` finishers take a loss. `k` is `half`
  (floor(size/2), default) or `last` (last place only, slower but gentler).
* Optional **lane-rotated matchups**: each heat runs once per lane and the
  summed time decides — the fairest method and the main criticism of
  elimination races answered.
* Rematch avoidance: groupings are drawn to minimize repeated pairings.
  Lane assignment minimizes each car's repeat use of the same lane.
* When the remaining racers fit in one heat, brackets merge into a **Final**;
  the undefeated car still needs `max_losses` losses to be knocked out, so the
  "if necessary" second final happens automatically.
* Dead heats on the loss boundary flag the heat for rerun (photo finish helps).
* Final placings: champion, then reverse elimination order, ties broken by
  best time.

## 7. Race control (parity with current software)

| Old button | Gravity Gate |
|---|---|
| Race | `display=race`: current heat, results, on-deck heats |
| Standings | `display=standings`: per-den and overall tables |
| Awards | `display=awards`: reveal awards one at a time, bottom-up |
| Manual Entry | enter times *or* places per lane (timer failure) |
| Rerun Last Race | invalidate last attempt, re-arm timer, same heat |
| Go to Next Race | accept results, advance, re-arm timer (auto-advance option) |
| Sound On/Off | display plays start/finish/photo-finish sounds |
| Cancel Display | blank "welcome" screen with QR/URL for spectators |

Plus: timer status/heartbeat, lane mask, check-in & inspection, roster
import, schedule generation, export (CSV), backup.

## 8. Timer drivers

`gravitygate/timers/parsers.py` contains profiles. Each profile defines baud
rate, reset/arm commands and a parser. A tolerant generic parser accepts the
common shapes:

* `A=2.5060! B=2.5430" C=2.6730#` (Micro Wizard Fast Track; `!"#$%&` = place)
* `1=2.5060a 2=2.5430b 3=2.6730c` (Champ-style lane=time+place letter)
* `1 2.5060 2 2.5430 3 2.6730` / `2.5060 2.5430 2.6730` (positional)

A **raw capture / learn** mode logs every byte so a new timer's format can be
added in minutes. Profiles other than the generic parser should be verified
against the pack's actual timer before race day (marked in code).

## 9. Photo finish (enhanced mode)

* Node keeps a ring buffer of the last ~3 s of frames from the camera at the
  highest frame rate the sensor allows at a cropped resolution
  (Camera Module 3 ≈ 120 fps, Global Shutter Camera ≈ 200+ fps), each with a
  monotonic timestamp.
* When the timer reports and the gap between any two adjacent places is
  `< photo_margin_ms` (default 10 ms), the node:
  1. builds a **slit-scan** image from the finish-line pixel column across all
     frames (time on the x-axis — the classic photo-finish strip),
  2. detects each lane's crossing from that strip, and
  3. picks the frame nearest the first car's crossing, annotates lanes and
     times, and uploads both images to the hub.
* The display overlays the still; spectators see it in the "Photo finishes"
  feed. Optional start-gate GPIO on the hub (with chrony time sync between
  Pis) gives an absolute t₀ for cross-checking.
* Honest limit: 120–200 fps is 5–8 ms per frame, so the photo *illustrates* a
  close finish; the timer remains the official result unless the operator
  overrides it via Manual Entry.

## 10. Spectator view (`/s`)

Phone-first, live via WebSocket:
* **Now** — current heat with lanes, live results, photo finish.
* **On deck** — next heats; your followed racers highlighted with ETA.
* **Follow** — star racers (stored on the phone); their runs, average, rank.
* **Leaderboard** — per den and overall.
* **Bracket** — elimination brackets A/B/Final with losses.
* **Stats** — fastest run, lane averages (lane bias), closest finish,
  scale speed (mph at 1:25 scale), heats remaining, average heat cadence.

## 11. Using REA on the current software

REA (morluto/rea) reverse engineers shipped binaries. It is the right tool
for questions the screenshot cannot answer:

1. Exact elimination rules (how many losers per heat on a 3-lane track,
   how byes and the final are handled) — so Gravity Gate can offer a
   "DerbyMaster-compatible" mode.
2. Exact serial timer profiles (baud, commands, parse strings) for every
   timer it supports.
3. Data file format, so existing rosters/results can be imported.

Steps: `npx rea-agents setup` on a machine with the installed program, then
`inspect_managed_artifact` (if .NET) or `open_binary` (native, via Ghidra),
then search strings like `Bracket`, `Rerun`, `COM`, `Baud`. The installed
binary is not available to this build environment, so this step is pending.

## 12. Milestones

1. **Core** (this commit): DB, chart scheduler, scoring, elimination engine,
   timer parsers + simulator, race controller, REST/WS API, admin, display,
   spectator pages, CSV/Sheets import, photo-finish analysis + node agent, tests.
2. **Track test**: verify timer profile on the real gantry, tune photo ROI,
   dry-run a full event with simulated racers.
3. **Polish**: awards reveal animation, printable reports, QR on welcome
   screen, Google Sheets write-back of results.
4. **REA pass** on DerbyMaster for compatibility mode and data import.

## 13. Revisions after review

* **Results sync**: two-way Google Sheets via an Apps Script web app
  (`deploy/google-apps-script.gs`), offline-first background push of
  Results / Standings / Roster tabs; gspread service account as alternative.
* **QR codes**: generated on the Pi (segno, offline). Live-results QR on the
  welcome screen and in the race display corner, optional join-Wi-Fi QR,
  printable `/poster`. Public URL auto-detects the LAN address so the
  projector browser's `localhost` never ends up in the code.
* **Every heat photographed** (when the camera is on): phones get a recent
  finishes gallery and a photo per run under "My racers"; the projector pops
  up only finishes closer than `photo_margin_ms`. Analysis uses only frames
  since the timer was armed plus a post-roll, so the strip never mixes heats.
* **Bracket-line view** for elimination: rounds left → right, Bracket A above
  Bracket B, gold advance lines, red dashed drop lines, Final → Champion;
  barycenter ordering to reduce crossings; phone scroll + overview.
* **Qualifier spec**: lanes and runs per lane in Settings; lane round robin is
  the default qualifier; standings toggle and drop-runs in Settings.

## 14. Backlog

* Dead-simple live streaming to YouTube / Facebook (RTMP stream key, one
  "Go live" button that streams the display plus an optional track camera).
* Camera calibration UI, start-gate GPIO + chrony, printable reports,
  DerbyMaster data import and compatibility mode, verified timer profiles.
