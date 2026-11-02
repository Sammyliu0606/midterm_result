"""
Maryland general election results scraper (MD State Board of Elections).

Scrapes:
  - Governor: statewide + county breakdown
  - Representative in Congress: each district (statewide page only)
  - Statewide ballot questions: statewide + county breakdown

Writes:
  <out>/master/   tidy "source of truth" CSVs
  <out>/flourish/ wide, Flourish-ready CSVs

Usage (2022 archive test; writes to data/live):
  python scrape.py

Usage (2026 election night):
  python scrape.py --base-url https://elections.maryland.gov/elections/2026/general_results/

Output always goes to data/live/ (unless --out is given), so Flourish links never
change when you switch from the 2022 test to 2026. The 2022 county baseline for the
shift map (Moore's official 2022 county results) is read from
reference/gov_2022_county.csv automatically.

If any validation check fails, NOTHING is written and the script exits with
code 1, so a broken scrape never overwrites the last good data.
"""

from __future__ import annotations

import argparse
import json
import math
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
# Popup text colors for the governor map. Set these to the two END colors of your
# Flourish diverging palette (the darkest blue and darkest red) so text matches the map.
DEM_COLOR = "#2166ac"
REP_COLOR = "#b2182b"
TIE_COLOR = "#555555"
# Ballot question maps: use non-partisan colors so For/Against isn't read as Dem/Rep.
# Set to the END colors of the Flourish palette you pick for the question maps.
Q_FOR_COLOR = "#77BB41"       # green (matches the question bar charts)
Q_AGAINST_COLOR = "#FF9300"   # orange
OTHER_PARTIES_LABEL = "Other parties"        # legend label for everyone not Dem/Rep

# Shift arrow map: arrow length = |margin shift| x ARROW_DEG_PER_POINT (map degrees).
# 0.02 makes a 10-point shift about 0.2 degrees (~20 km). Raise to make arrows longer.
ARROW_DEG_PER_POINT = 0.02
ARROW_TILT_DEG = 45          # 0 = flat left/right; 45 = NYT-style diagonal
ARROW_HEAD_FRACTION = 0.35   # head size relative to arrow length
ARROW_HEAD_MIN_DEG = 0.02    # so small shifts still get a visible head
ARROW_HEAD_ANGLE_DEG = 28
GOV_MAIN_VIEW_TOP_N = 3                    # candidates shown before "All candidates"
GOV_MAIN_VIEW_LABEL = "Leading candidates"
GOV_ALL_VIEW_LABEL = "All candidates"
N_JURISDICTIONS = 24

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

    # statewide page and county page must agree (NR / blank counts as 0 on both sides)
    cs = county.groupby("candidate")["votes"].sum()
    for _, r in state.iterrows():
        sw = 0 if pd.isna(r["votes"]) else r["votes"]
        if r["candidate"] in cs and cs[r["candidate"]] != sw:
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
    # response_label = SBE's wording, e.g. "For the Constitutional Amendment"
    # "For" / "Against" columns hold pct only on their own row, so Flourish can color each bar
    bar = add_display_columns(state[["response", "response_label", "votes", "pct"]])
    for r in ("For", "Against"):
        bar[r] = bar["pct"].where(bar["response"] == r)
    # pivot (not pivot_table) keeps columns that are still all blank before results come in
    wide = county.pivot(index=["fips", "county"], columns="response", values=["votes", "pct"])
    wide.columns = [f"{resp.lower()}_{val}" for val, resp in wide.columns]
    for c in ("for_votes", "against_votes", "for_pct", "against_pct"):
        if c not in wide:
            wide[c] = float("nan")
    wide = wide.astype(float).reset_index().merge(COUNTIES[["fips", "county_full"]], on="fips")
    wide["total_votes"] = wide["for_votes"].fillna(0) + wide["against_votes"].fillna(0)
    wide["margin"] = (wide["for_pct"] - wide["against_pct"]).round(2)
    wide["leader"] = wide["margin"].apply(lambda m: leader_text(m, "For", "Against"))
    # pre-formatted popup text, same pattern as the governor map
    for side in ("for", "against"):
        wide[f"{side}_votes_display"] = wide[f"{side}_votes"].apply(fmt_int)
        wide[f"{side}_pct_display"] = wide[f"{side}_pct"].apply(fmt_pct)
    wide["margin_display"] = wide["margin"].apply(lambda x: margin_text(x, "For", "Against"))
    wide["margin_color"] = wide["margin"].apply(lambda x: margin_color(x, Q_FOR_COLOR, Q_AGAINST_COLOR))
    # vote-share map (For %): headline text for the popup
    wide["for_share_display"] = wide["for_pct"].apply(lambda p: f"For: {fmt_pct(p)}" if pd.notna(p) else NO_RESULTS_TEXT)
    wide = wide[["fips", "county", "county_full", "for_votes", "against_votes", "total_votes",
                 "for_pct", "against_pct", "margin", "leader",
                 "for_votes_display", "for_pct_display", "against_votes_display", "against_pct_display",
                 "margin_display", "margin_color", "for_share_display"]]
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
NO_RESULTS_TEXT = "No results yet"


