#!/usr/bin/env python3
"""
update_data.py — månedlig opdatering af df-data repo'et.

Henter data fra enten sysapp-API'et (default) eller Energi Data Service
direkte, fletter ind i eksisterende årsfiler, og opdaterer DATA_VERSION.md.

CSV-KONTRAKTEN ER FROSSET. Uanset kilde skrives de samme kolonnenavne som
hidtil: balance-datasættene bruger EDS' PascalCase (TimeUTC, mFRRSAUpReqMW …),
spot og dmi bruger snake_case (hour_utc, spot_price_dkk …). sysapp leverer
snake_case for alt, så der omdøbes på vej ud. Det er bevidst: hvis kilde og
format skiftede samtidig, kunne man ikke skelne en proxy-fejl fra en
omdøbnings-fejl. Med frosset kontrakt skal diffen mellem gammel og ny CSV for
en overlappende periode være tom — det er beviset for at kildeskiftet er
neutralt. Se scripts/compare_sources.py.

Kørselseksempler:
    # Almindelig månedlig opdatering fra sysapp
    python scripts/update_data.py

    # Samme periode hentet fra EDS i stedet (sandhedsvidne)
    python scripts/update_data.py --source eds --start 2026-06-01 --end 2026-06-30

    # Initial fyldning
    python scripts/update_data.py --start 2023-01-01 --end 2026-04-30

    # Tving genhentning af specifik periode (overskriver eksisterende rækker)
    python scripts/update_data.py --start 2026-01-01 --end 2026-01-31 --force
"""

from __future__ import annotations
import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd


# ============================================================================
# KONFIGURATION
# ============================================================================

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_URL_SYSAPP = "https://api.sysapp.dk"
BASE_URL_EDS    = "https://api.energidataservice.dk/dataset"

PRICE_ZONES = ["DK1", "DK2"]
AFRR_ZONES  = ["DK1"]                        # DK2 har endnu ikke aFRR-marked
MFRR_ZONES  = ["DK1", "DK2"]
DMI_AREAS   = ["fyn", "vestkyst", "karup"]

# sysapp's api_energinet_prices.php kender kun DK1/DK2. De øvrige zoner i
# spot/ (DE, NO2, SE3, SE4, SYSTEM) stammer fra EDS' gamle Elspotprices og
# er allerede dokumenteret som ikke-vedligeholdte. De opdateres ikke ved
# --source sysapp. Vil du have dem igen, kræver det api_entsoe_prices.php
# (kun EUR, andet skema) — selvstændigt stykke arbejde.

TIMEOUT_SEC = 120
RETRY_COUNT = 3
RETRY_SLEEP = 5
PAGE_LIMIT  = 10000          # sysapp's loft; EDS bruger 5000
MAX_PAGES   = 500            # sikkerhedsstop mod uendelig paginering

USER_AGENT = "df-data-updater/2.0 (skj-1964/df-data)"


# ============================================================================
# OMDØBNING: sysapp snake_case  ->  frossen CSV-kontrakt
# ============================================================================

# spot: sysapp's api_energinet_prices.php leverer allerede præcis de
# kolonnenavne der står i spot/*.csv (id, hour_utc, hour_dk, price_area,
# spot_price_dkk, spot_price_eur, created_at, updated_at). Ingen omdøbning.
RENAME_SPOT_SYSAPP: dict[str, str] = {}

# EDS DayAheadPrices -> samme kontrakt. (Afløste Elspotprices ved ISP15.)
RENAME_SPOT_EDS = {
    "TimeUTC": "hour_utc", "TimeDK": "hour_dk", "PriceArea": "price_area",
    "DayAheadPriceDKK": "spot_price_dkk", "DayAheadPriceEUR": "spot_price_eur",
}

_CAP_COMMON = {
    "time_utc": "TimeUTC", "time_dk": "TimeDK", "price_area": "PriceArea",
    "up_demand_mw": "UpDemandMW", "up_procured_mw": "UpProcuredMW",
    "up_price_eur": "UpPriceEUR", "up_price_dkk": "UpPriceDKK",
    "down_demand_mw": "DownDemandMW", "down_procured_mw": "DownProcuredMW",
    "down_price_eur": "DownPriceEUR", "down_price_dkk": "DownPriceDKK",
}
RENAME_AFRR_CAP = dict(_CAP_COMMON)
RENAME_MFRR_CAP = dict(_CAP_COMMON)

