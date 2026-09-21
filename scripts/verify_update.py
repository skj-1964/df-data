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

# Genhentning i tilbageblikket (session 29, maalt 2026-09-14): sysapp
# genstempler updated_at/id og regner DKK-priser om med en lidt anden kurs.
# Spot: 288 raekker nyt updated_at, 104 med |Δ DKK| <= 0,055 kr./MWh.
# aFRR: 26 raekker med |Δ DKK| <= 0,007 kr./MW/t. EUR-priserne var uaendrede.
# Det er tilladt, men KUN saadan:
#   * ingen tabte noegler, ingen dubletter, samme kolonner
#   * kun raekker inde i tilbageblikket er aendret
#   * kun metadata-kolonner, eller DKK-kolonner med |Δ| <= DKK_TOL
# Alt andet — EUR, maengder, talformat, raekkefoelge — er stadig roedt.
META_COLS = {"id", "created_at", "updated_at"}
DKK_TOL = 0.1

# Kursrevision (maalt 2026-09-21): DKK-kolonnen kan ogsaa aendre sig mere end
# DKK_TOL, naar kilden har regnet et dansk leveringsdoegn om med en revideret
# EUR/DKK-kurs. Spot 14/9 00:00-01:45 DK: 7,4748 -> 7,4753, EUR uaendret,
# |Δ| op til 0,111 kr./MWh ved ~220 EUR. Det sker ved doegngraensen, fordi
# filerne klippes ved UTC-midnat mens kursen gaelder pr. dansk doegn, og det vil
# komme igen. Den absolutte graense er den forkerte maalestok ved priser paa
# 1.500 kr./MWh; en kursrevision genkendes i stedet paa sin form:
#   * EUR-parret (X_dkk/X_eur, XDKK/XEUR) er uaendret i de samme raekker
#   * pr. dansk doegn: EN gammel og EN ny kurs forklarer alle aendrede raekker
#     (restafvigelse |DKK - EUR*kurs| <= KURS_RES_TOL)
#   * begge kurser ligger i ERM II-baandet, og springet er <= KURS_REL_TOL
# Alt andet — EUR aendret, flere kurser samme doegn, spring > 0,1 % — er roedt.
KURS_RES_TOL = 0.006         # halv enhed ved 2 decimaler (imbalance) + margen
KURS_REL_TOL = 1e-3          # maalte spring: 2,7e-5 og 6,7e-5
KURS_BAAND = (7.2925, 7.6282)  # ERM II: 7,46038 +/- 2,25 %
KURS_MIN_EUR = 1.0           # kurs fittes kun paa raekker med |EUR| >= 1
LOOKBACK_DAYS = 2            # skal matche update_data.py --lookback
WINDOW_SLACK_DAYS = 1


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


def _head_csv(repo: Path, path: str) -> pd.DataFrame:
    import io
    return pd.read_csv(io.StringIO(_git(repo, "show", f"HEAD:{path}")),
                       dtype=str, keep_default_na=False)


def _window_start(repo: Path) -> pd.Timestamp | None:
    """Samme regel som update_data.determine_start, men paa HEAD."""
    seneste = []
    for folder in ("spot", "dmi"):
        m = None
        for path in _git(repo, "ls-tree", "--name-only", "HEAD",
                         f"{folder}/").split():
            if not path.endswith(".csv"):
                continue
            t = pd.to_datetime(_head_csv(repo, path)[tcol_for(folder)],
                               errors="coerce").max()
            if pd.notna(t) and (m is None or t > m):
                m = t
        if m is not None:
            seneste.append(m.normalize())
    if not seneste:
        return None
    return min(seneste) - pd.Timedelta(days=LOOKBACK_DAYS + WINDOW_SLACK_DAYS)


def _is_dkk(col: str) -> bool:
    return col.endswith("DKK") or col.endswith("_dkk")


def _eur_col(c: str, cols) -> str | None:
    """EUR-parret til en DKK-kolonne, hvis det findes."""
    e = (c[:-4] + "_eur") if c.endswith("_dkk") else (c[:-3] + "EUR")
    return e if e in cols else None