def fmt_pct(p) -> str:
    return f"{p:.{PCT_DISPLAY_DECIMALS}f}%" if pd.notna(p) else ""


def fmt_int(v) -> str:
    return f"{int(v):,}" if pd.notna(v) else ""


def margin_text(margin, pos_name: str, neg_name: str) -> str:
    """+18.57 -> 'Wes Moore +18.6' ; -33.87 -> 'Dan Cox +33.9' ; NaN -> 'No results yet'."""
    if pd.isna(margin):
        return NO_RESULTS_TEXT
    if margin == 0:
        return "Tied"
    return f"{pos_name if margin > 0 else neg_name} +{abs(margin):.{PCT_DISPLAY_DECIMALS}f}"


def margin_color(margin, pos_color: str, neg_color: str) -> str:
    if pd.isna(margin) or margin == 0:
        return TIE_COLOR
    return pos_color if margin > 0 else neg_color


def leader_text(margin, pos: str, neg: str) -> str:
    if pd.isna(margin):
        return NO_RESULTS_TEXT
    return pos if margin > 0 else (neg if margin < 0 else "Tied")


def add_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Pre-formatted text for Flourish popups: votes_display '1,293,944', pct_display '64.5%'."""
    df = df.copy()
    df["votes_display"] = df["votes"].apply(fmt_int)
    df["pct_display"] = df["pct"].apply(fmt_pct)
    return df


def gov_bar_table(state: pd.DataFrame) -> pd.DataFrame:
    """
    Governor bar-chart data for Flourish.

    - `view`: every row appears under "All candidates"; the top GOV_MAIN_VIEW_TOP_N
      also appear under "Leading candidates". Bind `view` to Flourish's Row filter
      to get a NYT-style "show all candidates" toggle.
    - One column per party (e.g. "Democratic", "Republican") holding that
      candidate's pct, blank for everyone else. Bind these as Values in a
      stacked bar chart so each bar is colored by party and gets a legend.
    """
    bar = state[["governor", "candidate", "party", "votes", "pct"]].copy()
    bar["party"] = bar["party"].fillna("").replace("", "Write-in")
    bar = bar.sort_values("votes", ascending=False).reset_index(drop=True)
    bar["label"] = bar["governor"].where(bar["party"] == "Write-in", bar["governor"] + " (" + bar["party"] + ")")
    bar["total_votes"] = bar["votes"].sum()
    bar = add_display_columns(bar)

    # Legend groups: always exactly these three columns, so Flourish bindings never change.
    # The real party name stays in the "party" column for popups.
    bar["party_group"] = bar["party"].apply(lambda p: p if p in (DEM_PARTY, REP_PARTY) else OTHER_PARTIES_LABEL)
    parties = [DEM_PARTY, REP_PARTY, OTHER_PARTIES_LABEL]
    for p in parties:
        bar[p] = bar["pct"].where(bar["party_group"] == p)

    main = bar.head(GOV_MAIN_VIEW_TOP_N).assign(view=GOV_MAIN_VIEW_LABEL)
    full = bar.assign(view=GOV_ALL_VIEW_LABEL)
    out = pd.concat([main, full], ignore_index=True)     # main view first = Flourish default
    return out[["view", "governor", "party", "party_group", "label", "votes", "pct", "votes_display", "pct_display",
                "total_votes", "candidate"] + parties]


NO_RESULTS_TEXT = "No results yet"


def fmt_pct(p) -> str:
    return f"{p:.{PCT_DISPLAY_DECIMALS}f}%" if pd.notna(p) else ""


def flourish_gov_files(state, county, meta):
    bar = gov_bar_table(state)

    base = county.groupby("fips", as_index=False)["votes"].sum().rename(columns={"votes": "total_votes"})
    m = (COUNTIES[["fips", "county", "county_full"]]
         .merge(party_columns(county, DEM_PARTY, "dem"), on="fips")
         .merge(party_columns(county, REP_PARTY, "rep"), on="fips")
         .merge(base, on="fips"))
    m["other_pct"] = (100 - m["dem_pct"] - m["rep_pct"]).round(2)
    m["margin"] = (m["dem_pct"] - m["rep_pct"]).round(2)
    m["leader"] = m["margin"].apply(lambda x: leader_text(x, "Dem", "Rep"))

    # pre-formatted text for popups (Flourish shows these as-is)
    for side in ("dem", "rep"):
        m[f"{side}_votes_display"] = m[f"{side}_votes"].apply(fmt_int)
        m[f"{side}_pct_display"] = m[f"{side}_pct"].apply(fmt_pct)
    # margin map
    m["margin_display"] = m.apply(lambda r: margin_text(r["margin"], r["dem_governor"], r["rep_governor"]), axis=1)
    m["margin_color"] = m["margin"].apply(lambda x: margin_color(x, DEM_COLOR, REP_COLOR))
    # vote-share map (color by dem_pct): headline text for the popup
    m["dem_share_display"] = m.apply(
        lambda r: f"{r['dem_governor']}: {fmt_pct(r['dem_pct'])}" if pd.notna(r["dem_pct"]) else NO_RESULTS_TEXT, axis=1)
    # winner's vote-share map: color each county for whoever won it, shaded by the winner's share
    def winner_cols(r):
        if pd.isna(r["margin"]) or r["margin"] == 0:
            return pd.Series({"winner": "", "winner_party": "", "winner_pct": float("nan"),
                              "winner_share_signed": float("nan"),
                              "winner_bin": NO_RESULTS_TEXT if pd.isna(r["margin"]) else "Tied"})
        dem_won = r["margin"] > 0
        pct = r["dem_pct"] if dem_won else r["rep_pct"]
        party = "Democrat" if dem_won else "Republican"
        return pd.Series({
            "winner": r["dem_governor"] if dem_won else r["rep_governor"],
            "winner_party": party,
            "winner_pct": pct,
            "winner_share_signed": pct if dem_won else -pct,   # + = Dem win, - = Rep win
            "winner_bin": f"{party} {share_bin(pct)}",         # stable labels for categorical colors
        })
    m = pd.concat([m, m.apply(winner_cols, axis=1)], axis=1)
    m["winner_pct_display"] = m["winner_pct"].apply(fmt_pct)
    m["winner_display"] = m.apply(
        lambda r: f"{r['winner']} won with {fmt_pct(r['winner_pct'])}" if r["winner"] else r["winner_bin"], axis=1)
    return add_meta(bar, meta), add_meta(m, meta)


WINNER_SHARE_BINS = [(0, 50, "under 50%"), (50, 60, "50-60%"), (60, 70, "60-70%"),
                     (70, 80, "70-80%"), (80, 101, "80%+")]


def share_bin(pct) -> str:
    for lo, hi, label in WINNER_SHARE_BINS:
        if lo <= pct < hi:
            return label
    return ""


# --------------------------------------------------------------------------
# Margin-shift arrow map
# --------------------------------------------------------------------------
def year_of(text: str, default: str) -> str:
    m = re.search(r"(20\d\d)", str(text or ""))
    return m.group(1) if m else default


def arrow_lines(lon: float, lat: float, shift: float) -> list:
    """
    Arrow from the county center. Shift toward Democrats points up-right,
    toward Republicans up-left; length grows with the size of the shift.
    Returns GeoJSON MultiLineString coordinates: [shaft, head-left, head-right].
    """
    if pd.isna(shift) or shift == 0:
        return []
    k = math.cos(math.radians(lat))                       # squash longitude so angles look right on the map
    length = abs(shift) * ARROW_DEG_PER_POINT
    sign = 1 if shift > 0 else -1
    ang = math.radians(ARROW_TILT_DEG)
    dx, dy = sign * length * math.cos(ang), length * math.sin(ang)
    tip = (lon + dx / k, lat + dy)
    head = min(max(length * ARROW_HEAD_FRACTION, ARROW_HEAD_MIN_DEG), length)
    back = math.atan2(-dy, -dx)                           # direction from tip back toward the start
    wings = []
    for side in (-1, 1):
        a = back + side * math.radians(ARROW_HEAD_ANGLE_DEG)
        wings.append([[tip[0], tip[1]], [tip[0] + head * math.cos(a) / k, tip[1] + head * math.sin(a)]])
    r = lambda p: [round(p[0], 5), round(p[1], 5)]
    return [[r((lon, lat)), r(tip)]] + [[r(a), r(b)] for a, b in wings]


def flourish_shift(county_now: pd.DataFrame, baseline_path: Path, meta: dict, base_url: str):
    """Change in Moore's (Democratic) MARGIN by county, plus arrow geometry for Flourish."""
    base = pd.read_csv(baseline_path, dtype={"fips": str})
    year_prev = year_of(base["last_refreshed"].iloc[0] if "last_refreshed" in base else "", "Previous")
    year_now = year_of(base_url, "Now")

    def margins(df, suffix):
        d = party_columns(df, DEM_PARTY, "dem").merge(party_columns(df, REP_PARTY, "rep"), on="fips")
        d[f"margin_{suffix}"] = (d["dem_pct"] - d["rep_pct"]).round(2)
        return d.rename(columns={"dem_pct": f"dem_pct_{suffix}", "dem_governor": f"dem_governor_{suffix}",
                                 "rep_governor": f"rep_governor_{suffix}"})[
            ["fips", f"dem_governor_{suffix}", f"rep_governor_{suffix}", f"dem_pct_{suffix}", f"margin_{suffix}"]]

    df = COUNTIES.merge(margins(base, "prev"), on="fips").merge(margins(county_now, "now"), on="fips")
    df["margin_shift"] = (df["margin_now"] - df["margin_prev"]).round(2)
    df["share_shift"] = (df["dem_pct_now"] - df["dem_pct_prev"]).round(2)

    df["direction"] = df["margin_shift"].apply(
        lambda s: NO_RESULTS_TEXT if pd.isna(s) else ("Toward Democrats" if s > 0 else
                                                     ("Toward Republicans" if s < 0 else "No change")))
    df["prev_margin_display"] = df.apply(
        lambda r: f"{year_prev}: {margin_text(r['margin_prev'], r['dem_governor_prev'], r['rep_governor_prev'])}", axis=1)
    df["now_margin_display"] = df.apply(
        lambda r: f"{year_now}: {margin_text(r['margin_now'], r['dem_governor_now'], r['rep_governor_now'])}", axis=1)
    df["shift_display"] = df.apply(
        lambda r: NO_RESULTS_TEXT if pd.isna(r["margin_shift"]) else
        ("No change" if r["margin_shift"] == 0 else
         f"{abs(r['margin_shift']):.{PCT_DISPLAY_DECIMALS}f} pts toward "
         f"{r['dem_governor_now'] if r['margin_shift'] > 0 else r['rep_governor_now']}"), axis=1)
    df["shift_color"] = df["margin_shift"].apply(lambda x: margin_color(x, DEM_COLOR, REP_COLOR))

    lines = [arrow_lines(r["lon"], r["lat"], r["margin_shift"]) for _, r in df.iterrows()]
    df["arrow_end_lon"] = [l[0][1][0] if l else None for l in lines]
    df["arrow_end_lat"] = [l[0][1][1] if l else None for l in lines]
    df["geometry"] = [json.dumps({"type": "MultiLineString", "coordinates": l}) if l else "" for l in lines]

    cols = ["fips", "county", "county_full", "lat", "lon",
            "dem_governor_now", "rep_governor_now", "dem_pct_prev", "dem_pct_now",
            "margin_prev", "margin_now", "margin_shift", "share_shift", "direction",
            "prev_margin_display", "now_margin_display", "shift_display", "shift_color",
            "arrow_end_lat", "arrow_end_lon", "geometry"]
    table = add_meta(df[cols], meta)

    feats = [{"type": "Feature", "id": r["fips"],
              "properties": {k: (None if pd.isna(r[k]) else r[k]) for k in
                             ["fips", "county_full", "margin_shift", "direction", "shift_display", "shift_color",
                              "prev_margin_display", "now_margin_display"]},
              "geometry": {"type": "MultiLineString", "coordinates": l}}
             for (_, r), l in zip(df.iterrows(), lines) if l]
    geojson = json.dumps({"type": "FeatureCollection", "features": feats})
    return table, geojson


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
        shift_table, shift_geojson = flourish_shift(gov_county, Path(args.baseline), gov_meta, args.base_url)
        out["flourish/flourish_moore_shift.csv"] = shift_table
        out["flourish/flourish_moore_shift_arrows.geojson"] = shift_geojson

    if found["districts"]:
        out["master/house_districts.csv"] = build_house(fetch, found["districts"])

    for q in want_q:
        q_state, q_county, q_meta = build_question(fetch, q, found["questions"][q])
        out[f"master/q{q}_statewide.csv"] = q_state
        out[f"master/q{q}_county.csv"] = q_county
        qbar, qmap = flourish_question_files(q, q_state, q_county, q_meta)
        out[f"flourish/flourish_q{q}_bar.csv"] = qbar
        out[f"flourish/flourish_q{q}_map.csv"] = qmap

    out[SOURCE_FILE] = args.base_url + "\n"
    return out