RENAME_MFRR_ACT = {
    "time_utc": "TimeUTC", "time_dk": "TimeDK", "price_area": "PriceArea",
    "mfrr_sa_up_req_mw": "mFRRSAUpReqMW", "mfrr_sa_up_eur": "mFRRSAUpEUR",
    "mfrr_sa_down_req_mw": "mFRRSADownReqMW", "mfrr_sa_down_eur": "mFRRSADownEUR",
    "mfrr_da_up_mw": "mFRRDAUpMW", "mfrr_da_up_eur": "mFRRDAUpEUR",
    "mfrr_da_down_mw": "mFRRDADownMW", "mfrr_da_down_eur": "mFRRDADownEUR",
    "total_mfrr_up_mw": "TotalmFRRUpMW", "total_mfrr_down_mw": "TotalmFRRDownMW",
    "mfrr_offered_up_mw": "mFRROfferedUpMW", "mfrr_offered_down_mw": "mFRROfferedDownMW",
    "mfrr_local_up_mw": "mFRRLocalUpMW", "mfrr_local_down_mw": "mFRRLocalDownMW",
    "mfrr_special_up_mw": "mFRRSpecialUpMW", "mfrr_special_down_mw": "mFRRSpecialDownMW",
}

RENAME_IMBALANCE = {
    "time_utc": "TimeUTC", "time_dk": "TimeDK", "price_area": "PriceArea",
    "satisfied_demand": "SatisfiedDemand",
    "imbalance_price_eur": "ImbalancePriceEUR", "imbalance_price_dkk": "ImbalancePriceDKK",
    "spot_price_eur": "SpotPriceEUR", "dominating_direction": "DominatingDirection",
    "afrr_up_mw": "aFRRUpMW", "afrr_vwa_up_eur": "aFRRVWAUpEUR",
    "afrr_vwa_up_dkk": "aFRRVWAUpDKK", "afrr_down_mw": "aFRRDownMW",
    "afrr_vwa_down_eur": "aFRRVWADownEUR", "afrr_vwa_down_dkk": "aFRRVWADownDKK",
    "mfrr_marginal_price_up_eur": "mFRRMarginalPriceUpEUR",
    "mfrr_marginal_price_up_dkk": "mFRRMarginalPriceUpDKK",
    "mfrr_marginal_price_down_eur": "mFRRMarginalPriceDownEUR",
    "mfrr_marginal_price_down_dkk": "mFRRMarginalPriceDownDKK",
}

# Kolonner sysapp leverer som ikke findes i den frosne CSV-kontrakt.
# NB: spot/*.csv HAR created_at og updated_at (de kom oprindeligt fra sysapp),
# mens balance-filerne stammer fra EDS og ikke har dem. Derfor er drop-listen
# datasæt-specifik og ikke global.
DROP_BALANCE = ["created_at", "updated_at", "auction"]
DROP_SPOT: list[str] = []

# Nøglekolonner der ikke må talkonverteres.
KEY_COLS = {"TimeUTC", "TimeDK", "PriceArea", "hour_utc", "hour_dk",
            "price_area", "area", "created_at", "updated_at"}


# ============================================================================
# HTTP
# ============================================================================