def _kursrevision(x: pd.Series, y: pd.Series, eur: pd.Series,
                  t: pd.Series) -> tuple[str | None, str]:
    """
    Forklarer (x -> y) sig som EN kursrevision pr. dansk doegn?
    Returnerer (beskrivelse, "") hvis ja, ellers (None, grund).
    """
    dag = (pd.to_datetime(t).dt.tz_localize("UTC")
           .dt.tz_convert("Europe/Copenhagen").dt.date)
    dele = []
    for d, idx in dag.groupby(dag).groups.items():
        xo, yn, e = x.loc[idx], y.loc[idx], eur.loc[idx]
        stor = e.abs() >= KURS_MIN_EUR
        if not stor.any():
            if (yn - xo).abs().max() <= DKK_TOL:
                continue
            return None, f"{d}: ingen raekker med |EUR| >= {KURS_MIN_EUR} at fitte kursen paa"
        ko = float((xo[stor] / e[stor]).median())
        kn = float((yn[stor] / e[stor]).median())
        rest = max(float((xo - e * ko).abs().max()), float((yn - e * kn).abs().max()))
        if rest > KURS_RES_TOL:
            return None, f"{d}: ikke en ensartet kurs (restafvigelse {rest:.3g})"
        lo, hi = KURS_BAAND
        if not (lo <= ko <= hi and lo <= kn <= hi):
            return None, f"{d}: kurs {ko:.4f} -> {kn:.4f} uden for ERM II-baandet"
        if abs(kn / ko - 1) > KURS_REL_TOL:
            return None, f"{d}: kursspring {ko:.4f} -> {kn:.4f} > {KURS_REL_TOL:.1%}"
        dele.append(f"{d} {ko:.4f}->{kn:.4f}")
    return ("kurs " + ", ".join(dele) if dele else "kurs uaendret"), ""


def _classify(repo: Path, path: str, start: pd.Timestamp | None) -> tuple[list[str], str]:
    """Returnerer (fejl, note) for en fil med fjernede linjer."""
    folder = path.split("/")[0]
    old = _head_csv(repo, path)
    new = pd.read_csv(repo / path, dtype=str, keep_default_na=False)
    fejl = []
    if list(old.columns) != list(new.columns):
        return [f"{path}: kolonner aendret"], ""
    keys = [tcol_for(folder)] + [c for c in ("price_area", "PriceArea")
                                 if c in old.columns]
    if new.duplicated(keys).any():
        fejl.append(f"{path}: {int(new.duplicated(keys).sum())} dublerede noegler")
    o = old.drop_duplicates(keys).set_index(keys)
    n = new.drop_duplicates(keys).set_index(keys)
    tabt = o.index.difference(n.index)
    if len(tabt):
        fejl.append(f"{path}: {len(tabt)} tabte raekker (foerste {tabt[0]})")
    fael = o.index.intersection(n.index)
    a, b = o.loc[fael], n.loc[fael]
    diff = a.ne(b)
    aendret = diff.any(axis=1)
    if not aendret.any():
        if not fejl:
            fejl.append(f"{path}: linjer fjernet uden aendrede raekker "
                        "(raekkefoelge eller format)")
        return fejl, ""
    t = pd.to_datetime(pd.Index(a.index[aendret].get_level_values(0)))
    if start is None or t.min() < start:
        fejl.append(f"{path}: aendringer foer tilbageblikket "
                    f"({t.min()} < {start})")
    dkk_max = 0.0
    kurser: list[str] = []
    for c in a.columns:
        m = diff[c]
        if not m.any() or c in META_COLS:
            continue
        x = pd.to_numeric(a.loc[m, c], errors="coerce")
        y = pd.to_numeric(b.loc[m, c], errors="coerce")
        if _is_dkk(c) and x.notna().all() and y.notna().all():
            d = float((y - x).abs().max())
            if d <= DKK_TOL:
                dkk_max = max(dkk_max, d)
                continue
            grund = f"max |Δ| {d:.4g} > {DKK_TOL}"
            e = _eur_col(c, a.columns)
            if e is None:
                grund += ", intet EUR-par"
            elif diff.loc[m, e].any():
                grund += f", {e} ogsaa aendret"
            else:
                eur = pd.to_numeric(a.loc[m, e], errors="coerce")
                if eur.isna().any():
                    grund += f", {e} ikke numerisk"
                else:
                    tt = pd.Series(x.index.get_level_values(0), index=x.index)
                    rev, hvorfor = _kursrevision(x, y, eur, tt)
                    if rev is not None:
                        dkk_max = max(dkk_max, d)
                        kurser.append(f"{c}: {rev}")
                        continue
                    grund += f", ikke kursrevision: {hvorfor}"
            fejl.append(f"{path}: {c} aendret i {int(m.sum())} raekker, {grund}")
        else:
            fejl.append(f"{path}: {c} aendret i {int(m.sum())} raekker "
                        f"(fx {a.loc[m, c].iloc[0]!r} -> {b.loc[m, c].iloc[0]!r})")
    note = (f"{path}: {int(aendret.sum())} raekker genhentet "
            f"({t.min():%Y-%m-%d} til {t.max():%Y-%m-%d}), kun metadata og "
            f"DKK-omregning (max |Δ| {dkk_max:.3g})"
            + (f"; {'; '.join(kurser)}" if kurser else ""))
    return fejl, note


