# Race-Day Card

*Print this and keep it at the race director's table. Full details: [SETUP.md](SETUP.md).*

## Before the doors open (30 minutes)
- [ ] Network router on; boxes cabled and powered on.
- [ ] Each box's screen shows **Brain: connected** (or **THIS MACHINE is the brain**).
- [ ] Race director's laptop: open `http://<brain IP>:8000/admin` (the brain's screen shows the address).
- [ ] **Admin → Fleet**: every box online; the panel says the timer source is *the finish-line machine*.
- [ ] One test car down the track: times fill in, and a photo appears (if you have a camera).
- [ ] Race tab shows **no** yellow *Drop gate (simulator)* button. If it does, the timer isn't connected.
- [ ] Projector on **Cancel Display** (the QR welcome screen). Posters up at the doors.
- [ ] **Settings → Admin PIN** is set.

## Check-in
**Racers & check-in** → tick **Checked in** (and **Inspected**) as each car arrives.
Missing Scout? **Add racer** at the top. Their car number is assigned automatically.

## Qualifiers
1. **Sessions → New qualifier race → Create & activate.**
2. Projector → **Race**.
3. Loop: **Go to Next Race** → announcer calls the lanes → gate drops → results appear.

## When something goes wrong

| Problem | Do this |
|---|---|
| Car jumped the track / gate stuck / wrong car in a lane | **Rerun Last Race** (old times are kept in history, not counted). |
| Timer missed a car or a time looks wrong | Type lane times *or* places in the **Manual time / Manual place** boxes → **Save Manual Entry**. Tick **DNF** for a car that didn't finish. |
| Timer waiting forever for a car that fell off | **Force results**. |
| Car needs repair before its heat | **Skip heat (race later)**: the heat moves to the end of the queue, so nobody loses a run. |
| Scout arrives late | **Sessions → Heats → Add make-up heats**. |
| A lane breaks | **Settings → Lanes out of service**, for races you create from now on. |
| A box looks frozen | Its role restarts itself. Otherwise power-cycle that box; race data lives on the brain. |
| Close finish dispute | The photo pops up on the projector. The timer is official; correct it with Manual Entry if everyone agrees. |

## Awards
1. **Standings** tab: check for ties (`=`).
2. Projector → **Awards**; click **Reveal next award** for each one (3rd → 1st, den by den, then pack overall).

## Champions bracket
1. **Sessions → New elimination bracket** → Format *Double elimination* → Seed from *Top of each den in "Qualifiers"* → Top per den *2 or 3* → **Create & activate**.
2. Race it the same way: **Go to Next Race** each heat. New rounds appear automatically.
3. Projector → **Bracket** between heats to show who's still in.
4. Champion crowned → Projector **Awards**.

## After
- [ ] **Standings → Push to Google Sheets now** (or **Download results CSV**).
- [ ] Copy the database to a USB stick (SETUP.md, Appendix C).
- [ ] Shut the boxes down.

| Keys on any box's screen | |
|---|---|
| **B** | Make this the brain (only one!) |
| **F** / **D** | Toggle finish line / display |
| **S** | Standby (clear roles) |
| **Enter** (brain) | Fleet screen: ↑↓ select, **F**/**D**/**S** assign, **I** identify, **A** auto-arrange, **Esc** back |
| **Ctrl+Alt+F1** | Show the setup screen on a display box |
