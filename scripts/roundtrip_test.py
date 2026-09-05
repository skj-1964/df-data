#!/usr/bin/env python3
"""
Round-trip smoke test paa en maaneds rigtige data — uden API-adgang.

Ide: de eksisterende CSV-filer ER data. Hvis vi oversaetter en maaneds
raekker tilbage til sysapps wire-format (snake_case, DECIMAL som strenge,
created_at/updated_at/auction paa), fjerner maaneden fra filen, og lader
update_data.py skrive den ind igen — saa skal resultatet vaere byte-identisk
med udgangspunktet.

Det tester det API'et ikke kan testes paa her: rename, talkonvertering,
streng-fletning, dedup, sortering og kolonnerraekkefoelge, paa titusinder af
rigtige raekker med rigtige NaN-, heltal- og decimalmoenstre.

Det tester IKKE om sysapp returnerer de samme tal som EDS. Det kraever
compare_sources.py paa serveren.
"""
from __future__ import annotations
import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

# Stier saettes i main() ud fra --repo. Testen roerer ALDRIG originalen:
# den arbejder paa en kopi i et temp-katalog.
PRISTINE: Path
TEST: Path
MONTH: str
U = None  # update_data, importeres fra --repo/scripts


def invert(m: dict) -> dict:
    return {v: k for k, v in m.items()}


CASES: list = []


def _rebuild_cases() -> None:
    """(mappe, fil, tidskolonne, rename-map sysapp->kontrakt, ekstra kolonner)"""
    global CASES
    CASES = [
    ("spot", "DK1_2026.csv", "hour_utc", U.RENAME_SPOT_SYSAPP, []),
    ("spot", "DK2_2026.csv", "hour_utc", U.RENAME_SPOT_SYSAPP, []),
    ("afrr", "DK1_2026.csv", "TimeUTC", U.RENAME_AFRR_CAP, ["created_at", "updated_at"]),
    ("mfrr_cap", "DK1_2026.csv", "TimeUTC", U.RENAME_MFRR_CAP,
     ["created_at", "updated_at", "auction"]),
    ("mfrr_cap", "DK2_2026.csv", "TimeUTC", U.RENAME_MFRR_CAP,
     ["created_at", "updated_at", "auction"]),
    ("mfrr_act", "DK1_2026.csv", "TimeUTC", U.RENAME_MFRR_ACT, ["created_at", "updated_at"]),
    ("mfrr_act", "DK2_2026.csv", "TimeUTC", U.RENAME_MFRR_ACT, ["created_at", "updated_at"]),
    ("imbalance", "DK1_2026.csv", "TimeUTC", U.RENAME_IMBALANCE, ["created_at", "updated_at"]),
    ("imbalance", "DK2_2026.csv", "TimeUTC", U.RENAME_IMBALANCE, ["created_at", "updated_at"]),
    ("dmi", "fyn_2026.csv", "hour_utc", {}, []),
    ("dmi", "karup_2026.csv", "hour_utc", {}, []),
    ("dmi", "vestkyst_2026.csv", "hour_utc", {}, []),
    ]
    yr = MONTH[:4]
    CASES = [(f, n.replace("2026", yr), t, r, e) for f, n, t, r, e in CASES]


