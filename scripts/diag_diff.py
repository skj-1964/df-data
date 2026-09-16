"""
diag_diff.py — hvad er der ændret i de linjer, verify_update kalder "slettede"?

Sammenligner hver datafil i arbejdstræet med HEAD pr. tidsnøgle og deler
ændringerne i tre:
  * tabte rækker     — nøglen findes i HEAD, men ikke længere (ægte tab)
  * metadata         — kun id/created_at/updated_at er ændret
  * værdier          — datakolonner er ændret (revision fra kilden)

Rører ingen filer.

Kør fra df-data-roden:
    python scripts/diag_diff.py
    python scripts/diag_diff.py spot/DK1_2026.csv afrr/DK1_2026.csv
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIRS = ["spot", "afrr", "mfrr_cap", "mfrr_act", "imbalance", "dmi"]
TCOL = {"spot": "hour_utc", "dmi": "hour_utc"}      # øvrige: TimeUTC
META = {"id", "created_at", "updated_at"}


def changed_files() -> list[str]:
    out = subprocess.run(["git", "diff", "--numstat", "--"] + DATA_DIRS,
                         capture_output=True, text=True, check=True).stdout
    return [l.split("\t")[2] for l in out.strip().splitlines()
            if l and l.split("\t")[1] not in ("0", "-")]


def read_head(path: str) -> pd.DataFrame:
    txt = subprocess.run(["git", "show", f"HEAD:{path}"],
                         capture_output=True, text=True, check=True).stdout
    return pd.read_csv(io.StringIO(txt), dtype=str, keep_default_na=False)


def keycols(folder: str, df: pd.DataFrame) -> list[str]:
    k = [TCOL.get(folder, "TimeUTC")]
    for extra in ("price_area", "PriceArea"):
        if extra in df.columns:
            k.append(extra)
    return k


def diag(path: str) -> None:
    folder = path.split("/")[0]
    old = read_head(path)
    new = pd.read_csv(path, dtype=str, keep_default_na=False)
    keys = keycols(folder, old)
    print(f"\n=== {path}  (nøgle: {', '.join(keys)})")

    if list(old.columns) != list(new.columns):
        print(f"  KOLONNER ÆNDRET\n    før:  {list(old.columns)}\n    nu:   {list(new.columns)}")

    for navn, df in (("HEAD", old), ("nu", new)):
        d = df.duplicated(keys).sum()
        if d:
            print(f"  DUBLETTER i {navn}: {d} nøgler")

    o = old.drop_duplicates(keys).set_index(keys)
    n = new.drop_duplicates(keys).set_index(keys)
    tabt = o.index.difference(n.index)
    ny = n.index.difference(o.index)
    faelles = o.index.intersection(n.index)
    print(f"  rækker: HEAD {len(o):,}  nu {len(n):,}  nye {len(ny):,}  TABTE {len(tabt):,}")
    if len(tabt):
        print(f"    første tabte: {list(tabt[:3])}  sidste: {list(tabt[-3:])}")

    cols = [c for c in o.columns if c in n.columns]
    a, b = o.loc[faelles, cols], n.loc[faelles, cols]
    diff = a.ne(b)
    aendret = diff.any(axis=1)
    if not aendret.any():
        print("  ingen ændrede rækker blandt de fælles nøgler")
        return
    # En værdi er kun ændret, hvis tallet er et andet: '90' -> '90.0' er format.
    reel = pd.DataFrame(False, index=a.index, columns=cols)
    for c in cols:
        if c in META or not diff[c].any():
            continue
        x = pd.to_numeric(a[c], errors="coerce")
        y = pd.to_numeric(b[c], errors="coerce")
        tal = x.notna() & y.notna()
        reel[c] = diff[c] & ~(tal & ((y - x).abs() <= 1e-9))
    vaerdi = reel.any(axis=1) & aendret
    meta = diff[[c for c in cols if c in META]].any(axis=1) & aendret & ~vaerdi
    fmt = aendret & ~vaerdi & ~meta
    print(f"  ændrede rækker: {aendret.sum():,}  = kun metadata {meta.sum():,}"
          f" + kun talformat {fmt.sum():,} + VÆRDIÆNDRING {vaerdi.sum():,}")
    t = pd.Index(a.index[aendret].get_level_values(0))
    print(f"  tidsrum: {t.min()} → {t.max()}")

    for c in cols:
        m = diff[c] & aendret
        if not m.any():
            continue
        x = pd.to_numeric(a.loc[m, c], errors="coerce")
        y = pd.to_numeric(b.loc[m, c], errors="coerce")
        if c not in META and x.notna().all() and y.notna().all():
            d = (y - x).abs()
            reelt = int((d > 1e-9).sum())
            print(f"    {c:28s} {m.sum():5,} linjer  numerisk ændret {reelt:5,}  "
                  f"max |Δ| {d.max():.6g}")
        else:
            eks = f"{a.loc[m, c].iloc[0]!r} -> {b.loc[m, c].iloc[0]!r}"
            print(f"    {c:28s} {m.sum():5,} linjer  fx {eks}")


def main() -> int:
    filer = sys.argv[1:] or changed_files()
    if not filer:
        print("Ingen datafiler med slettede linjer.")
        return 0
    for f in filer:
        diag(f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