SOURCE_FILE = "_source.txt"   # records which SBE address the files in this folder came from


def check_against_previous(out: dict, out_dir: Path):
    """
    Refuse to publish if a race suddenly has fewer candidates/rows than last time.
    Skipped when the source address changed (e.g. switching from the 2022 test
    data to 2026), since a different election can legitimately have fewer rows.
    """
    src = out_dir / SOURCE_FILE
    if src.exists() and src.read_text().strip() != out[SOURCE_FILE].strip():
        print(f"Source changed ({src.read_text().strip()} -> {out[SOURCE_FILE].strip()}); skipping row-count check.")
        return
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
            if isinstance(df, str):
                p.write_text(df, encoding="utf-8")
            else:
                df.to_csv(p, index=False)
        for rel in out:
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp / rel), dest)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--out", default="data/live",
                    help="output folder; Flourish links point here, so keep it the same all night")
    ap.add_argument("--questions", default=DEFAULT_QUESTIONS, help="comma-separated, e.g. 1,2,3")
    ap.add_argument("--districts", type=int, default=DEFAULT_DISTRICTS, help="expected number of House districts")
    default_baseline = HERE / "reference" / "gov_2022_county.csv"   # Moore 2022, from SBE's official county page
    ap.add_argument("--baseline", default=str(default_baseline) if default_baseline.exists() else None,
                    help="2022 county results for the shift map (default: reference/gov_2022_county.csv); pass '' to skip")
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
        size = f"{len(df)} rows" if isinstance(df, pd.DataFrame) else "text"
        print(f"  wrote {Path(args.out) / rel}  ({size})")


if __name__ == "__main__":
    main()