#!/usr/bin/env python3
"""
update_data.py — månedlig opdatering af df-data repo'et.

Henter ny måneds data fra Energinet (sysapp.dk-proxy) og DMI, fletter ind i
eksisterende årsfiler, og opdaterer DATA_VERSION.md.

Kørselseksempler:
    # Almindelig månedlig opdatering — finder selv sidste dato pr. dataset
    # og henter alt fra det punkt frem til "i går".
    python scripts/update_data.py

    # Initial fyldning af 3 års historik
    python scripts/update_data.py --start 2023-01-01 --end 2026-04-30

    # Tving genhentning af specifik periode (overskriver eksisterende rækker)
    python scripts/update_data.py --start 2026-01-01 --end 2026-01-31 --force

Konfiguration nedenfor — udvid PRICE_ZONES og DMI_AREAS efter behov.
"""

from __future__ import annotations
import argparse
import sys
import urllib.parse
import urllib.request
import urllib.error
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd


# ============================================================================
# KONFIGURATION
# ============================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_URL_PROXY = "https://www.sysapp.dk"            # samme proxy som modellen bruger
BASE_URL_EDS   = "https://api.energidataservice.dk/dataset"

# Hvilke priszoner og DMI-områder skal hentes
PRICE_ZONES = ["DK1", "DK2"]
AFRR_ZONES  = ["DK1"]                                # DK2 har endnu ikke aFRR-marked
MFRR_ZONES  = ["DK1", "DK2"]
DMI_AREAS   = ["fyn","vestkyst"]                                # tilføj fx 'jylland_syd', 'sjaelland'

# Timeout og retry pr. API-kald
TIMEOUT_SEC = 120
RETRY_COUNT = 3
RETRY_SLEEP = 5

USER_AGENT = "df-data-updater/1.0 (steenkj/df-data)"


# ============================================================================
# HJÆLPEFUNKTIONER
# ============================================================================

