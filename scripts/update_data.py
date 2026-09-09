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
import re
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
# EDS-kvoter, malt af api-projektet 2026-09-07. Overskrides de, svarer EDS
# 429 — og en retry-loop uden pause gor det vaerre, ikke bedre.
#   Elspotprices:    1 kald pr. 299,9 s  -> 330 s pause
#   DayAheadPrices:  3 kald pr. ~2 s     -> 20 s pause
# Balance-datasaettenes kvoter er ikke malt; de far DayAhead-satsen, som er
# den forsigtige af de to der er kendt.
EDS_PAUSE_SEC = {"Elspotprices": 330.0}
EDS_PAUSE_DEFAULT = 20.0
_eds_last_call: dict[str, float] = {}
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


def _eds_throttle(endpoint: str) -> None:
    """Hold EDS-kvoten pr. endpoint. Forste kald venter ikke."""
    pause = EDS_PAUSE_SEC.get(endpoint, EDS_PAUSE_DEFAULT)
    prev = _eds_last_call.get(endpoint)
    if prev is not None:
        wait = pause - (time.monotonic() - prev)
        if wait > 0:
            print(f"    EDS-kvote: venter {wait:.0f}s for {endpoint}")
            time.sleep(wait)
    _eds_last_call[endpoint] = time.monotonic()


def fetch_eds(endpoint: str, start: str, end: str, zone: str | None = None) -> pd.DataFrame:
    """
    Henter fra Energi Data Service for UTC-døgnene [start, end] (end inklusiv)
    og klipper til præcis det vindue.

    EDS filtrerer på dansk lokaltid med halvåbent interval, så et bart `end=D`
    betyder `D T00:00` DANSK tid — to timer før UTC-døgnets slut om sommeren,
    én om vinteren. Sendes `end` uændret, taber hver kørsel stille den sidste
    dag (og `start == end` giver nul rækker). Derfor hentes et superset og
    klippes ned bagefter, så resultatet er sammenligneligt med sysapps
    UTC-vindue række for række. Se datosemantik-blokken nedenfor.
    """
    # +2 dage: DK-midnat på end+2 ligger 22:00 UTC på end+1, altså sikkert
    # efter UTC-vinduets slut i både sommer- og vintertid. +1 ville ikke være
    # nok — den lander 22:00 UTC på end, to timer for tidligt.
    req_end = (date.fromisoformat(end) + timedelta(days=2)).isoformat()
    rows: list[dict] = []
    offset = 0
    limit = 5000
    filt = json.dumps({"PriceArea": [zone]}) if zone else None
    for _ in range(MAX_PAGES):
        params = {"start": start, "end": req_end, "offset": offset, "limit": limit}
        if filt:
            params["filter"] = filt
        url = f"{BASE_URL_EDS}/{endpoint}?" + urllib.parse.urlencode(params)
        _eds_throttle(endpoint)
        data = http_get_json(url)
        records = data.get("records", []) if isinstance(data, dict) else []
        if not records:
            break
        rows.extend(records)
        if len(records) < limit:
            break
        offset += limit
    return clip_to_utc_window(pd.DataFrame(rows), start, end)


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


# --- Formatkonformitet -------------------------------------------------------
# normalize() bruger pd.to_numeric pr. kolonne, og den resulterende dtype
# afhaenger af batchens indhold: kun heltal -> int64 -> "331"; ét decimaltal
# eller én NaN -> float64 -> "331.0". Filens format kom altsaa til at afhaenge
# af hvad kilden tilfaeldigvis leverede i den enkelte hentning, og en
# genhentning af de samme raekker kunne skifte format uden at vaerdien aendrede
# sig. Under cron er der ingen der laeser diffen, saa den slags skal ikke kunne
# opstaa. Reglen her er den samme som i reorder_like_existing: aarsfilen er
# kontrakten, og nye raekker retter ind efter den.
#
# Kun to ting roeres — heltalsvaerdier og tidsstempler. Kolonner med aegte
# decimaler passerer urørt, saa 29.8984 og 27.730766 kan ligge i samme kolonne.

_INTEGRAL_RE = re.compile(r"^-?\d+(?:\.0+)?$")
_TS_RE       = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def _integral_suffix(col: pd.Series) -> str | None:
    """Filens skrivemaade for heltal: '' , '.0', '.00' — None hvis uklar."""
    v = col[col != ""]
    m = v[v.str.match(_INTEGRAL_RE, na=False)]
    if m.empty:
        return None
    suf = m.str.extract(r"(\.0+)$", expand=False).fillna("")
    uniq = suf.unique()
    return uniq[0] if len(uniq) == 1 else None


def _ts_separator(col: pd.Series) -> str | None:
    """Filens separator mellem dato og tid: 'T' eller ' ' — None hvis uklar."""
    v = col[col != ""]
    m = v[v.str.match(_TS_RE, na=False)]
    if m.empty:
        return None
    uniq = m.str[10].unique()
    return uniq[0] if len(uniq) == 1 else None


