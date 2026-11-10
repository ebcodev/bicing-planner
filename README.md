# citibik

Experiments that combine the [CityBikes v2 API](https://api.citybik.es/v2/) with TypeSafe's
**Jev** model (`Choice` / `Noul` / `Score`), using CSV and XLSX files as inputs and outputs.
The target network is **Bicing (Barcelona)**, network id `bicing`.

Code fetches, filters, measures distances and does arithmetic. Jev only makes the calls that
need an understanding of language.

The main result is **Bicing Planner**, a desktop app for Windows that suggests the most reliable
pickup and drop-off stations for a trip. You can run it from source, or build it yourself into a
single `BicingPlanner.exe`.

## Build from a fresh copy

**The repository contains only source code. The `.exe` is not in it.** You generate it on your
own machine with the steps below. The other generated files aren't in the repository either
(see `.gitignore`): the virtual environment, the station data and history, and the icon. These
steps recreate all of them.

**Requirements:** Windows 10/11, [uv](https://docs.astral.sh/uv/) and an internet connection.
uv installs the right Python by itself. For the plain-language box you also need a TypeSafe API
key. You can add it later in the app's Settings.

Run every command from this folder (`citibik/`):

```bash
# 1. Install Python and the dependencies into .venv (from pyproject.toml / uv.lock)
uv sync

# 2. Download the current Bicing station data
#    -> data/bicing.csv (live snapshot) and data/history/bicing.csv (first history entry)
uv run python citybikes.py snapshot

# 3. Draw the app icon -> assets/bicing_planner.ico and assets/bicing_planner.png
uv run python make_icon.py

# 4. (optional) Run the app from source
uv run python app.py

# 5. Build the Windows executable -> dist/BicingPlanner.exe
uv run python build_exe.py

# 6. (optional) Check the executable without opening its window
#    -> writes %LOCALAPPDATA%\BicingPlanner\data\selftest.txt
dist/BicingPlanner.exe --selftest
```

Notes:

- **API key:** set it as an environment variable before step 4 or 6 (PowerShell:
  `$env:TYPESAFE_API_KEY = "..."`; permanently: `setx TYPESAFE_API_KEY "..."`), or paste it in the
  app's **Settings**. Without a key everything works except the plain-language box, and the
  self-test reports Jev as skipped. No key is ever bundled into the `.exe`.
- **Step 2** also seeds the `.exe`: the snapshot and history in `data/` at build time are bundled
  and copied to the user's data folder on first run. To give the planner more history before
  building, keep collecting for a while: `uv run python citybikes.py snapshot --every 600`
  (Ctrl+C to stop).
- **Step 5** takes about 30-60 s. Rebuild after any code change. If step 2 or 3 was skipped,
  `build_exe.py` stops and names the missing command.
- In Git Bash use `dist/BicingPlanner.exe`; in PowerShell or cmd use `.\dist\BicingPlanner.exe`.
- `uv run` always uses this folder's `.venv`. If another environment is active, uv prints a
  harmless `VIRTUAL_ENV ... will be ignored` warning.

## Data available

| Endpoint | Fields |
|---|---|
| `/v2/networks/bicing` (about 525 stations, no roaming vehicles) | `id, name, company[], location{city, country, latitude, longitude}, ebikes` + `stations[]` |
| station | `id, name, latitude, longitude, timestamp, free_bikes, empty_slots` |
| station `extra` | `uid, online, normal_bikes, ebikes, has_ebikes` |

Features of the Bicing data that shape the use cases:

- **Station names are street addresses**, in uppercase Catalan or Spanish abbreviations, for
  example `C/ SARDENYA, 178`, `AV. PARAL·LEL, 54` or `PL JOANIC - C / BRUNIQUER, 59`. No station
  is named after a landmark, so finding "Sagrada Família" takes interpretation, not a lookup.
- **Mechanical and e-bikes are counted separately** (`normal_bikes` / `ebikes`). This matters in
  a city with steep uphill areas (Montjuïc, Gràcia, Horta, Collserola).
- **`online`** shows whether a station is out of service.
- **Missing fields:** no `slots`, `payment`, `renting`/`returning` or `last_updated`. Total
  docks can be estimated as `free_bikes + empty_slots`.
- **Users write in Catalan, Spanish and English.**

```bash
uv run python citybikes.py stations                # -> data/bicing.csv (live snapshot only)
uv run python citybikes.py snapshot                # -> data/bicing.csv + one entry in data/history/bicing.csv
uv run python citybikes.py snapshot --every 600    # same, every 10 minutes until Ctrl+C
```

### Files

Source files (in the repository):

| File | Contents |
|---|---|
| `app.py` | **Desktop app** (CustomTkinter window with an OpenStreetMap map) |
| `jev_assistant.py` | Turns a plain-language request into form values with Jev |
| `geocode.py` | Address search limited to Barcelona (OpenStreetMap Nominatim) |
| `i18n.py` | All interface text in English, Catalan and Spanish |
| `app_config.py`, `app_paths.py` | API key, language and settings; where the app keeps its files |
| `citybikes.py` | Bicing fetcher: live snapshot and history logger |
| `planner.py` | Trip planner: reliable pickup and drop-off stations (use case 1) |
| `make_icon.py` | Draws the app icon into `assets/` |
| `build_exe.py` | Packages the app as `dist/BicingPlanner.exe` (PyInstaller) |
| `pyproject.toml`, `uv.lock` | Dependencies, pinned for `uv sync` |
| `data/landmarks.csv` | Hand-made list of Barcelona landmarks (used by the `planner.py` command line) |
| `.gitignore` | Keeps the generated files below out of the repository |

Generated files (ignored by git; the command recreates them):

| File | Created by |
|---|---|
| `.venv/` | `uv sync` |
| `data/bicing.csv` | `citybikes.py snapshot` / `stations`, or **Update now** in the app |
| `data/history/bicing.csv` | `citybikes.py snapshot`, or each update in the app |
| `data/geocode_cache.json` | The app's address searches |
| `data/plan.csv` and other exports | `planner.py -o …` / **Save as CSV** |
| `assets/bicing_planner.ico`, `.png` | `make_icon.py` |
| `build/`, `dist/BicingPlanner.exe` | `build_exe.py` |

## Desktop app

```bash
uv run python app.py       # after steps 1-2 of "Build from a fresh copy"
```

Without any station data the app offers to download it on the first search, so step 2 is
optional when running from source. Without the icon the window simply uses the default one.

- **Ask bar (Jev):** type a request in English, Catalan or Spanish, for example *"vull una bici
  elèctrica de la Sagrada Família a Park Güell demà a les 9"*. Jev fills in the form and the
  app looks up the addresses. You check the form and press the button. This needs a TypeSafe
  key: `TYPESAFE_API_KEY` if it is set, otherwise the one saved in **Settings**
  (`%APPDATA%\BicingPlanner\config.json`, plain text).
- **Plan trip tab:** From and To are address searches limited to Barcelona. A search runs on
  Enter or Find; the best match is used and a dropdown offers the others. Also: When (now /
  leave at / arrive by, today or tomorrow), Bike (any / e-bike / mechanical) and the longest
  walk you accept. The result shows the pickup and drop-off stations with the reasons,
  alternatives, and a map: A and B for your places, numbered pins for candidate stations
  coloured by reliability, the ride as a blue line.
- **Nearby tab:** stations around one address with live bikes, e-bikes and free docks.
- **Fresh data:** before planning or searching nearby, if the live data is more than 10 minutes
  old, the app asks whether to update the snapshot. Each update also adds to the history, so the
  "usually free" numbers improve the more the app is used. **Update now** does the same at any time.
- **Save as CSV:** writes the stations in the current result (UTF-8 with BOM, so Excel shows accents).
- **Language:** English, Català or Español from the menu at the top right. The switch is immediate,
  keeps the form and the current result, and is remembered. On first start the app follows the
  Windows display language (Catalan or Spanish if that's the setting, otherwise English). Address
  results come back in the chosen language; station names stay as Bicing publishes them.
- **Online services:** CityBikes API (station data), OpenStreetMap tiles (map), Nominatim
  (addresses, at most one search per second, never while typing) and TypeSafe (Jev).

### Windows executable (built by you)

```bash
uv run python build_exe.py      # -> dist/BicingPlanner.exe (single file, about 29 MB)
```

The `.exe` is not provided with the project. Build it on your machine after steps 1-3 of "Build
from a fresh copy", because the build needs the icon and the station data. `dist/` is ignored by
git, so never commit the `.exe`; rebuild it after pulling or changing code.

- Once built, double-click `dist\BicingPlanner.exe`. It runs on its own: you can copy that single
  file to another Windows PC and it doesn't need Python or uv there. The window opens in about
  2 seconds.
- **Where it keeps data:** station data and history in `%LOCALAPPDATA%\BicingPlanner\data`;
  settings (API key, language) in `%APPDATA%\BicingPlanner\config.json`. Replacing or moving the
  `.exe` keeps both.
- **First run:** the live snapshot and history in `data/` at build time are bundled into the
  `.exe` and copied to the data folder, dated with their real snapshot time. If that's more than
  10 minutes old, the first search offers an update.
- **API key:** none is bundled. The `.exe` uses `TYPESAFE_API_KEY` or asks once in Settings.
- **Diagnostics:** `BicingPlanner.exe --selftest` checks the data, the live download, address
  search, the planner and one Jev call without opening the window, and writes
  `selftest.txt` in the data folder.
- When the `.exe` is shared (downloaded, emailed), Windows SmartScreen may warn because it is
  unsigned ("More info" → "Run anyway"); signing it would need a code-signing certificate.

---

## Use case 1: multilingual station finder (output: CSV)

A rider might write *"vull una bici elèctrica a prop de la Sagrada Família per pujar al Park
Güell"*, *"bici normal cerca de Plaça Catalunya"* or *"e-bike from Barceloneta beach"*.

1. **Jev turns the request into typed parameters.** All questions go in one `system_one` call:
   - `Choice` for intent: `rent | return | rent_and_return`
   - `Choice` for bike type: `ebike | mechanical | either`
   - `Noul`: "the rider's trip goes significantly uphill". If it does, code prefers e-bikes even
     when the rider didn't ask for one.
2. **Jev picks the place the rider means.** Station names are addresses, so code shortlists
   candidates from a small `landmarks.csv` (name, lat, lon) plus the station street names. A
   `Choice` over that list (plus `none`) picks the intended origin and destination. The model
   chooses from real places instead of inventing coordinates.
3. **Jev reads trip details:** departure time (*"demà a les 9"*, *"after work"*), and a `Score`
   for walking tolerance (*"amb maleta"* means a short walk, *"no tinc pressa"* means a longer
   one). Code maps the walking score to `--radius`.
4. **Code plans the trip with `planner.py`** (built, standard library only). It picks the
   **origin** station that usually has the right bike type and the **destination** station that
   usually has free docks *at the arrival time*, not just right now.
5. **Output:** `plan.csv` with the top candidates for each end and the reasons, for example:

```
Drop off at: PL. POETA BOSCÀ | C/ ATLÀNTIDA (240 m walk): had free docks 73% of the time
  [weekday 09h, n=20]; skipped closer C/ PONTEVEDRA, 58B (229 m, 18%),
  PG. MARÍTIM DE LA BARCELONETA,6 (129 m, 9%)
```

Pattern: route and fill arguments, and select instead of generate. Jev handles the language;
`planner.py` handles distances, history and policy.

### How `planner.py` decides

| Step | Rule (constants at the top of `planner.py`) |
|---|---|
| History | `citybikes.py snapshot --every 600` (or each update in the app) appends to `data/history/bicing.csv`. Run it for days to weeks. |
| "Works" | Origin: at least `MIN_BIKES` (2) of the requested type and `online`. Destination: at least `MIN_DOCKS` (2) free docks and `online`. |
| Time bucket | Weekday or weekend × hour in Barcelona local time. Arrival hour = departure + estimated ride time (straight line × 1.3 at 14 km/h). |
| Reliability | Share of past snapshots in that bucket where the station worked. If fewer than 3 samples, it widens to ±1 h, then to all history, then to the live snapshot only. |
| Uncertainty | Smoothed toward 50% (`PRIOR_WEIGHT` = 2 samples), so one lucky snapshot (67%) doesn't beat 20 days of evidence. |
| Score | `reliability - 0.04 × (walk_m / 100)`. Each extra 100 m of walking must buy at least 4 points of reliability. |
| Radius | Starts at 500 m. If nothing usable is found it widens by 250 m, up to 1250 m (needed around Park Güell, for example). |
| Warning | Flags when the chosen station does not meet the need in the live snapshot right now. |

```bash
uv run python planner.py --from "Sagrada Família" --to "Barceloneta" --at "2026-11-11 09:00"
uv run python planner.py --from 41.3870,2.1700 --to "Park Güell" --ebike -o data/plan.csv
uv run python planner.py --from "Sants" --to "Camp Nou" --arrive-by "2026-11-11 20:30"
```

`--from` and `--to` accept `lat,lon` or an exact name or alias from `data/landmarks.csv`.
Matching free text to a landmark is Jev's job.

## Use case 2: citizen feedback triage linked to live stations (input: CSV/XLSX, output: XLSX)

Input is `feedback.xlsx` with complaints or social posts in Catalan, Spanish or English, for
example *"l'estació de Sardenya 178 sempre està buida al matí"* or *"no hay sitio para dejar
la bici en Paral·lel"*.

1. **Jev classifies each message:**
   - `Choice` for category: `station_empty | station_full | station_offline | broken_bike |
     ebike_battery | app_or_account | other`
   - `Score` for urgency
   - `Noul`: "the message refers to a specific station or street"
2. **Jev links the message to a station.** Code shortlists Bicing stations whose address
   matches words in the message. It handles abbreviations such as `C/`, `AV.` and `PG.`, and
   spellings such as "Paral·lel", "Paralelo" and "PARAL.LEL". A `Choice` over that shortlist
   (plus `none`) picks the station meant.
3. **Code checks the complaint against live data.** For example, `station_empty` with
   `free_bikes == 0` is marked *confirmed now*, and `station_offline` with `online == False`
   is marked *confirmed*.
4. **Output:** `feedback_triage.xlsx` with one row per message (category, urgency, linked
   station, confirmed against the snapshot), plus a sheet per station listing all complaints
   about it.

Pattern: select instead of generate, then verify against observed state.

## Use case 3: event-day rebalancing planner (input: events CSV/XLSX, output: XLSX)

Input is `events.xlsx` with one row per event: `name, venue, date, start_time, end_time,
expected_attendance, description`. Examples: a match at Spotify Camp Nou, a concert at Palau
Sant Jordi (on Montjuïc, uphill), Primavera Sound at Parc del Fòrum, the Mobile World Congress
at Fira Gran Via, or La Mercè festivities.

1. **Code picks nearby stations.** It looks up the venue in `landmarks.csv` and takes the
   stations within about 800 m from the latest snapshot.
2. **Jev judges each event and its stations:**
   - `Score`, bike demand impact: *"how much extra bike-share demand will this event create?"*
     A festival crowd differs from a business congress or a gala.
   - `Choice`, peak direction: `arrivals | departures | both`
   - `Noul`: "attendees reach this venue by going uphill". If so, code targets e-bike supply
     for arrivals.
   - For each candidate station, `Score` on how well it serves the venue's entrances, using
     its street address.
3. **Code builds the plan.** It combines the demand score, attendance and the current
   `normal_bikes` / `ebikes` / `empty_slots` into target levels: fill docks before departure
   peaks, clear docks before arrival peaks. It skips stations that are not `online`.
4. **Output:** `event_rebalancing_plan.xlsx` with one sheet per event, listing stations,
   current level, target level, and mechanical bikes and e-bikes to add or remove.

Pattern: find and judge evidence, then let code apply the policy.

---

## Status

- `citybikes.py` works. It fetches only Bicing and flattens stations into CSV using only the
  standard library, including `online`, `normal_bikes` and `ebikes`.
- A snapshot on 2026-11-10 showed 525 stations: 5 offline, 92 with no bikes, 12 full and 217
  with no e-bikes. The live count changes slightly from fetch to fetch (523 to 525).
- `citybikes.py snapshot` builds the availability history; it repeats with `--every`, at least
  every 300 s. Reliability numbers mean little until it has run for a few days.
- `planner.py` works. It was tested on live data and on simulated history: two closer stations
  full ~90% of weekday mornings and a third one usually free. It picked the free one.
- `data/landmarks.csv` has 18 Barcelona landmarks with approximate coordinates and aliases in
  Catalan, Spanish and English.
- **Desktop app** (`app.py`) works when run from source. Tested with scripted sessions: trip plan
  (now, and arrive by tomorrow 09:00 with an e-bike), nearby free docks, the update prompt, and
  address search.
- **Jev request parsing** (`jev_assistant.py`) was tested on 6 requests in Catalan, Spanish and
  English, about 0.4 s and 2-4k tokens each. Places, bike type, uphill → e-bike, luggage → short
  walk, "arrive by 20:30", "tomorrow at 9", "where can I leave the bike" → free docks, and an
  off-topic question were all handled correctly after one wording fix to the time question.
- **Languages:** switching between English, Catalan and Spanish was tested with a trip result on
  screen and on both tabs; the form and result are kept.
- **The `.exe` build** (`build_exe.py` → `dist/BicingPlanner.exe`, not committed) was tested from a
  clean copy of the repository following "Build from a fresh copy". It builds in about 25-45 s. `--selftest` passed every check (seeded
  data, live download, address search, trip plan, Jev), and the window opens in about 1.5 s with
  the map and the bike icon.
