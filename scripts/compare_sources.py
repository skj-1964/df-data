#!/usr/bin/env python3
"""
compare_sources.py — bevis at kildeskiftet sysapp <-> EDS er neutralt.

Henter den samme periode fra begge kilder, normaliserer begge til den frosne
CSV-kontrakt, og sammenligner række for række. Skriver ikke i repo'et.

Det her er det skridt der gør kildeskiftet forsvarligt. Uden det er den eneste
måde at opdage en systematisk afvigelse på, at en modelkørsel giver et tal der
ser forkert nok ud til at nogen studser — og en forskel på fx en enkelt time i
tidsstemplet ville aldrig nå den tærskel.

Kørsel:
    python scripts/compare_sources.py --start 2026-06-01 --end 2026-06-07
    python scripts/compare_sources.py --start 2026-06-01 --end 2026-06-07 \
        --datasets spot,afrr --tol 1e-6
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import update_data as U


def _sysapp_balance(dataset: str, zone: str, start: str, end: str,
                    rename: dict, extra: dict | None = None) -> pd.DataFrame:
    params = {"dataset": dataset, "startdate": start,
              "enddate": U.exclusive_end(end), "area": zone}
    if extra:
        params.update(extra)
    return U.normalize(U.fetch_sysapp("api_eds_balance.php", params), rename)


SPECS = {
    "spot": dict(
        key=["hour_utc", "price_area"],
        zones=U.PRICE_ZONES,
        sysapp=lambda z, s, e: U.normalize(
            U.fetch_sysapp("api_energinet_prices.php",
                           {"startdate": s, "enddate": e, "area": z,
                            "tz": "utc", "fields": "all"}),
            U.RENAME_SPOT_SYSAPP, U.DROP_SPOT),
        eds=lambda z, s, e: U.normalize(
            U.fetch_eds("DayAheadPrices", s, e, zone=z), U.RENAME_SPOT_EDS, U.DROP_SPOT),
    ),
    "afrr": dict(
        key=["TimeUTC", "PriceArea"],
        zones=U.AFRR_ZONES,
        sysapp=lambda z, s, e: _sysapp_balance("afrr_capacity", z, s, e, U.RENAME_AFRR_CAP),
        eds=lambda z, s, e: U.normalize(
            U.fetch_eds("AfrrReservesNordic", s, e, zone=z), {}),
    ),
    "mfrr_cap": dict(
        key=["TimeUTC", "PriceArea"],
        zones=U.MFRR_ZONES,
        sysapp=lambda z, s, e: _sysapp_balance("mfrr_capacity", z, s, e,
                                               U.RENAME_MFRR_CAP, {"auction": "main"}),
        eds=lambda z, s, e: U.normalize(
            U.fetch_eds("MfrrCapacityMarket", s, e, zone=z), {}),
    ),
    "mfrr_act": dict(
        key=["TimeUTC", "PriceArea"],
        zones=U.MFRR_ZONES,
        sysapp=lambda z, s, e: _sysapp_balance("mfrr_activation", z, s, e, U.RENAME_MFRR_ACT),
        eds=lambda z, s, e: U.normalize(
            U.fetch_eds("MfrrEnergyActivationMarket", s, e, zone=z), {}),
    ),
    "imbalance": dict(
        key=["TimeUTC", "PriceArea"],
        zones=U.MFRR_ZONES,
        sysapp=lambda z, s, e: _sysapp_balance("imbalance_price", z, s, e, U.RENAME_IMBALANCE),
        eds=lambda z, s, e: U.normalize(
            U.fetch_eds("ImbalancePrice", s, e, zone=z), {}),
    ),
}


def compare(name: str, zone: str, a: pd.DataFrame, b: pd.DataFrame,
            key: list[str], tol: float) -> bool:
    """Returnerer True hvis identiske indenfor tolerance."""
    tag = f"{name}/{zone}"
    if a.empty and b.empty:
        print(f"  {tag}: begge tomme")
        return True
    if a.empty or b.empty:
        print(f"  {tag}: FEJL — sysapp={len(a)} rækker, eds={len(b)} rækker")
        return False

    tcol = key[0]
    for d in (a, b):
        d[tcol] = pd.to_datetime(d[tcol], errors="coerce")

    only_sysapp = sorted(set(a.columns) - set(b.columns))
    only_eds = sorted(set(b.columns) - set(a.columns))
    if only_sysapp or only_eds:
        print(f"  {tag}: kolonner kun i sysapp={only_sysapp}, kun i eds={only_eds}")

    a = a.set_index(key).sort_index()
    b = b.set_index(key).sort_index()

    ok = True
    miss_a = b.index.difference(a.index)
    miss_b = a.index.difference(b.index)
    if len(miss_a):
        print(f"  {tag}: {len(miss_a)} rækker mangler i sysapp, "
              f"første: {list(miss_a[:3])}")
        ok = False
    if len(miss_b):
        print(f"  {tag}: {len(miss_b)} ekstra rækker i sysapp, "
              f"første: {list(miss_b[:3])}")
        ok = False

    both = a.index.intersection(b.index)
    shared = [c for c in a.columns if c in b.columns]
    worst = []
    for c in shared:
        x = pd.to_numeric(a.loc[both, c], errors="coerce")
        y = pd.to_numeric(b.loc[both, c], errors="coerce")
        if x.isna().all() and y.isna().all():
            continue
        # NaN skal matche NaN — en kolonne der er tom i den ene kilde og
        # udfyldt i den anden er præcis den slags fejl vi leder efter.
        namask = x.isna() ^ y.isna()
        d = (x - y).abs()
        n_bad = int((d > tol).sum()) + int(namask.sum())
        if n_bad:
            worst.append((c, n_bad, float(d.max()) if d.notna().any() else float("nan")))
    if worst:
        ok = False
        for c, n, mx in sorted(worst, key=lambda r: -r[1])[:10]:
            print(f"  {tag}: {c} afviger i {n:,} rækker (max |diff| = {mx:,.6g})")

    if ok:
        print(f"  {tag}: OK — {len(both):,} rækker, {len(shared)} kolonner identiske")
    return ok


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True, help="inklusiv")
    p.add_argument("--datasets", default=",".join(SPECS))
    p.add_argument("--tol", type=float, default=1e-6)
    args = p.parse_args()

    wanted = [d.strip() for d in args.datasets.split(",") if d.strip()]
    all_ok = True
    for name in wanted:
        spec = SPECS.get(name)
        if not spec:
            print(f"Ukendt datasæt: {name}")
            all_ok = False
            continue
        print(f"\n=== {name} {args.start} → {args.end} ===")
        for zone in spec["zones"]:
            try:
                a = spec["sysapp"](zone, args.start, args.end)
                b = spec["eds"](zone, args.start, args.end)
            except Exception as e:
                print(f"  {name}/{zone}: hentning fejlede — {e}")
                all_ok = False
                continue
            all_ok &= compare(name, zone, a, b, spec["key"], args.tol)

    print("\n" + ("ALT OK — kildeskiftet er neutralt."
                  if all_ok else "AFVIGELSER FUNDET — se ovenfor."))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