def conform_to_existing(new_s: pd.DataFrame, old_s: pd.DataFrame,
                        dest_name: str = "") -> pd.DataFrame:
    """Ret nye strengraekker ind efter aarsfilens egen skrivemaade."""
    new_s = new_s.copy()
    noter: list[str] = []
    for c in new_s.columns:
        if c not in old_s.columns:
            continue                      # ny kolonne: ingen konvention at foelge
        old_v, new_v = old_s[c].astype(str), new_s[c].astype(str)

        sep = _ts_separator(old_v)
        if sep is not None:
            mask = new_v.str.match(_TS_RE, na=False)
            if mask.any():
                afvig = mask & (new_v.str[10] != sep)
                if afvig.any():
                    new_s.loc[afvig, c] = (new_v[afvig].str[:10] + sep
                                           + new_v[afvig].str[11:])
                    noter.append(f"{c}: {int(afvig.sum())} tidsstempler -> '{sep}'")
            continue

        suf = _integral_suffix(old_v)
        if suf is None:
            continue                      # blandet eller ingen heltal: lad staa
        mask = new_v.str.match(_INTEGRAL_RE, na=False)
        if not mask.any():
            continue
        rettet = new_v[mask].str.replace(r"\.0+$", "", regex=True) + suf
        afvig = mask & (new_v != rettet.reindex(new_v.index))
        if afvig.any():
            new_s.loc[afvig, c] = rettet[afvig]
            noter.append(f"{c}: {int(afvig.sum())} heltal -> '{{n}}{suf}'")
    if noter:
        print(f"    {dest_name}: rettet ind efter filens format — "
              + "; ".join(noter[:4]) + ("…" if len(noter) > 4 else ""))
    return new_s


def merge_into_yearfile(new_df: pd.DataFrame, dest: Path, time_col: str,
                        force: bool = False,
                        window: tuple[str, str] | None = None) -> None:
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

    if dest.exists():
        old_s = pd.read_csv(dest, dtype=str, keep_default_na=False)
        _check_key_format(old_s, time_col, dest)
        if force:
            # --force betyder "kasser det hentede vindue og skriv det forfra",
            # IKKE "kasser filen". Uden vinduesafgraensningen ville en
            # genhentning af fx marts slette hele resten af aarsfilen — malt
            # til 14.104 tabte raekker paa spot/DK1_2026.csv. Rammer man den
            # fejl, ser filen helt normal ud; den er bare kortere.
            if window is None:
                raise ValueError("force kraever et vindue")
            ot = pd.to_datetime(old_s[time_col], errors="coerce")
            lo = pd.Timestamp(window[0])
            hi = pd.Timestamp(window[1]) + pd.Timedelta(days=1)
            drop = ot.notna() & (ot >= lo) & (ot < hi)
            if drop.any():
                print(f"    {dest.name}: --force kasserer {int(drop.sum()):,} "
                      f"raekker i {window[0]}..{window[1]}")
            old_s = old_s[~drop]
        for c in old_s.columns:
            if c not in new_s.columns:
                new_s[c] = ""
        for c in new_s.columns:
            if c not in old_s.columns:
                old_s[c] = ""
        new_s = new_s[old_s.columns]
        new_s = conform_to_existing(new_s, old_s, dest.name)
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
                    out_dir: Path, prefix: str = "", force: bool = False,
                    window: tuple[str, str] | None = None) -> None:
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
                                out_dir / f"{prefix}{int(year)}.csv", time_col,
                                force, window)
    else:
        for (zone, year), part in df.groupby([zone_col, "_year"]):
            if pd.isna(zone) or pd.isna(year):
                continue
            merge_into_yearfile(part.drop(columns=["_year"]),
                                out_dir / f"{zone}_{int(year)}.csv", time_col,
                                force, window)


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


# ----------------------------------------------------------------------------
# DATOSEMANTIK — målt, ikke antaget (F6 Gate 0, verificeret igen 2026-09-05)
#
#                   | api.sysapp.dk        | api.energidataservice.dk
#   --------------- | -------------------- | ------------------------
#   filterakse      | UTC                  | dansk lokaltid
#   bar end-dato    | inklusiv hele døgnet | T00:00, eksklusiv
#   start == end    | hele døgnet          | 0 rækker
#
# De to API'er deler datasætnavne og deler intet i datosemantik. Alle tre
# sysapp-endpoints (api_eds_balance, api_energinet_prices, api_dmi_obs_ny) er
# målt enige: `enddate` er en inklusiv bar dato på UTC-aksen, og serveren
# lægger selv døgnet til (`meta.range_utc.to_exclusive == enddate + 1 dag`).
# Derfor sendes `end` UÆNDRET til sysapp. Lægges der et døgn til her, henter
# hver månedskørsel én dag for meget — tavst. Det var fejlen indtil nu.
#
# EDS-siden håndteres i fetch_eds(), som henter et superset og klipper.
# ----------------------------------------------------------------------------

UTC_TIME_COLS = ("TimeUTC", "hour_utc")