def http_get_json(url: str) -> dict | list:
    """GET med timeout og retry, returnerer parsed JSON."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 400 fra de strenge endpoints er en kontraktfejl, ikke et
            # transient problem — retry ville bare gentage fejlen. Læs
            # fejlbeskeden ud og stop.
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if e.code == 400:
                raise RuntimeError(f"400 fra {url}\n  {body}") from e
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        if attempt < RETRY_COUNT:
            print(f"    Forsøg {attempt} fejlede ({last}); venter {RETRY_SLEEP}s…")
            time.sleep(RETRY_SLEEP)
    raise last  # type: ignore[misc]


def fetch_sysapp(path: str, params: dict) -> pd.DataFrame:
    """
    Paginér et sysapp-endpoint til bunds.

    Paginering styres af meta.has_more / meta.next_offset. Det er sikrere end
    at gætte på 'færre rækker end limit betyder slut' — hvis et endpoint en dag
    klipper svaret af serverside, holder has_more stadig.
    """
    rows: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        q = dict(params, format="json", limit=PAGE_LIMIT, offset=offset)
        url = f"{BASE_URL_SYSAPP}/{path}?" + urllib.parse.urlencode(q)
        resp = http_get_json(url)
        if not isinstance(resp, dict):
            break
        if resp.get("status") == "error":
            raise RuntimeError(f"{path}: {resp.get('code')} — {resp.get('message')}")
        batch = resp.get("data") or []
        rows.extend(batch)
        meta = resp.get("meta") or {}
        if not meta.get("has_more"):
            break
        nxt = meta.get("next_offset")
        offset = nxt if isinstance(nxt, int) else offset + len(batch)
        if not batch:
            break
    else:
        print(f"    ADVARSEL: {path} nåede MAX_PAGES={MAX_PAGES}; data kan mangle")
    return pd.DataFrame(rows)


def fetch_eds(endpoint: str, start: str, end: str, zone: str | None = None) -> pd.DataFrame:
    """Henter et helt datasæt fra Energi Data Service (paginerer til alt er hentet)."""
    rows: list[dict] = []
    offset = 0
    limit = 5000
    filt = json.dumps({"PriceArea": [zone]}) if zone else None
    for _ in range(MAX_PAGES):
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


# ============================================================================
# NORMALISERING
# ============================================================================

def normalize(df: pd.DataFrame, rename: dict[str, str],
              drop: list[str] | None = None) -> pd.DataFrame:
    """
    Omdøb til den frosne CSV-kontrakt, drop kolonner der ikke hører hjemme,
    og tvangskonvertér tal.

    Talkonverteringen er ikke kosmetik. sysapp serialiserer DECIMAL-kolonner
    som JSON-strenge ("13.219953"). Skrives de uændret, får CSV'en samme
    tekst som før, men enhver senere sammenligning eller aggregering på tværs
    af gamle og nye rækker arbejder på blandede dtypes.
    """
    if df.empty:
        return df
    df = df.rename(columns=rename)
    if drop is None:
        drop = DROP_BALANCE
    df = df.drop(columns=[c for c in drop if c in df.columns], errors="ignore")
    for c in df.columns:
        if c in KEY_COLS:
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def reorder_like_existing(df: pd.DataFrame, dest: Path) -> pd.DataFrame:
    """
    Bring kolonnerækkefølgen på linje med den eksisterende årsfil, så en
    ren git-diff ikke drukner i omrokerede kolonner.
    """
    if df.empty or not dest.exists():
        return df
    try:
        cols = list(pd.read_csv(dest, nrows=0).columns)
    except Exception:
        return df
    known = [c for c in cols if c in df.columns]
    extra = [c for c in df.columns if c not in cols]
    if extra:
        print(f"    NOTE: {dest.name} får nye kolonner: {extra}")
    return df[known + extra]


# ============================================================================
# SKRIVNING
# ============================================================================

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _check_key_format(old_s: pd.DataFrame, time_col: str, dest: Path) -> None:
    """
    Advar hvis den eksisterende fils noeglekolonne ikke er paa kanonisk form.

    Sker det, matcher nye raekker ikke gamle ved dedup, og filen vokser med
    dubletter i stedet for at blive opdateret. Bedre at raabe op end at
    producere en fil der ser rigtig ud.
    """
    if old_s.empty or time_col not in old_s.columns:
        return
    s = old_s[time_col].astype(str)
    parsed = pd.to_datetime(s, errors="coerce")
    ok = parsed.notna() & (parsed.dt.strftime(TIME_FMT) == s)
    if not ok.all():
        bad = s[~ok]
        print(f"    ADVARSEL: {dest.name} har {len(bad):,} tidsstempler i "
              f"{time_col} paa ikke-kanonisk form (fx {bad.iloc[0]!r}). "
              f"Dedup vil ikke matche dem — koer med --force for perioden.")


def _as_written_strings(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returnér df som rene strenge, formateret præcis som to_csv ville skrive dem.

    Omvejen over en CSV-buffer er ikke elegant, men den garanterer at
    strengformen er den samme pandas selv ville producere — i stedet for at
    genopfinde float-repr og NaN-håndtering og komme til at ramme lidt ved
    siden af.
    """
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    buf.seek(0)
    return pd.read_csv(buf, dtype=str, keep_default_na=False)


