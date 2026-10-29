# Maryland 2026 general election results scraper

Scrapes Governor, Congress (by district), and statewide ballot questions from the
Maryland State Board of Elections (SBE) results pages, and writes tidy CSVs plus
Flourish-ready CSVs.

## 1. Set up (VS Code terminal)

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Edit `USER_AGENT` near the top of `scrape.py` to include a real newsroom contact email.

## 2. Test against the 2022 archive

```bash
python scrape.py
```

This reads the live 2022 archive (Governor, all 8 districts, Questions 1–3) and writes
to `data/2022/`. **Commit `data/2022/master/gov_county.csv`** — it is the baseline for
the 2022 → 2026 shift map.

## 3. Election night

1. When SBE confirms the 2026 results address, set a repo variable
   `SBE_BASE_URL` (Settings → Secrets and variables → Actions → Variables),
   ending with `/`. If the index page isn't `index.html`, also add `--index <name>`
   in `.github/workflows/scrape.yml`.
2. Test it by hand first:
   `python scrape.py --base-url <URL> --out data/2026 --baseline data/2022/master/gov_county.csv`
3. Push. The workflow runs every ~10 min; use **Actions → Scrape MD election results →
   Run workflow** to force a run.
4. When you're done updating: **Actions → workflow → Disable workflow**.

## Outputs (`data/<year>/`)

| File | What it is |
|---|---|
| `master/gov_statewide.csv` | Every governor candidate incl. write-ins; votes by vote type, total, pct |
| `master/gov_county.csv` | County × candidate, votes and pct of all votes in that county |
| `master/house_districts.csv` | Each district × candidate |
| `master/q{1,2,3}_statewide.csv` | For/Against statewide |
| `master/q{1,2,3}_county.csv` | County × For/Against |
| `flourish/flourish_gov_bar.csv` | Governor bar chart |
| `flourish/flourish_gov_map.csv` | One row per county: Dem pct, Rep pct, **margin** (Dem − Rep). Feeds both the margin map and the vote-share map (`dem_pct`) |
| `flourish/flourish_q{N}_bar.csv` | Question bar chart |
| `flourish/flourish_q{N}_map.csv` | One row per county: For/Against pct, **margin** (For − Against), leader |
| `flourish/flourish_moore_shift.csv` | 2022 vs 2026 Dem vote share by county, `shift_pts`, centroid lat/lon (only when `--baseline` is passed) |

Every file carries `last_refreshed` and `precincts_reported` / `precincts_total` from
the SBE page, for "as of" labels. Join maps on **`fips`**.

**Flourish live link:**
`https://raw.githubusercontent.com/<org>/<repo>/main/data/2026/flourish/<file>.csv`
(the repo must be public).

## Definitions

- Governor vote share = candidate votes ÷ all votes in the race, including write-ins.
- Question vote share = For (or Against) ÷ (For + Against).
- Moore is identified as the `Democratic` candidate (and the opponent as `Republican`),
  so names never need to be hard-coded. Change `DEM_PARTY` / `REP_PARTY` if needed.

## Safety checks

The script writes **nothing** (exit code 1) if any check fails, so a bad scrape never
replaces good data:

- index page must link Governor, the expected number of districts, and every question
- county pages must have all 24 jurisdictions
- candidate rows must add up to the page's Totals row
- county sums must equal the statewide totals
- no file may have fewer rows than the previous run

## Notes

- Contests are found by **link text** on the index page, not hard-coded page numbers.
- County lat/lon in `reference/md_counties.csv` are approximate centroids for arrow
  placement; adjust if they look off on the map.
- `--fixtures <folder>` reads saved HTML pages instead of the web (for offline testing).
