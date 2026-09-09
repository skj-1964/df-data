#!/usr/bin/env python3
"""
verify_update.py — kontrol der skal bestaas foer en automatisk commit.

Under manuel drift er `git diff` reviewfladen. Under cron er der ingen der
laeser den, saa kontrollen skal ligge i koden i stedet. Scriptet er skrevet
til at kunne blive roedt: hver kontrol har en kendt maade at fejle paa, og
--selftest fremkalder dem.

Exit 0 = commit forsvarligt. Exit 1 = stop, et menneske skal se paa det.

Kørsel:
    python scripts/verify_update.py --repo .
    python scripts/verify_update.py --repo . --selftest
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

import pandas as pd

DATA_DIRS = ["spot", "afrr", "mfrr_cap", "mfrr_act", "imbalance", "dmi"]
TCOL = {"spot": "hour_utc", "dmi": "hour_utc"}
AERA_6DEC = "2025-09-30"   # efter denne dato er 6 decimaler aegte


class Result:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.notes: list[str] = []

    def fail(self, msg: str) -> None:
        self.fails.append(msg)
        print(f"  FEJL   {msg}")

    def ok(self, msg: str) -> None:
        print(f"  ok     {msg}")

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print(f"  note   {msg}")


def tcol_for(folder: str) -> str:
    return TCOL.get(folder, "TimeUTC")


# Datasaet hvor kilden reviderer allerede publicerede raekker, og hvor aendrede
# linjer derfor ikke i sig selv er en fejl. Se check_no_deletions.
REVISION_DATASETS = {"mfrr_act"}
REVISION_MAX = 1000          # ~2,5 doegn i kvarter; rummer --lookback=2


def check_no_deletions(repo: Path, r: Result) -> None:
    """
    Ingen slettede linjer i datafilerne.

    En opdatering tilfoejer. Sletter den, er det enten formatdrift eller en
    --force der har ramt bredere end vinduet — begge dele ser ud som gyldige
    filer bagefter.
    """
    out = subprocess.run(["git", "diff", "--numstat", "--"] + DATA_DIRS,
                         cwd=repo, capture_output=True, text=True).stdout
    if not out.strip():
        r.note("ingen aendringer i datafilerne")
        return
    bad, revideret = [], []
    for line in out.strip().splitlines():
        add, rem, path = line.split("\t")
        if rem == "0" or rem == "-":
            continue
        # sysapp reviderer mfrr_activation efter publicering: maalt 2026-09-09
        # aendrede TotalmFRRDownMW og mFRRLocalUpMW sig ved genhentning af et
        # doegn to maaneder gammelt. Ingen af dem bruges af modellen, men
        # aendringen er aegte og ville ellers stoppe cron hver eneste nat.
        # Graensen binder tolerancen til --lookback: en normal koersel henter
        # kun faa doegn om igen, saa flere aendrede raekker end det betyder
        # noget andet end en revision, og skal stadig vaere roedt.
        if path.split("/")[0] in REVISION_DATASETS and int(rem) <= REVISION_MAX:
            revideret.append(f"{path}: {rem} raekker revideret af kilden")
        else:
            bad.append(f"{path}: {rem} slettede linjer")
    for note in revideret:
        r.note(note)
    if bad:
        # Ingen afkortning. Blev listen klippet, kunne kontrollen svare paa
        # noget den ikke havde vist — praecis den tavshed den skal fjerne.
        for b in bad:
            r.fail(b)
    else:
        n = len(out.strip().splitlines())
        r.ok(f"{n} filer aendret, kun tilfoejelser")


def check_gaps(repo: Path, r: Result) -> None:
    """Intet hul stoerre end datasaettets eget skridt, bortset fra kendte."""
    kendte = {
        ("dmi", "fyn"): ["2026-02-28"],
        ("dmi", "vestkyst"): ["2026-02-28"],
        # Seriestarten i mfrr_capacity ligger i DK2, ikke DK1. Undtagelsen
        # pegede paa den forkerte zone og har derfor aldrig virket — kontrollen
        # var permanent roed, og en kontrol der altid er roed bliver ikke laest.
        # Begge zoner starter 2023-06-23. Undtagelsen stod oprindeligt kun
        # paa DK1, saa DK2 var permanent roed — og en kontrol der altid er
        # roed bliver ikke laest.
        ("mfrr_cap", "DK1"): ["2023-06-22"],
        ("mfrr_cap", "DK2"): ["2023-06-22"],
    }
    for folder in DATA_DIRS:
        d = repo / folder
        if not d.exists():
            continue
        tc = tcol_for(folder)
        for f in sorted(d.glob("*.csv")):
            key = f.stem.rsplit("_", 1)[0]
            t = pd.to_datetime(pd.read_csv(f, usecols=[tc])[tc], errors="coerce")
            t = t.dropna().sort_values().drop_duplicates()
            if len(t) < 10:
                continue
            g = t.diff()
            step = g.mode().iloc[0]
            bad = g[g > max(step, pd.Timedelta("1h"))]
            for i in bad.index:
                start = str(t.loc[:i].iloc[-2])[:10]
                if start in kendte.get((folder, key), []):
                    continue
                r.fail(f"{folder}/{f.name}: hul {t.loc[:i].iloc[-2]} -> {t.loc[i]}")


def check_mfrr_auction(repo: Path, r: Result) -> None:
    """
    24 raekker pr. doegn pr. zone i mfrr_cap.

    Faerre betyder at auction=extra er dukket op: da CSV-kontrakten ikke har
    en auction-kolonne, er (TimeUTC, PriceArea) saa ikke laengere entydig, og
    dedup kaster tavst den ene af to gyldige raekker vaek. Det er den fejl der
    ikke melder sig selv.
    """
    d = repo / "mfrr_cap"
    if not d.exists():
        return
    for f in sorted(d.glob("*.csv")):
        x = pd.read_csv(f, usecols=["TimeUTC"])
        t = pd.to_datetime(x["TimeUTC"], errors="coerce").dropna()
        pr = t.dt.date.value_counts().sort_index()
        # foerste og sidste doegn kan vaere delvise; seriestarten i juni 2023
        # er et kendt vilkaar og ikke en auction-kollision
        full = pr.iloc[1:-1] if len(pr) > 2 else pr
        full = full[[str(d) not in ("2023-06-22", "2023-06-23") for d in full.index]]
        afvig = full[full != 24]
        if len(afvig):
            r.fail(f"mfrr_cap/{f.name}: {len(afvig)} doegn uden 24 raekker "
                   f"(foerste: {afvig.index[0]} = {afvig.iloc[0]})")
        else:
            r.ok(f"mfrr_cap/{f.name}: 24 raekker/doegn")


def check_spot_precision(repo: Path, r: Result) -> None:
    """Efter aeragraensen skal spot have mere end to decimaler."""
    d = repo / "spot"
    if not d.exists():
        return
    for f in sorted(d.glob("DK*.csv")):
        x = pd.read_csv(f, dtype=str, keep_default_na=False)
        if "spot_price_dkk" not in x.columns:
            continue
        x = x[x["hour_utc"] > AERA_6DEC]
        if x.empty:
            continue
        x = x.assign(m=x.hour_utc.str[:7],
                     nd=x.spot_price_dkk.map(lambda s: len(s.split(".")[1]) if "." in s else 0))
        andel = x.groupby("m").nd.apply(lambda s: (s > 2).mean())
        flad = andel[andel < 0.5]
        if len(flad):
            r.fail(f"spot/{f.name}: {len(flad)} maaned(er) efter {AERA_6DEC} "
                   f"uden fuld praecision ({', '.join(flad.index[:4])})")
        else:
            r.ok(f"spot/{f.name}: praecision i orden efter aeragraensen")


def check_dmi_axis(repo: Path, r: Result) -> None:
    """hour_utc skal stemme med unixtime — kilden har fejlet her to gange."""
    d = repo / "dmi"
    if not d.exists():
        return
    for f in sorted(d.glob("*.csv")):
        x = pd.read_csv(f, usecols=["unixtime", "hour_utc"])
        exp = pd.to_datetime(x.unixtime, unit="s")
        got = pd.to_datetime(x.hour_utc, errors="coerce")
        n = int((exp != got).sum())
        if n:
            r.fail(f"dmi/{f.name}: {n} raekker hvor hour_utc != unixtime")
    if not r.fails:
        r.ok("dmi: hour_utc stemmer med unixtime overalt")


CHECKS = [
    ("ingen slettede linjer", check_no_deletions),
    ("ingen uventede huller", check_gaps),
    ("mfrr_cap auction", check_mfrr_auction),
    ("spot-praecision", check_spot_precision),
    ("dmi-akse", check_dmi_axis),
]


def selftest(repo: Path) -> int:
    """
    Fremkald hver kontrol som roed. En groen kontrol er ikke et bevis foer
    man har set den kunne blive roed.
    """
    import shutil
    import tempfile
    print("=== selftest: hver kontrol skal kunne fejle ===")
    alle_ok = True
    sabotager = [
        ("ingen uventede huller", "dmi/karup_2026.csv", "drop"),
        ("mfrr_cap auction", "mfrr_cap/DK1_2026.csv", "drop"),
        ("spot-praecision", "spot/DK1_2026.csv", "round"),
        ("dmi-akse", "dmi/fyn_2026.csv", "shift"),
    ]
    for navn, fil, hvordan in sabotager:
        tmp = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(repo, tmp, ignore=shutil.ignore_patterns(".git"))
        p = tmp / fil
        x = pd.read_csv(p, dtype=str, keep_default_na=False)
        if hvordan == "drop":
            x = x.drop(x.index[len(x) // 2])
        elif hvordan == "round":
            x["spot_price_dkk"] = x.spot_price_dkk.map(lambda v: f"{float(v):.2f}")
        elif hvordan == "shift":
            x["hour_utc"] = (pd.to_datetime(x.hour_utc) + pd.Timedelta("1h")
                             ).dt.strftime("%Y-%m-%d %H:%M:%S")
        x.to_csv(p, index=False)
        fn = dict(CHECKS)[navn]
        r = Result()
        fn(tmp, r)
        status = "ROED (godt)" if r.fails else "GROEN — kontrollen maaler ingenting"
        print(f"  {navn:24s} {status}")
        alle_ok &= bool(r.fails)
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return 0 if alle_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--rebuild", action="store_true",
                    help="Bevidst genhentning: slettede linjer er forventede "
                         "og kontrolleres ikke. De oevrige kontroller koerer.")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()

    if a.selftest:
        return selftest(repo)

    print(f"=== verify_update: {repo} ===")
    r = Result()
    for navn, fn in CHECKS:
        if a.rebuild and fn is check_no_deletions:
            r.note("--rebuild: slettede linjer kontrolleres ikke")
            continue
        fn(repo, r)
    print()
    if r.fails:
        print(f"{len(r.fails)} fejl — COMMIT IKKE. Et menneske skal se paa det.")
        return 1
    print("Alle kontroller bestaaet — commit forsvarligt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