def merge_into_yearfile(new_df: pd.DataFrame, dest: Path, time_col: str,
                        force: bool = False) -> None:
    """
    Fletter `new_df` ind i `dest`, dedupliker på time_col + ev. PriceArea.

    Fletningen sker på STRENGE, ikke på parsede værdier. Grunden er at en
    almindelig concat lader den nye batch bestemme dtype for hele kolonnen:
    ét NaN i fx mFRROfferedUpMW flipper kolonnen til float, og alle 17.000
    historiske rækker omskrives fra '320' til '320.0'. Data er de samme, men
    git-diffen bliver ulæselig — og diffen er den eneste reviewflade der er,
    før månedens opdatering pushes. Med streng-fletning røres gamle rækker
    aldrig; kun tilføjede og faktisk ændrede linjer optræder i diffen.
    """
    if new_df.empty:
        print(f"    {dest.name}: ingen nye data")
        return
    new_df = new_df.copy()
    parsed = pd.to_datetime(new_df[time_col], errors="coerce")
    new_df = new_df[parsed.notna()]
    if new_df.empty:
        print(f"    {dest.name}: ingen brugbare tidsstempler")
        return
    new_df = reorder_like_existing(new_df, dest)
    new_s = _as_written_strings(new_df)

    # Dedup sker paa strenge, saa noeglekolonnen skal have kanonisk format.
    # Uden det ville sysapps '2026-06-01T00:00:00' og filens
    # '2026-06-01 00:00:00' vaere to forskellige noegler, og en genhentning
    # ville tilfoeje dubletter i stedet for at opdatere — uden fejlmeddelelse.
    # Kun noeglekolonnen normaliseres; TimeDK og oevrige tidsfelter er ikke
    # noegler og faar lov at beholde kildens format.
    canon = pd.to_datetime(new_s[time_col], errors="coerce")
    if canon.isna().any():
        raise ValueError(
            f"{dest.name}: {int(canon.isna().sum())} tidsstempler i {time_col} "
            f"kunne ikke parses; foerste: {new_s[time_col][canon.isna()].iloc[0]!r}")
    new_s[time_col] = canon.dt.strftime(TIME_FMT)

    if dest.exists() and not force:
        old_s = pd.read_csv(dest, dtype=str, keep_default_na=False)
        _check_key_format(old_s, time_col, dest)
        for c in old_s.columns:
            if c not in new_s.columns:
                new_s[c] = ""
        for c in new_s.columns:
            if c not in old_s.columns:
                old_s[c] = ""
        new_s = new_s[old_s.columns]
        combined = pd.concat([old_s, new_s], ignore_index=True)
    else:
        combined = new_s

    dedup = [time_col]
    for c in ("PriceArea", "price_area"):
        if c in combined.columns:
            dedup.append(c)
            break
    # Tidsstemplerne er ISO (YYYY-MM-DD HH:MM:SS), så leksikografisk sortering
    # er kronologisk. kind='stable' bevarer rækkefølgen indenfor samme nøgle,
    # så keep='last' konsekvent betyder "den nyest hentede".
    combined = combined.sort_values(dedup, kind="stable")
    combined = combined.drop_duplicates(subset=dedup, keep="last")

    dest.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(dest, index=False)
    print(f"    {dest.name}: {len(combined):,} rækker (skrevet)")


def split_and_write(df: pd.DataFrame, time_col: str, zone_col: str | None,
                    out_dir: Path, prefix: str = "", force: bool = False) -> None:
    """Splitter df pr. (zone, år) og fletter ind i årsfilerne."""
    if df.empty:
        return
    df = df.copy()
    df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    df = df.dropna(subset=[time_col])
    df["_year"] = df[time_col].dt.year

    if zone_col is None:
        for year, part in df.groupby("_year"):
            merge_into_yearfile(part.drop(columns=["_year"]),
                                out_dir / f"{prefix}{int(year)}.csv", time_col, force)
    else:
        for (zone, year), part in df.groupby([zone_col, "_year"]):
            if pd.isna(zone) or pd.isna(year):
                continue
            merge_into_yearfile(part.drop(columns=["_year"]),
                                out_dir / f"{zone}_{int(year)}.csv", time_col, force)


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


