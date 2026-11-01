"""
Maryland general election results scraper (MD State Board of Elections).

Scrapes:
  - Governor: statewide + county breakdown
  - Representative in Congress: each district (statewide page only)
  - Statewide ballot questions: statewide + county breakdown

Writes:
  <out>/master/   tidy "source of truth" CSVs
  <out>/flourish/ wide, Flourish-ready CSVs

Usage (2022 archive test):
  python scrape.py

Usage (2026 election night, once SBE confirms the URL):
  python scrape.py --base-url https://<host>/elections/2026/general_results/ \
                   --out data/2026 --baseline data/2022/master/gov_county.csv

If any validation check fails, NOTHING is written and the script exits with
code 1, so a broken scrape never overwrites the last good data.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Settings (all can be overridden on the command line)
# --------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://elections.maryland.gov/elections/archive/2022/general_results/"
DEFAULT_INDEX = "index.html"
DEFAULT_QUESTIONS = "1,2,3"
DEFAULT_DISTRICTS = 8
DEM_PARTY = "Democratic"      # used to find Moore (and his 2022 baseline) by party, not name
REP_PARTY = "Republican"
USER_AGENT = "MD-election-results-scraper (newsroom data desk; contact: your-email@example.com)"
REQUEST_PAUSE_SEC = 0.5
N_JURISDICTIONS = 24
GOV_MAIN_VIEW_TOP_N = 3
GOV_MAIN_VIEW_LABEL = "Leading candidates"
GOV_ALL_VIEW_LABEL = "All candidates"

HERE = Path(__file__).resolve().parent
COUNTIES = pd.read_csv(HERE / "reference" / "md_counties.csv", dtype={"fips": str})


class ScrapeError(Exception):
    """Raised when the data looks wrong. Nothing gets written."""


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, fixtures: Path | None = None):
        self.fixtures = fixtures
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def soup(self, url: str) -> BeautifulSoup:
        if self.fixtures:
            name = unquote(Path(urlparse(url).path).name)
            path = self.fixtures / name
            if not path.exists():
                raise ScrapeError(f"Fixture not found: {path}")
            return BeautifulSoup(path.read_text(encoding="utf-8"), "lxml")

        last_err = None
        for attempt in range(3):
            try:
                r = self.session.get(url, timeout=30)
                r.raise_for_status()
                time.sleep(REQUEST_PAUSE_SEC)
                return BeautifulSoup(r.text, "lxml")
            except requests.RequestException as e:
                last_err = e
                time.sleep(2 * (attempt + 1))
        raise ScrapeError(f"Could not fetch {url}: {last_err}")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def cell_lines(cell) -> list[str]:
    """Text of a table cell split on <br>, e.g. ['Wes Moore and Aruna Miller', 'Democratic']."""
    return [clean(t) for t in cell.get_text("\n").split("\n") if clean(t)]


def to_int(text: str):
    """'1,293,944' -> 1293944 ; '644,000 (32.12%)' -> 644000 ; 'NR' / '' -> None"""
    m = re.match(r"^\s*([\d,]+)", text or "")
    return int(m.group(1).replace(",", "")) if m else None


def to_pct(text: str):
    m = re.search(r"([\d.]+)\s*%", text or "")
    return float(m.group(1)) if m else None


def slug(header: str) -> str:
    h = header.lower()
    if "early" in h:
        return "early_voting"
    if "election day" in h:
        return "election_day"
    if "mail" in h:
        return "mail_in" if not re.search(r"\d", h) else "mail_in_" + re.sub(r"\D", "", h)
    if "provisional" in h:
        return "provisional"
    if h.strip() == "total":
        return "votes"
    if "percent" in h:
        return "pct_sbe"
    return re.sub(r"[^a-z0-9]+", "_", h).strip("_")


def norm_county(name: str) -> str:
    s = clean(name).lower().replace("’", "'")
    s = re.sub(r"^saint\s+", "st. ", s)
    s = re.sub(r"^st\s+", "st. ", s)
    if s not in ("baltimore county", "baltimore city") and s.endswith(" county"):
        s = s[: -len(" county")]
    return s


_COUNTY_LOOKUP = {}
for _, row in COUNTIES.iterrows():
    _COUNTY_LOOKUP[norm_county(row["county"])] = row["fips"]
    _COUNTY_LOOKUP[norm_county(row["county_full"])] = row["fips"]


def county_fips(name: str) -> str:
    key = norm_county(name)
    if key not in _COUNTY_LOOKUP:
        raise ScrapeError(f"Unknown jurisdiction name on SBE page: {name!r}")
    return _COUNTY_LOOKUP[key]


def normalize_response(label: str) -> str:
    l = label.lower()
    if l.startswith("for"):
        return "For"
    if l.startswith("against"):
        return "Against"
    raise ScrapeError(f"Unexpected ballot question response label: {label!r}")


# --------------------------------------------------------------------------
# Page parsing
# --------------------------------------------------------------------------
def page_meta(soup: BeautifulSoup) -> dict:
    text = clean(soup.get_text(" "))
    refreshed = re.search(r"Last refreshed:\s*([\d/]+\s+[\d:]+\s*[AP]M)", text)
    precincts = re.search(r"\(([\d,]+)\s+of\s+([\d,]+)\s+election day precincts reported\)", text)
    return {
        "last_refreshed": refreshed.group(1) if refreshed else "",
        "precincts_reported": to_int(precincts.group(1)) if precincts else None,
        "precincts_total": to_int(precincts.group(2)) if precincts else None,
    }


def tables_with_first_header(soup: BeautifulSoup, predicate):
    """Yield (header_cells, body_rows) for every table whose first header cell matches."""
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = rows[0].find_all(["th", "td"])
        if header and predicate(clean(header[0].get_text(" "))):
            yield header, rows[1:]


def county_breakdown_url(soup: BeautifulSoup, page_url: str) -> str:
    for a in soup.find_all("a", href=True):
        if "county break down" in clean(a.get_text(" ")).lower():
            return urljoin(page_url, a["href"])
    raise ScrapeError(f"No 'County Break Down' link on {page_url}")


def parse_statewide_candidates(soup: BeautifulSoup) -> pd.DataFrame:
    """Statewide (or district) candidate table: Name | Party | Early | ... | Total | Percentage."""
    found = list(tables_with_first_header(soup, lambda h: h.lower() == "name"))
    if not found:
        raise ScrapeError("No candidate table (first header 'Name') found")
    header, rows = found[0]
    cols = [slug(clean(h.get_text(" "))) for h in header]
    records, sbe_totals = [], None
    for tr in rows:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        name = clean(cells[0].get_text(" "))
        if name.lower().startswith("totals"):
            sbe_totals = to_int(cells[cols.index("votes")].get_text(" "))
            continue
        rec = {
            "candidate": clean(re.sub(r"\(write[\s-]*in\)", "", name, flags=re.I)),
            "party": clean(cells[1].get_text(" ")),
            "write_in": bool(re.search(r"write[\s-]*in", name, re.I)),
        }
        for col, cell in zip(cols[2:], cells[2:]):
            txt = clean(cell.get_text(" "))
            rec[col] = to_pct(txt) if col == "pct_sbe" else to_int(txt)
        records.append(rec)
    df = pd.DataFrame(records)
    if df.empty:
        raise ScrapeError("Candidate table has no rows")
    if sbe_totals is not None and df["votes"].fillna(0).sum() != sbe_totals:
        raise ScrapeError(f"Candidate votes sum {df['votes'].sum()} != page Totals {sbe_totals}")
    return df


def parse_statewide_question(soup: BeautifulSoup) -> pd.DataFrame:
    found = list(tables_with_first_header(soup, lambda h: h.lower().startswith("question")))
    if not found:
        raise ScrapeError("No question table (first header 'Question N') found")
    header, rows = found[0]
    cols = [slug(clean(h.get_text(" "))) for h in header]
    records = []
    for tr in rows:
        cells = tr.find_all(["th", "td"])
        label = clean(cells[0].get_text(" "))
        if label.lower().startswith("totals"):
            continue
        rec = {"response": normalize_response(label), "response_label": label}
        for col, cell in zip(cols[1:], cells[1:]):
            txt = clean(cell.get_text(" "))
            rec[col] = to_pct(txt) if col == "pct_sbe" else to_int(txt)
        records.append(rec)
    return pd.DataFrame(records)


def parse_county_breakdown(soup: BeautifulSoup) -> pd.DataFrame:
    """
    County page: one or more tables with Jurisdiction rows and one column per
    candidate/response. SBE splits wide races across several tables, so we
    read every 'Jurisdiction' table and stack them into long format.
    """
    long_rows, page_totals = [], {}
    for header, rows in tables_with_first_header(soup, lambda h: h.lower() == "jurisdiction"):
        choices = []
        for h in header[1:]:
            lines = cell_lines(h)
            write_in = any(re.search(r"write[\s-]*in", l, re.I) and l.startswith("(") for l in lines)
            name_lines = [l for l in lines if not (l.startswith("(") and "write" in l.lower())]
            name = name_lines[0] if name_lines else ""
            party = name_lines[-1] if len(name_lines) > 1 else ""
            choices.append({"choice": name, "party": party, "write_in": write_in})
        for tr in rows:
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            juris = clean(cells[0].get_text(" "))
            for ch, cell in zip(choices, cells[1:]):
                v = to_int(clean(cell.get_text(" ")))
                if juris.lower().startswith("totals"):
                    page_totals[ch["choice"]] = v
                else:
                    long_rows.append({"sbe_name": juris, **ch, "votes": v})
    df = pd.DataFrame(long_rows)
    if df.empty:
        raise ScrapeError("County breakdown has no 'Jurisdiction' tables")

    df["fips"] = df["sbe_name"].map(county_fips)
    n = df["fips"].nunique()
    if n != N_JURISDICTIONS:
        raise ScrapeError(f"Expected {N_JURISDICTIONS} jurisdictions, found {n}")
    sums = df.groupby("choice")["votes"].sum(min_count=1)
    for choice, total in page_totals.items():
        if total is not None and sums.get(choice) != total:
            raise ScrapeError(f"County sum for {choice!r} = {sums.get(choice)} but page Totals = {total}")
    df = df.merge(COUNTIES[["fips", "county"]], on="fips", how="left")
    return df


# --------------------------------------------------------------------------
# Index discovery: find contests by link text, not hard-coded numbers
# --------------------------------------------------------------------------
def discover(index_soup: BeautifulSoup, index_url: str) -> dict:
    links = [(clean(a.get_text(" ")), urljoin(index_url, a["href"])) for a in index_soup.find_all("a", href=True)]

    gov = next((u for t, u in links if t.lower().startswith("governor")), None)
    if not gov:
        raise ScrapeError("Governor link not found on index page")

    congress_main = next((u for t, u in links if t.lower().startswith("representative in congress")), None)
    districts = {}
    if congress_main:
        m = re.search(r"gen_results_\d{4}_(\d+)\.html", congress_main)
        if m:
            k = m.group(1)
            for t, u in links:
                dm = re.search(rf"gen_results_\d{{4}}_{k}_(\d+)\.html", u)
                if dm and t.isdigit():
                    districts[int(t)] = u

    questions = {}
    for t, u in links:
        qm = re.fullmatch(r"question\s+(\d+)", t.lower())
        if qm and "qresults" in u:
            questions.setdefault(int(qm.group(1)), u)

    return {"governor": gov, "districts": dict(sorted(districts.items())), "questions": questions}


# --------------------------------------------------------------------------
# Building outputs
# --------------------------------------------------------------------------
def add_meta(df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    df = df.copy()
    for k, v in meta.items():
        df[k] = v
    return df


def share(votes: pd.Series, total) -> pd.Series:
    return (votes / total * 100).round(2) if total else pd.Series([None] * len(votes), index=votes.index)


def governor_name(ticket: str) -> str:
    """'Wes Moore and Aruna Miller' -> 'Wes Moore'. Rows without a running mate
    (e.g. 'Other Write-Ins') are returned unchanged."""
    return clean(re.split(r"\s+and\s+", ticket, maxsplit=1)[0])


def build_governor(fetch: Fetcher, url: str):
    soup = fetch.soup(url)
    meta = page_meta(soup)
    state = parse_statewide_candidates(soup)
    state.insert(1, "governor", state["candidate"].map(governor_name))
    state["pct"] = share(state["votes"], state["votes"].sum())
    state = state.sort_values("votes", ascending=False)

    county = parse_county_breakdown(fetch.soup(county_breakdown_url(soup, url)))
    county = county.rename(columns={"choice": "candidate"})
    county.insert(county.columns.get_loc("candidate") + 1, "governor", county["candidate"].map(governor_name))
    county["county_total"] = county.groupby("fips")["votes"].transform("sum")
    county["pct"] = (county["votes"] / county["county_total"] * 100).round(2)

    # statewide page and county page must agree
    cs = county.groupby("candidate")["votes"].sum()
    for _, r in state.iterrows():
        if r["candidate"] in cs and cs[r["candidate"]] != r["votes"]:
            raise ScrapeError(f"Governor: county total for {r['candidate']} ({cs[r['candidate']]}) != statewide ({r['votes']})")

    return add_meta(state, meta), add_meta(county, meta), meta


def build_question(fetch: Fetcher, n: int, url: str):
    soup = fetch.soup(url)
    meta = page_meta(soup)
    state = parse_statewide_question(soup)
    state["pct"] = share(state["votes"], state["votes"].sum())
    state.insert(0, "question", n)

    county = parse_county_breakdown(fetch.soup(county_breakdown_url(soup, url)))
    county["response"] = county["choice"].map(normalize_response)
    county = county[["fips", "county", "response", "votes"]].copy()
    county["county_total"] = county.groupby("fips")["votes"].transform("sum")
    county["pct"] = (county["votes"] / county["county_total"] * 100).round(2)
    county.insert(0, "question", n)
    return add_meta(state, meta), add_meta(county, meta), meta


def build_house(fetch: Fetcher, districts: dict) -> pd.DataFrame:
    frames = []
    for d, url in districts.items():
        soup = fetch.soup(url)
        df = parse_statewide_candidates(soup)
        df["pct"] = share(df["votes"], df["votes"].sum())
        df.insert(0, "district", d)
        frames.append(add_meta(df, page_meta(soup)))
    return pd.concat(frames, ignore_index=True)


def flourish_question_files(n, state, county, meta):
    bar = add_display_columns(state[["response", "votes", "pct"]])
    wide = county.pivot_table(index=["fips", "county"], columns="response", values=["votes", "pct"], aggfunc="first")
    wide.columns = [f"{resp.lower()}_{val}" for val, resp in wide.columns]
    wide = wide.reset_index().merge(COUNTIES[["fips", "county_full"]], on="fips")
    wide["total_votes"] = wide["for_votes"].fillna(0) + wide["against_votes"].fillna(0)
    wide["margin"] = (wide["for_pct"] - wide["against_pct"]).round(2)
    wide["leader"] = wide["margin"].apply(lambda m: "For" if m > 0 else ("Against" if m < 0 else "Tied"))
    wide = wide[["fips", "county", "county_full", "for_votes", "against_votes", "total_votes",
                 "for_pct", "against_pct", "margin", "leader"]]
    return add_meta(bar, meta), add_meta(wide, meta)


def party_columns(county: pd.DataFrame, party: str, prefix: str) -> pd.DataFrame:
    sub = county[(county["party"] == party) & (~county["write_in"])]
    if sub["candidate"].nunique() != 1:
        raise ScrapeError(f"Expected exactly one {party} governor candidate, found {sub['candidate'].unique()}")
    sub = sub.assign(governor=sub["candidate"].map(governor_name))
    return sub[["fips", "governor", "candidate", "votes", "pct"]].rename(
        columns={"governor": f"{prefix}_governor", "candidate": f"{prefix}_candidate",
                 "votes": f"{prefix}_votes", "pct": f"{prefix}_pct"})


PCT_DISPLAY_DECIMALS = 1


def add_display_columns(df):
    df = df.copy()
    df["votes_display"] = df["votes"].apply(lambda v: f"{int(v):,}" if pd.notna(v) else "")
    df["pct_display"] = df["pct"].apply(lambda p: f"{p:.{PCT_DISPLAY_DECIMALS}f}%" if pd.notna(p) else "")
    return df


def gov_bar_table(state: pd.DataFrame) -> pd.DataFrame:
    bar = state[["governor", "candidate", "party", "votes", "pct"]].copy()
    bar["party"] = bar["party"].fillna("").replace("", "Write-in")
    bar = bar.sort_values("votes", ascending=False).reset_index(drop=True)
    bar["label"] = bar.apply(lambda r: r["governor"] if r["party"] == "Write-in" else f"{r['governor']} ({r['party']})", axis=1)
    bar["total_votes"] = bar["votes"].sum()
    bar = add_display_columns(bar)

    # one pct column per party (in vote order) so Flourish can colour bars by party
    parties = list(dict.fromkeys(bar["party"]))
    for p in parties:
        bar[p] = bar["pct"].where(bar["party"] == p)

    main = bar.head(GOV_MAIN_VIEW_TOP_N).assign(view=GOV_MAIN_VIEW_LABEL)
    full = bar.assign(view=GOV_ALL_VIEW_LABEL)
    cols = ["view", "governor", "party", "label", "votes", "pct", "votes_display", "pct_display", "total_votes", "candidate"] + parties
    return pd.concat([main, full], ignore_index=True)[cols]


def flourish_gov_files(state, county, meta):
    bar = gov_bar_table(state)

    base = county.groupby("fips", as_index=False)["votes"].sum().rename(columns={"votes": "total_votes"})
    m = (COUNTIES[["fips", "county", "county_full"]]
         .merge(party_columns(county, DEM_PARTY, "dem"), on="fips")
         .merge(party_columns(county, REP_PARTY, "rep"), on="fips")
         .merge(base, on="fips"))
    m["other_pct"] = (100 - m["dem_pct"] - m["rep_pct"]).round(2)
    m["margin"] = (m["dem_pct"] - m["rep_pct"]).round(2)
    m["leader"] = m["margin"].apply(lambda x: "Dem" if x > 0 else ("Rep" if x < 0 else "Tied"))
    fmt_pct = lambda p: f"{p:.{PCT_DISPLAY_DECIMALS}f}%" if pd.notna(p) else ""
    fmt_int = lambda v: f"{int(v):,}" if pd.notna(v) else ""
    for side in ("dem", "rep"):
        m[f"{side}_votes_display"] = m[f"{side}_votes"].apply(fmt_int)
        m[f"{side}_pct_display"] = m[f"{side}_pct"].apply(fmt_pct)
    m["margin_display"] = m.apply(
        lambda r: "Tied" if r["margin"] == 0 else
        f"{r['dem_governor'] if r['margin'] > 0 else r['rep_governor']} +{abs(r['margin']):.{PCT_DISPLAY_DECIMALS}f}",
        axis=1)
    return add_meta(bar, meta), add_meta(m, meta)


def flourish_shift(county_now: pd.DataFrame, baseline_path: Path, meta) -> pd.DataFrame:
    base = pd.read_csv(baseline_path, dtype={"fips": str})
    base_dem = party_columns(base, DEM_PARTY, "dem")
    now_dem = party_columns(county_now, DEM_PARTY, "dem")
    df = (COUNTIES.merge(base_dem[["fips", "dem_pct"]].rename(columns={"dem_pct": "pct_2022"}), on="fips")
                  .merge(now_dem[["fips", "dem_pct"]].rename(columns={"dem_pct": "pct_2026"}), on="fips"))
    df["shift_pts"] = (df["pct_2026"] - df["pct_2022"]).round(2)
    df["direction"] = df["shift_pts"].apply(lambda s: "Up" if s > 0 else ("Down" if s < 0 else "No change"))
    return add_meta(df[["fips", "county", "county_full", "lat", "lon", "pct_2022", "pct_2026",
                        "shift_pts", "direction"]], meta)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def run(args) -> dict[str, pd.DataFrame]:
    fetch = Fetcher(Path(args.fixtures) if args.fixtures else None)
    index_url = urljoin(args.base_url, args.index)
    found = discover(fetch.soup(index_url), index_url)
    print(f"Found governor, {len(found['districts'])} congressional districts, questions {sorted(found['questions'])}")

    want_q = [int(q) for q in args.questions.split(",") if q.strip()]
    missing = [q for q in want_q if q not in found["questions"]]
    if missing:
        raise ScrapeError(f"Questions {missing} not linked on index page")
    if len(found["districts"]) != args.districts:
        raise ScrapeError(f"Expected {args.districts} congressional districts, found {len(found['districts'])}")

    out: dict[str, pd.DataFrame] = {}

    gov_state, gov_county, gov_meta = build_governor(fetch, found["governor"])
    out["master/gov_statewide.csv"] = gov_state
    out["master/gov_county.csv"] = gov_county
    bar, gmap = flourish_gov_files(gov_state, gov_county, gov_meta)
    out["flourish/flourish_gov_bar.csv"] = bar
    out["flourish/flourish_gov_map.csv"] = gmap
    if args.baseline:
        out["flourish/flourish_moore_shift.csv"] = flourish_shift(gov_county, Path(args.baseline), gov_meta)

    if found["districts"]:
        out["master/house_districts.csv"] = build_house(fetch, found["districts"])

    for q in want_q:
        q_state, q_county, q_meta = build_question(fetch, q, found["questions"][q])
        out[f"master/q{q}_statewide.csv"] = q_state
        out[f"master/q{q}_county.csv"] = q_county
        qbar, qmap = flourish_question_files(q, q_state, q_county, q_meta)
        out[f"flourish/flourish_q{q}_bar.csv"] = qbar
        out[f"flourish/flourish_q{q}_map.csv"] = qmap

    return out


def check_against_previous(out: dict, out_dir: Path):
    """Refuse to publish if a race suddenly has fewer candidates/rows than last time."""
    for rel, df in out.items():
        prev = out_dir / rel
        if rel.startswith("master/") and prev.exists():
            n_prev = len(pd.read_csv(prev))
            if len(df) < n_prev:
                raise ScrapeError(f"{rel}: {len(df)} rows now vs {n_prev} last run")


def write_atomically(out: dict, out_dir: Path):
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for rel, df in out.items():
            p = tmp / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(p, index=False)
        for rel in out:
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp / rel), dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--out", default="data/2022")
    ap.add_argument("--questions", default=DEFAULT_QUESTIONS, help="comma-separated, e.g. 1,2,3")
    ap.add_argument("--districts", type=int, default=DEFAULT_DISTRICTS, help="expected number of House districts")
    ap.add_argument("--baseline", help="2022 master/gov_county.csv, enables the shift file")
    ap.add_argument("--fixtures", help="read saved HTML pages from this folder instead of the web")
    args = ap.parse_args()

    try:
        out = run(args)
        check_against_previous(out, Path(args.out))
    except ScrapeError as e:
        print(f"VALIDATION FAILED - nothing written. {e}", file=sys.stderr)
        sys.exit(1)

    write_atomically(out, Path(args.out))
    for rel, df in out.items():
        print(f"  wrote {Path(args.out) / rel}  ({len(df)} rows)")


if __name__ == "__main__":
    main()