def md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def main() -> int:
    global PRISTINE, TEST, MONTH, U
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, help="Sti til df-data-repoet")
    ap.add_argument("--month", default="2026-06", help="YYYY-MM (default: 2026-06)")
    ap.add_argument("--keep", action="store_true", help="Behold temp-kopien")
    a = ap.parse_args()

    PRISTINE = Path(a.repo).resolve()
    MONTH = a.month
    if not (PRISTINE / "scripts" / "update_data.py").exists():
        print(f"FEJL: {PRISTINE}/scripts/update_data.py findes ikke")
        return 2

    sys.path.insert(0, str(PRISTINE / "scripts"))
    import update_data as _U
    U = _U
    globals()["U"] = _U
    _rebuild_cases()

    TEST = Path(tempfile.mkdtemp(prefix="df-data-rt-")) / "repo"
    shutil.copytree(PRISTINE, TEST)
    U.REPO_ROOT = TEST
    print(f"Repo:  {PRISTINE}")
    print(f"Kopi:  {TEST}")
    print(f"Maaned: {MONTH}\n")

    wire: dict[tuple[str, str], pd.DataFrame] = {}
    baseline: dict[tuple[str, str], str] = {}

    # 1) Skaer maaneden ud, oversaet til wire-format, fjern den fra filen.
    for folder, fname, tcol, rename, extra in CASES:
        p = TEST / folder / fname
        baseline[(folder, fname)] = md5(p)
        df = pd.read_csv(p, dtype=str, keep_default_na=False)
        mask = df[tcol].str.startswith(MONTH)
        month = df[mask].copy()
        if month.empty:
            print(f"  SPRING OVER {folder}/{fname}: ingen {MONTH}-raekker")
            continue
        w = month.rename(columns=invert(rename)) if rename else month.copy()
        for c in extra:
            if c not in w.columns:
                w[c] = "main" if c == "auction" else "2026-06-01 00:00:00"
        # sysapp leverer NULL som None, ikke som tom streng
        w = w.where(w != "", None)
        wire[(folder, fname)] = w
        df[~mask].to_csv(p, index=False)
        print(f"  {folder}/{fname}: udskaaret {len(month):,} raekker")

    # 2) Spil dem tilbage gennem update_data.py's rigtige skrivesti.
    def fake_sysapp(path, params):
        if path == "api_energinet_prices.php":
            return wire.get(("spot", f"{params['area']}_2026.csv"), pd.DataFrame())
        if path == "api_eds_balance.php":
            folder = {"afrr_capacity": "afrr", "mfrr_capacity": "mfrr_cap",
                      "mfrr_activation": "mfrr_act", "imbalance_price": "imbalance"}[params["dataset"]]
            return wire.get((folder, f"{params['area']}_2026.csv"), pd.DataFrame())
        if path == "api_dmi_obs_ny.php":
            return wire.get(("dmi", f"{params['area']}_2026.csv"), pd.DataFrame())
        return pd.DataFrame()

    U.fetch_sysapp = fake_sysapp
    print()
    for fn in (U.update_spot, U.update_afrr, U.update_mfrr_cap,
               U.update_mfrr_act, U.update_imbalance, U.update_dmi):
        fn("2026-06-01", "2026-06-30", False, "sysapp")

    # 3) Sammenlign med udgangspunktet.
    print("\n=== resultat ===")
    bad = 0
    for folder, fname in baseline:
        p = TEST / folder / fname
        orig = PRISTINE / folder / fname
        if md5(p) == baseline[(folder, fname)]:
            print(f"  IDENTISK  {folder}/{fname}")
            continue
        bad += 1
        a = pd.read_csv(orig, dtype=str, keep_default_na=False)
        b = pd.read_csv(p, dtype=str, keep_default_na=False)
        print(f"  AFVIGER   {folder}/{fname}  ({len(a):,} -> {len(b):,} raekker)")
        if list(a.columns) != list(b.columns):
            print(f"      kolonner: {list(a.columns)} -> {list(b.columns)}")
            continue
        if len(a) != len(b):
            continue
        for c in a.columns:
            d = a[c] != b[c]
            if d.any():
                i = d.idxmax()
                print(f"      {c}: {d.sum():,} celler, foerste "
                      f"{a[c][i]!r} -> {b[c][i]!r}")
    print(f"\n{len(baseline) - bad}/{len(baseline)} filer byte-identiske")
    if not a.keep:
        shutil.rmtree(TEST.parent, ignore_errors=True)
    else:
        print(f"Kopi beholdt: {TEST}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