def exclusive_end(end: str) -> str:
    """
    api_eds_balance.php har halvåbent interval [startdate, enddate).
    Alle øvrige endpoints — sysapps Energinet/DMI såvel som EDS — er
    inklusive. Den forskel er den mest oplagte kilde til at miste stille og
    roligt den sidste dag i hver månedskørsel, så den ligger eksplicit her
    frem for at være gemt i et kald.
    """
    return (date.fromisoformat(end) + timedelta(days=1)).isoformat()


# ============================================================================
# DATASÆT-HENTERE
# ============================================================================

def update_spot(start: str, end: str, force: bool, source: str):
    print(f"  spot ({source}):")
    if source == "sysapp":
        frames = []
        for zone in PRICE_ZONES:
            # tz=utc så filtreringsaksen matcher hour_utc, som er den akse
            # årsfilerne er splittet og dedupliceret på.
            df = fetch_sysapp("api_energinet_prices.php", {
                "startdate": start, "enddate": end, "area": zone,
                "tz": "utc", "fields": "all",
            })
            if not df.empty:
                frames.append(normalize(df, RENAME_SPOT_SYSAPP, DROP_SPOT))
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    else:
        df = normalize(fetch_eds("DayAheadPrices", start, end), RENAME_SPOT_EDS, DROP_SPOT)
    if df.empty:
        print("    ingen data returneret")
        return
    split_and_write(df, "hour_utc", "price_area", REPO_ROOT / "spot", force=force)


def _update_balance(name: str, sysapp_dataset: str, eds_endpoint: str,
                    rename: dict, zones: list[str], out: str,
                    start: str, end: str, force: bool, source: str,
                    extra_params: dict | None = None):
    print(f"  {name} ({source}):")
    for zone in zones:
        if source == "sysapp":
            params = {
                "dataset": sysapp_dataset,
                "startdate": start,
                "enddate": exclusive_end(end),   # halvåbent interval
                "area": zone,
            }
            # api_eds_balance.php afviser ukendte parametre med 400 i stedet
            # for at ignorere dem. 'auction' må kun sendes til mfrr_capacity.
            if extra_params:
                params.update(extra_params)
            df = normalize(fetch_sysapp("api_eds_balance.php", params), rename)
        else:
            df = fetch_eds(eds_endpoint, start, end, zone=zone)
        split_and_write(df, "TimeUTC", "PriceArea", REPO_ROOT / out, force=force)


def update_afrr(start, end, force, source):
    _update_balance("afrr", "afrr_capacity", "AfrrReservesNordic",
                    RENAME_AFRR_CAP, AFRR_ZONES, "afrr", start, end, force, source)


def update_mfrr_cap(start, end, force, source):
    # auction=main: den frosne CSV-kontrakt har ingen auction-kolonne, så
    # rækkerne skal være entydige på (TimeUTC, PriceArea). Pr. august 2026
    # findes kun 'main' i data. Dukker 'extra' op, skal dette valg tages om
    # — og CSV-kontrakten udvides med en tredje nøglekolonne.
    _update_balance("mfrr_cap", "mfrr_capacity", "MfrrCapacityMarket",
                    RENAME_MFRR_CAP, MFRR_ZONES, "mfrr_cap", start, end, force,
                    source, extra_params={"auction": "main"} if source == "sysapp" else None)


def update_mfrr_act(start, end, force, source):
    _update_balance("mfrr_act", "mfrr_activation", "MfrrEnergyActivationMarket",
                    RENAME_MFRR_ACT, MFRR_ZONES, "mfrr_act", start, end, force, source)


def update_imbalance(start, end, force, source):
    _update_balance("imbalance", "imbalance_price", "ImbalancePrice",
                    RENAME_IMBALANCE, MFRR_ZONES, "imbalance", start, end, force, source)