def http_get_json(url: str) -> dict | list:
    """GET med timeout og retry, returnerer parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            if attempt == RETRY_COUNT:
                raise
            print(f"    Forsøg {attempt} fejlede ({e}); venter {RETRY_SLEEP}s og prøver igen…")
            time.sleep(RETRY_SLEEP)


def fetch_eds(endpoint: str, start: str, end: str, zone: str | None = None) -> pd.DataFrame:
    """Henter et helt datasæt fra Energi Data Service (paginerer til alt er hentet)."""
    rows: list[dict] = []
    offset = 0
    limit = 5000
    filt = json.dumps({"PriceArea": [zone]}) if zone else None
    while True:
        params = {"start": start, "end": end, "offset": offset, "limit": limit}
        if filt:
            params["filter"] = filt
        url = f"{BASE_URL_EDS}/{endpoint}?" + urllib.parse.urlencode(params)
        data = http_get_json(url)
        records = data.get("records", []) if isinstance(data, dict) else []
        if not records:
            break
        rows.extend(records)
        if len(records) < limit:
            break
        offset += limit
    return pd.DataFrame(rows)


def fetch_dmi(area: str, start: str, end: str) -> pd.DataFrame:
    """Henter timelige DMI-observationer for et område."""
    rows: list[dict] = []
    offset = 0
    limit = 10000
    while True:
        params = {
            "shortname": "all",     # alle vejrvariable
            "startdate": start,
            "enddate": end,
            "area": area,
            "limit": limit,
            "offset": offset,
            "format": "json",
        }
        url = f"{BASE_URL_PROXY}/api_dmi_obs.php?" + urllib.parse.urlencode(params)
        resp = http_get_json(url)
        records = resp.get("data", []) if isinstance(resp, dict) else []
        if not records:
            break
        rows.extend(records)
        if len(records) < limit:
            break
        offset += limit
    return pd.DataFrame(rows)

def merge_into_yearfile(new_df: pd.DataFrame, dest: Path, time_col: str, force: bool = False) -> None:
    """Fletter `new_df` ind i `dest`, dedupliker på time_col + ev. PriceArea."""
    if new_df.empty:
        print(f"    {dest.name}: ingen nye data")
        return
    new_df[time_col] = pd.to_datetime(new_df[time_col], errors="coerce")
    new_df = new_df.dropna(subset=[time_col])
    
    if dest.exists() and not force:
        old = pd.read_csv(dest)
        old[time_col] = pd.to_datetime(old[time_col], errors="coerce")
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df
    
    dedup = [time_col]
    for c in ("PriceArea", "price_area"):
        if c in combined.columns:
            dedup.append(c)
            break
    combined = combined.sort_values(dedup).drop_duplicates(subset=dedup, keep="last")
    
    dest.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(dest, index=False)
    print(f"    {dest.name}: {len(combined):,} rækker (skrevet)")


def split_and_write(df: pd.DataFrame, time_col: str, zone_col: str | None,
                     out_dir: Path, prefix: str = "", force: bool = False) -> None:
    """Splitter df pr. (zone, år) og fletter ind i årsfilerne."""
    if df.empty:
        return
    df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    df = df.dropna(subset=[time_col]).copy()
    df["_year"] = df[time_col].dt.year
    
    if zone_col is None:
        for year, part in df.groupby("_year"):
            part = part.drop(columns=["_year"])
            merge_into_yearfile(part, out_dir / f"{prefix}{int(year)}.csv", time_col, force)
    else:
        for (zone, year), part in df.groupby([zone_col, "_year"]):
            if pd.isna(zone) or pd.isna(year):
                continue
            part = part.drop(columns=["_year"])
            merge_into_yearfile(part, out_dir / f"{zone}_{int(year)}.csv", time_col, force)


def find_last_date(folder: Path, time_col: str) -> date | None:
    """Returnerer seneste dato i en mappes filer, eller None hvis tom."""
    latest = None
    for f in folder.glob("*.csv"):
        try:
            df = pd.read_csv(f, usecols=[time_col])
            df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
            m = df[time_col].max()
            if pd.notna(m) and (latest is None or m > latest):
                latest = m
        except Exception:
            continue
    return latest.date() if latest is not None else None


# ============================================================================
# DATASÆT-HENTERE
# ============================================================================

def update_spot(start: str, end: str, force: bool):
    print("  spot (DayAheadPrices):")
    df = fetch_eds("DayAheadPrices", start, end)
    if df.empty:
        print("    ingen data returneret")
        return
    # Normaliser kolonnenavne så de matcher den eksisterende cache-struktur.
    # DayAheadPrices afløser Elspotprices fra april 2026 sammenfaldende med
    # ISP15-fuld-overgangen. Skemaforskelle ift. det gamle endpoint:
    #   TimeUTC           ← var HourUTC          (nu altid 15-min opløsning)
    #   TimeDK            ← var HourDK
    #   PriceArea         (uændret)
    #   DayAheadPriceDKK  ← var SpotPriceDKK
    #   DayAheadPriceEUR  ← var SpotPriceEUR
    # CSV-kolonnerne i repo'et bevares som 'hour_utc'/'hour_dk'/'spot_price_dkk'
    # for bagudkompatibilitet med data_loader.py og data_loader_github.py.
    # Navnet 'hour_utc' er nu strengt taget misvisende (15-min, ikke time),
    # men det er tidsstemplet i kolonnen der bestemmer adressering, ikke navnet.
    rename = {"TimeUTC": "hour_utc", "TimeDK": "hour_dk", "PriceArea": "price_area",
              "DayAheadPriceDKK": "spot_price_dkk", "DayAheadPriceEUR": "spot_price_eur"}
    df = df.rename(columns=rename)
    split_and_write(df, "hour_utc", "price_area", REPO_ROOT / "spot", force=force)


def update_afrr(start: str, end: str, force: bool):
    print("  afrr (AfrrReservesNordic):")
    for zone in AFRR_ZONES:
        df = fetch_eds("AfrrReservesNordic", start, end, zone=zone)
        split_and_write(df, "TimeUTC", "PriceArea", REPO_ROOT / "afrr", force=force)


def update_mfrr_cap(start: str, end: str, force: bool):
    print("  mfrr_cap (MfrrCapacityMarket):")
    for zone in MFRR_ZONES:
        df = fetch_eds("MfrrCapacityMarket", start, end, zone=zone)
        split_and_write(df, "TimeUTC", "PriceArea", REPO_ROOT / "mfrr_cap", force=force)


def update_mfrr_act(start: str, end: str, force: bool):
    print("  mfrr_act (MfrrEnergyActivationMarket):")
    for zone in MFRR_ZONES:
        df = fetch_eds("MfrrEnergyActivationMarket", start, end, zone=zone)
        split_and_write(df, "TimeUTC", "PriceArea", REPO_ROOT / "mfrr_act", force=force)


def update_imbalance(start: str, end: str, force: bool):
    print("  imbalance (ImbalancePrice):")
    for zone in MFRR_ZONES:
        df = fetch_eds("ImbalancePrice", start, end, zone=zone)
        split_and_write(df, "TimeUTC", "PriceArea", REPO_ROOT / "imbalance", force=force)


def update_dmi(start: str, end: str, force: bool):
    print("  dmi:")
    for area in DMI_AREAS:
        df = fetch_dmi(area, start, end)
        split_and_write(df, "hour_utc", None, REPO_ROOT / "dmi",
                        prefix=f"{area}_", force=force)


# ============================================================================
# DATA_VERSION.md
# ============================================================================

def update_version_file():
    today = date.today().isoformat()
    lines = [
        "# DATA_VERSION",
        "",
        "Dette dokument viser den aktuelle datadækning i repo'et. Opdateres af `scripts/update_data.py` ved hver kørsel.",
        "",
        "## Seneste opdatering",
        "",
        f"**{today}** — automatisk opdatering",
        "",
        "## Dækning pr. dataset",
        "",
        "| Dataset | Område | Tidligst | Seneste | Antal rækker |",
        "|---|---|---|---|---|",
    ]
    
    folders_and_timecols = [
        ("spot", "hour_utc"),
        ("afrr", "TimeUTC"),
        ("mfrr_cap", "TimeUTC"),
        ("mfrr_act", "TimeUTC"),
        ("imbalance", "TimeUTC"),
        ("dmi", "hour_utc"),
    ]
    for folder, tc in folders_and_timecols:
        path = REPO_ROOT / folder
        if not path.exists():
            continue
        for f in sorted(path.glob("*.csv")):
            stem = f.stem  # fx 'DK1_2025' eller 'fyn_2025'
            try:
                area, year = stem.rsplit("_", 1)
            except ValueError:
                continue
            try:
                df = pd.read_csv(f, usecols=[tc])
                df[tc] = pd.to_datetime(df[tc], errors="coerce")
                lines.append(f"| {folder} | {area} | {df[tc].min()} | {df[tc].max()} | {len(df):,} |")
            except Exception:
                continue
    
    (REPO_ROOT / "DATA_VERSION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  DATA_VERSION.md opdateret")


# ============================================================================
# MAIN
# ============================================================================

def determine_start(args) -> str:
    if args.start:
        return args.start
    # auto: brug tidligste "seneste dato" på tværs af spot/dmi som udgangspunkt + 1 dag
    candidates = []
    for folder, tc in [("spot", "hour_utc"), ("dmi", "hour_utc")]:
        d = find_last_date(REPO_ROOT / folder, tc)
        if d:
            candidates.append(d)
    if candidates:
        return (min(candidates) + timedelta(days=1)).isoformat()
    # fallback: 3 år tilbage
    return (date.today() - timedelta(days=3*365)).isoformat()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", help="YYYY-MM-DD. Hvis ikke angivet, fortsætter fra seneste data.")
    p.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat(),
                   help="YYYY-MM-DD (default: i går)")
    p.add_argument("--force", action="store_true", help="Overskriv eksisterende rækker i målperiode")
    p.add_argument("--skip", default="", help="Komma-sepereret liste af datasæt at springe over")
    args = p.parse_args()
    
    start = determine_start(args)
    end = args.end
    skip = set(args.skip.split(",")) if args.skip else set()
    
    print(f"=== df-data update ===")
    print(f"Periode: {start} → {end}")
    if skip:
        print(f"Springer over: {sorted(skip)}")
    print()
    
    if "spot" not in skip:       update_spot(start, end, args.force)
    if "afrr" not in skip:       update_afrr(start, end, args.force)
    if "mfrr_cap" not in skip:   update_mfrr_cap(start, end, args.force)
    if "mfrr_act" not in skip:   update_mfrr_act(start, end, args.force)
    if "imbalance" not in skip:  update_imbalance(start, end, args.force)
    if "dmi" not in skip:        update_dmi(start, end, args.force)
    
    print()
    update_version_file()
    print("Færdig.")


if __name__ == "__main__":
    main()