def check_no_deletions(repo: Path, r: Result) -> None:
    """
    Ingen slettede linjer i datafilerne — med to navngivne undtagelser.

    En opdatering tilfoejer. Sletter den, er det enten formatdrift eller en
    --force der har ramt bredere end vinduet — begge dele ser ud som gyldige
    filer bagefter. Undtagelserne: kildens revisioner i mfrr_act, og
    genstempling/DKK-omregning af raekker i tilbageblikket, herunder
    kursrevisioner pr. dansk doegn (se ovenfor).
    """
    out = subprocess.run(["git", "diff", "--numstat", "--"] + DATA_DIRS,
                         cwd=repo, capture_output=True, text=True).stdout
    if not out.strip():
        r.note("ingen aendringer i datafilerne")
        return
    bad, noter = [], []
    start = None
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
            noter.append(f"{path}: {rem} raekker revideret af kilden")
            continue
        if start is None:
            start = _window_start(repo)
        fejl, note = _classify(repo, path, start)
        if fejl:
            bad.extend(fejl)
        else:
            noter.append(note)
    for note in noter:
        r.note(note)
    if bad:
        # Ingen afkortning. Blev listen klippet, kunne kontrollen svare paa
        # noget den ikke havde vist — praecis den tavshed den skal fjerne.
        for b in bad:
            r.fail(b)
    else:
        n = len(out.strip().splitlines())
        r.ok(f"{n} filer aendret, kun tilfoejelser og tilladte genhentninger")


