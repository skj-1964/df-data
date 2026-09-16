#!/usr/bin/env bash
# cron_update.sh — daglig opdatering af df-data med commit kun ved groent lys.
#
# Committer ALDRIG uden at verify_update.py har svaret 0. Under manuel drift
# er `git diff` reviewfladen; under cron er der ingen der laeser den, saa
# kontrollen er det eneste der staar mellem en stille datafejl og repoet.
#
# Crontab (04:10 hver dag, efter EDS/sysapp har lukket doegnet):
#   10 4 * * *  /sti/til/df-data/scripts/cron_update.sh >> /var/log/df-data.log 2>&1

set -uo pipefail

REPO="${DF_DATA_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${DF_DATA_BRANCH:-main}"

# Python: eksplicit valg > venv ved siden af repoet > PATH. Under cron er PATH
# typisk /usr/bin:/bin, saa "python3" er systemets og ikke venv'ets.
if [[ -n "${DF_DATA_PYTHON:-}" ]]; then
    PY="$DF_DATA_PYTHON"
elif [[ -x "$REPO/../venv/bin/python" ]]; then
    PY="$(cd "$REPO/.." && pwd)/venv/bin/python"
else
    PY="python3"
fi

# Forkert interpreter skal stoppe koerslen foer pull, ikke midt i hentningen.
if ! "$PY" -c "import pandas, requests" 2>/dev/null; then
    echo "FEJL: $PY mangler pandas/requests — forkert interpreter?"
    exit 1
fi
echo "python: $PY"

cd "$REPO" || { echo "FEJL: kan ikke skifte til $REPO"; exit 1; }
echo "=== df-data cron $(date -u +%FT%TZ) ==="

# Et beskidt arbejdstrae betyder at nogen er i gang, eller at en tidligere
# koersel stoppede paa roedt lys. Begge dele skal et menneske se paa, saa
# koerslen springes over frem for at lægge nye aendringer ovenpaa.
if [[ -n "$(git status --porcelain -- spot afrr mfrr_cap mfrr_act imbalance dmi)" ]]; then
    echo "STOP: udestaaende aendringer i datamapperne. Ryd op foerst."
    git status --short -- spot afrr mfrr_cap mfrr_act imbalance dmi | head
    exit 1
fi

git pull --ff-only origin "$BRANCH" || { echo "FEJL: pull fejlede"; exit 1; }

# Ingen --start: determine_start gaar 2 doegn tilbage fra seneste raekke, saa
# et ufuldstaendigt sidste doegn hentes faerdigt. Dedup goer det idempotent.
if ! "$PY" scripts/update_data.py; then
    echo "FEJL: update_data.py fejlede — intet committet"
    git checkout -- spot afrr mfrr_cap mfrr_act imbalance dmi 2>/dev/null
    exit 1
fi

if [[ -z "$(git status --porcelain -- spot afrr mfrr_cap mfrr_act imbalance dmi)" ]]; then
    echo "Ingen nye data. Intet at committe."
    git checkout -- DATA_VERSION.md 2>/dev/null
    exit 0
fi

if ! "$PY" scripts/verify_update.py --repo .; then
    echo "STOP: kontrollen fejlede. AENDRINGER BEVARET, IKKE COMMITTET."
    echo "Se dem med: cd $REPO && git diff"
    exit 1
fi

DAYS=$(git diff --numstat -- spot afrr mfrr_cap mfrr_act imbalance dmi \
       | awk '{s+=$1} END {print s+0}')
git add -A spot afrr mfrr_cap mfrr_act imbalance dmi DATA_VERSION.md
git commit -q -m "Automatisk dataopdatering $(date -u +%F) (+${DAYS} raekker)"
git push -q origin "$BRANCH" || { echo "FEJL: push fejlede (commit staar lokalt)"; exit 1; }
echo "Committet og pushet: +${DAYS} raekker"