def clip_to_utc_window(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """
    Klip til det halvåbne UTC-vindue [start 00:00, end+1d 00:00).

    `start`/`end` er bare datoer, `end` inklusiv — samme betydning som sysapps
    `startdate`/`enddate`. Det er dét der gør de to kilder sammenlignelige.
    """
    if df.empty:
        return df
    col = next((c for c in UTC_TIME_COLS if c in df.columns), None)
    if col is None:
        print(f"    ADVARSEL: ingen UTC-tidskolonne i {list(df.columns)[:5]}…; "
              f"kan ikke klippe til vinduet")
        return df
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end) + pd.Timedelta(days=1)
    t = pd.to_datetime(df[col], errors="coerce")
    return df[(t >= lo) & (t < hi)].reset_index(drop=True)


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
    split_and_write(df, "hour_utc", "price_area", REPO_ROOT / "spot",
                    force=force, window=(start, end))


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
                "enddate": end,                  # inklusiv bar dato, UTC-akse
                "area": zone,
            }
            # api_eds_balance.php afviser ukendte parametre med 400 i stedet
            # for at ignorere dem. 'auction' må kun sendes til mfrr_capacity.
            if extra_params:
                params.update(extra_params)
            df = normalize(fetch_sysapp("api_eds_balance.php", params), rename)
        else:
            df = fetch_eds(eds_endpoint, start, end, zone=zone)
        split_and_write(df, "TimeUTC", "PriceArea", REPO_ROOT / out,
                        force=force, window=(start, end))


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


def _derive_dmi_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    Udled hour_utc og hour_dk af `unixtime` frem for at bruge API'ets egne.

    api_dmi_obs_ny.php afleder felterne med CONVERT_TZ fra flertydig lokal tid.
    Ved efterarsskiftet forekommer lokal 02:00 to gange, og begge far
    hour_utc = "00:00:00" — den anden skulle vaere "01:00:00". Feltet er
    altsa ikke unikt, og det er praecis det felt vi splitter aar og
    deduplikerer pa.

    Konsekvensen er malt: af fire leverede raekker hen over skiftet skrives
    tre, og den overlevende baerer et forkert tidsstempel. At nogle pa
    unixtime alene raekker ikke — sa star to raekker med samme hour_utc i
    filen, og modellens loader laeser hour_utc.

    `unixtime` er et heltal og immunt. Udleder vi selv, er etiketten korrekt
    og unik, og dedup pa hour_utc bliver identisk med dedup pa unixtime.
    Det gor os ogsa immune over for en fremtidig regression samme sted.
    """
    if df.empty or "unixtime" not in df.columns:
        return df
    df = df.copy()
    ux = pd.to_numeric(df["unixtime"], errors="coerce")
    if ux.isna().any():
        raise ValueError(f"{int(ux.isna().sum())} raekker uden brugbar unixtime")
    t = pd.to_datetime(ux.astype("int64"), unit="s", utc=True)
    df["unixtime"] = ux.astype("int64")
    df["hour_utc"] = t.dt.tz_localize(None).dt.strftime(TIME_FMT)
    # NB: dmi/*.csv bruger mellemrum i hour_dk, mens balance-datasaettenes
    # TimeDK bruger "T". Konventionen er ikke ens paa tvaers, saa den skal
    # laeses af filerne og ikke antages.
    df["hour_dk"] = (t.dt.tz_convert("Europe/Copenhagen").dt.tz_localize(None)
                      .dt.strftime(TIME_FMT))
    return df


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
        df = _derive_dmi_time(df)
        split_and_write(df, "hour_utc", None, REPO_ROOT / "dmi",
                        prefix=f"{area}_", force=force, window=(start, end))


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


def main() -> int:
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

    # Tomt interval er ikke en fejl når `start` er udledt: to kørsler i træk
    # giver determine_start() = seneste dato + 1, altså i morgen, mens `--end`
    # som default er i går. api_eds_balance.php afviser det med 400 ("Empty
    # range"), så uden dette værn crasher en kørsel der reelt bare er en no-op.
    # Det var maskeret indtil nu, fordi enddate fik lagt et døgn til og
    # intervallet dermed blev gyldigt — se datosemantik-blokken ovenfor.
    if start > end:
        if args.start:
            print(f"FEJL: --start {start} ligger efter --end {end}; "
                  f"intet at hente.")
            return 2
        print(f"Intet at hente: repo'et dækker allerede til og med {end}. "
              f"(Næste startdato ville være {start}.)")
        return 0

    if "spot" not in skip:      update_spot(start, end, args.force, args.source)
    if "afrr" not in skip:      update_afrr(start, end, args.force, args.source)
    if "mfrr_cap" not in skip:  update_mfrr_cap(start, end, args.force, args.source)
    if "mfrr_act" not in skip:  update_mfrr_act(start, end, args.force, args.source)
    if "imbalance" not in skip: update_imbalance(start, end, args.force, args.source)
    if "dmi" not in skip:       update_dmi(start, end, args.force, args.source)

    print()
    update_version_file(args.source)
    print("Færdig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