def check_gaps(repo: Path, r: Result) -> None:
    """Intet hul stoerre end datasaettets eget skridt, bortset fra kendte."""
    kendte = {
        ("dmi", "fyn"): ["2026-02-28", "2026-09-07"],
        ("dmi", "vestkyst"): ["2026-02-28", "2026-09-07"],
        # Begge zoner har hullet 2023-06-22 21:00 -> 2023-06-23 22:00, to
        # doegn inde i serien. Undtagelsen stod oprindeligt kun paa DK1, saa
        # DK2 var permanent roed — og en kontrol der altid er roed bliver
        # ikke laest.
        ("mfrr_cap", "DK1"): ["2023-06-22"],
        ("mfrr_cap", "DK2"): ["2023-06-22"],

        # Kendte kildefejl, maalt 2026-09-09. Hverken aFRR-doegnet eller
        # DMI-timerne lukkes ved genhentning.
        #   afrr DK1: hele auktionsdoegnet 4. september mangler hos sysapp.
        #   dmi: 4-5 timer om formiddagen 7. september i alle tre omraader.
        # NB: undtagelser er permanente og skjuler ogsaa et fremtidigt hul paa
        # samme dato. Fylder kilden dem, boer de fjernes igen.
        ("afrr", "DK1"): ["2026-09-03"],
        ("dmi", "karup"): ["2026-09-07"],
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


# Spot-praecisionen kontrolleres IKKE. Sysapp gemte spot afrundet til to
# decimaler frem til et sted mellem 12. august og 6. september 2026 og har ikke
# backfillet, saa en kontrol af hele serien ville vaere permanent roed. Maalt
# paa Spor A marts-juni 2026 er forskellen mellem to og seks decimaler 1,11 DKK
# af 5,2 mio — 2e-7. Kontrollen blev derfor foerst gjort til en note, men saa
# kunne den ikke laengere blive roed, og selftesten afviste den med rette: en
# note er dokumentation, ikke bevogtning. Den er fjernet frem for at staa som
# en kontrol der ikke maaler noget.
#
# Skal den tilbage en dag, er den rigtige form ikke "findes der gamle maaneder
# uden praecision" men "mangler de seneste 30 doegn praecision" — dvs. opdag at
# kilden begynder at afrunde NYE data igen. Det er den fejl der betyder noget,
# og den kan blive roed.
CHECKS = [
    ("ingen slettede linjer", check_no_deletions),
    ("ingen uventede huller", check_gaps),
    ("mfrr_cap auction", check_mfrr_auction),
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
        ("dmi-akse", "dmi/fyn_2026.csv", "shift"),
    ]
    for navn, fil, hvordan in sabotager:
        tmp = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(repo, tmp, ignore=shutil.ignore_patterns(".git"))
        p = tmp / fil
        x = pd.read_csv(p, dtype=str, keep_default_na=False)
        if hvordan == "drop":
            x = x.drop(x.index[len(x) // 2])
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
    alle_ok &= _selftest_deletions(repo)
    return 0 if alle_ok else 1


def _selftest_deletions(repo: Path) -> bool:
    """
    check_no_deletions laeser git, saa den testes i et lille git-repo med
    spot, aFRR og DMI. To tilfaelde skal vaere GROENNE (den maalte genhentning
    fra 2026-09-14 og kursrevisionen fra 2026-09-21); resten skal vaere roede.
    """
    import shutil
    import tempfile
    filer = ["spot/DK1_2026.csv", "afrr/DK1_2026.csv", "dmi/fyn_2026.csv"]

    def _dkk(x, delta):
        return (pd.to_numeric(x) + delta).map(lambda v: f"{v:.6f}")

    def genhentning(sp, af):
        i = sp.index[-288:]
        sp.loc[i, "updated_at"] = "2026-09-10 14:05:02"
        sp.loc[i[:100], "spot_price_dkk"] = _dkk(sp.loc[i[:100], "spot_price_dkk"], 0.05)
        j = af.index[-26:]
        af.loc[j, "UpPriceDKK"] = _dkk(af.loc[j, "UpPriceDKK"], 0.006)

    def _kursraekker(sp):
        # 8 raekker i tilbageblikket, samme danske doegn, |EUR| >= 60, saa
        # et kursspring paa 0,002 giver |Δ| > DKK_TOL og rammer kursreglen
        t = sp.iloc[-288:]
        eur = pd.to_numeric(t.spot_price_eur)
        dag = (pd.to_datetime(t.hour_utc).dt.tz_localize("UTC")
               .dt.tz_convert("Europe/Copenhagen").dt.date)
        ok = eur.abs() >= 60
        bedst = dag[ok].value_counts().index[0]
        i = t.index[ok & (dag == bedst)][:8]
        assert len(i) == 8, "selftest: for faa raekker med |EUR| >= 60"
        return i, eur.loc[i], pd.to_numeric(sp.loc[i, "spot_price_dkk"])

    def kursrevision(sp, af):
        # formen fra 2026-09-21: EUR uaendret, een ny kurs for doegnet
        i, eur, dkk = _kursraekker(sp)
        sp.loc[i, "spot_price_dkk"] = (eur * (dkk / eur + 0.002)).map(lambda v: f"{v:.6f}")
        sp.loc[i, "updated_at"] = "2026-09-18 14:05:02"

    def kurs_uensartet(sp, af):
        i, eur, dkk = _kursraekker(sp)
        fortegn = pd.Series([1, -1] * 4, index=i)
        sp.loc[i, "spot_price_dkk"] = (eur * (dkk / eur + 0.002 * fortegn)).map(lambda v: f"{v:.6f}")

    def kursspring(sp, af):
        i, eur, dkk = _kursraekker(sp)
        sp.loc[i, "spot_price_dkk"] = (dkk * 1.002).map(lambda v: f"{v:.6f}")

    def tabt_raekke(sp, af):
        sp.drop(sp.index[-50], inplace=True)

    def dkk_over_tol(sp, af):
        i = sp.index[-10:]
        sp.loc[i, "spot_price_dkk"] = _dkk(sp.loc[i, "spot_price_dkk"], 5.0)

    def eur_aendret(sp, af):
        i = sp.index[-10:]
        sp.loc[i, "spot_price_eur"] = _dkk(sp.loc[i, "spot_price_eur"], 0.01)

    def gammel_raekke(sp, af):
        sp.loc[sp.index[-96 * 30], "updated_at"] = "2026-09-10 14:05:02"

    def talformat(sp, af):
        j = af.index[-5:]
        # 90.0 -> 90: samme tal, andet format
        af.loc[j, "UpDemandMW"] = af.loc[j, "UpDemandMW"].str.replace(
            r"\.0$", "", regex=True)

    def maengde(sp, af):
        j = af.index[-5:]
        af.loc[j, "UpProcuredMW"] = "1.0"

    def byttet_om(sp, af):
        a, b = sp.index[-3], sp.index[-2]
        ra, rb = sp.loc[a].copy(), sp.loc[b].copy()
        sp.loc[a], sp.loc[b] = rb, ra

    tilfaelde = [
        ("genhentning 14/9 (skal vaere groen)", genhentning, False),
        ("kursrevision 21/9 (skal vaere groen)", kursrevision, False),
        ("kurs ikke ensartet i doegnet", kurs_uensartet, True),
        ("kursspring > 0,1 %", kursspring, True),
        ("tabt raekke", tabt_raekke, True),
        ("DKK over tolerance", dkk_over_tol, True),
        ("EUR aendret", eur_aendret, True),
        ("aendring foer tilbageblik", gammel_raekke, True),
        ("talformat aendret", talformat, True),
        ("maengde aendret", maengde, True),
        ("raekker byttet om", byttet_om, True),
    ]
    ok = True
    base = Path(tempfile.mkdtemp())
    try:
        src = base / "src"
        for f in filer:
            (src / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(repo / f, src / f)
        g = ["git", "-c", "user.name=selftest", "-c", "user.email=s@t"]
        subprocess.run(g + ["init", "-q"], cwd=src, check=True)
        subprocess.run(g + ["add", "."], cwd=src, check=True)
        subprocess.run(g + ["commit", "-q", "-m", "base"], cwd=src, check=True)
        for navn, fn, skal_fejle in tilfaelde:
            subprocess.run(["git", "checkout", "-q", "--", "."], cwd=src, check=True)
            sp = pd.read_csv(src / filer[0], dtype=str, keep_default_na=False)
            af = pd.read_csv(src / filer[1], dtype=str, keep_default_na=False)
            fn(sp, af)
            sp.to_csv(src / filer[0], index=False)
            af.to_csv(src / filer[1], index=False)
            r = Result()
            import contextlib, io as _io
            with contextlib.redirect_stdout(_io.StringIO()):
                check_no_deletions(src, r)
            roed = bool(r.fails)
            godt = roed == skal_fejle
            forventet = "ROED" if skal_fejle else "GROEN"
            print(f"  {'ingen slettede linjer':24s} {navn}: "
                  f"{'ROED' if roed else 'GROEN'} "
                  f"({'godt' if godt else 'FORKERT, forventet ' + forventet})")
            if not godt or roed:
                for x in r.fails:
                    print(f"      {x}")
            ok &= godt
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


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