def update_dmi(start: str, end: str, force: bool, source: str):
    """
    DMI kommer altid fra sysapp — der findes ingen EDS-vej.

    NB: tidligere sendte scriptet 'shortname=all'. Den parameter findes ikke i
    api_dmi_obs_ny.php's kontrakt (den hedder 'fields'), og endpointet
    ignorerer ukendte parametre tavst, så kaldet har virket uden at gøre noget.
    Det forklarer formentlig hvorfor wind_dir_past1h og temp_dew mangler i
    dmi/*.csv. Adfærden er bevaret uændret her — at sætte fields=all ville
    tilføje to kolonner midt i historikken og er en selvstændig beslutning.
    """
    print("  dmi (sysapp):")
    for area in DMI_AREAS:
        df = fetch_sysapp("api_dmi_obs_ny.php", {
            "startdate": start, "enddate": end, "area": area, "tz": "utc",
        })
        split_and_write(df, "hour_utc", None, REPO_ROOT / "dmi",
                        prefix=f"{area}_", force=force)


# ============================================================================
# DATA_VERSION.md
# ============================================================================

def update_version_file(source: str):
    today = date.today().isoformat()
    lines = [
        "# DATA_VERSION",
        "",
        "Dette dokument viser den aktuelle datadækning i repo'et. "
        "Opdateres af `scripts/update_data.py` ved hver kørsel.",
        "",
        "## Seneste opdatering",
        "",
        f"**{today}** — automatisk opdatering (kilde: `{source}`)",
        "",
        "## Dækning pr. dataset",
        "",
        "| Dataset | Område | Tidligst | Seneste | Antal rækker |",
        "|---|---|---|---|---|",
    ]
    for folder, tc in [("spot", "hour_utc"), ("afrr", "TimeUTC"),
                       ("mfrr_cap", "TimeUTC"), ("mfrr_act", "TimeUTC"),
                       ("imbalance", "TimeUTC"), ("dmi", "hour_utc")]:
        path = REPO_ROOT / folder
        if not path.exists():
            continue
        for f in sorted(path.glob("*.csv")):
            try:
                area, _year = f.stem.rsplit("_", 1)
            except ValueError:
                continue
            try:
                df = pd.read_csv(f, usecols=[tc])
                df[tc] = pd.to_datetime(df[tc], errors="coerce")
                lines.append(
                    f"| {folder} | {area} | {df[tc].min()} | {df[tc].max()} | {len(df):,} |")
            except Exception:
                continue
    (REPO_ROOT / "DATA_VERSION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  DATA_VERSION.md opdateret")


# ============================================================================
# MAIN
# ============================================================================

def determine_start(args) -> str:
    if args.start:
        return args.start
    candidates = []
    for folder, tc in [("spot", "hour_utc"), ("dmi", "hour_utc")]:
        d = find_last_date(REPO_ROOT / folder, tc)
        if d:
            candidates.append(d)
    if candidates:
        return (min(candidates) + timedelta(days=1)).isoformat()
    return (date.today() - timedelta(days=3 * 365)).isoformat()


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", choices=["sysapp", "eds"], default="sysapp",
                   help="Datakilde for spot og balance. DMI kommer altid fra sysapp.")
    p.add_argument("--start", help="YYYY-MM-DD. Hvis udeladt, fortsættes fra seneste data.")
    p.add_argument("--end", default=(date.today() - timedelta(days=1)).isoformat(),
                   help="YYYY-MM-DD, inklusiv (default: i går)")
    p.add_argument("--force", action="store_true",
                   help="Overskriv eksisterende rækker i målperioden")
    p.add_argument("--skip", default="",
                   help="Komma-separeret liste af datasæt at springe over")
    args = p.parse_args()

    start = determine_start(args)
    end = args.end
    skip = set(s.strip() for s in args.skip.split(",") if s.strip())

    print("=== df-data update ===")
    print(f"Kilde:   {args.source}")
    print(f"Periode: {start} → {end} (inklusiv)")
    if skip:
        print(f"Springer over: {sorted(skip)}")
    print()

    if "spot" not in skip:      update_spot(start, end, args.force, args.source)
    if "afrr" not in skip:      update_afrr(start, end, args.force, args.source)
    if "mfrr_cap" not in skip:  update_mfrr_cap(start, end, args.force, args.source)
    if "mfrr_act" not in skip:  update_mfrr_act(start, end, args.force, args.source)
    if "imbalance" not in skip: update_imbalance(start, end, args.force, args.source)
    if "dmi" not in skip:       update_dmi(start, end, args.force, args.source)

    print()
    update_version_file(args.source)
    print("Færdig.")


if __name__ == "__main__":
    main()
