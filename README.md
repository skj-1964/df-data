# df-data

Cachede markeds- og vejrdata til brug i fjernvarme-driftsoptimeringsmodeller, primært til at understøtte modeller som [fjernvarme-businesscase](https://github.com/skj-1964/fjernvarme-businesscase).

Repo'et indeholder råudtræk fra [Energinet Energi Data Service](https://www.energidataservice.dk/) (spotpris og balancemarkeder) og DMI-observationer (vejrdata via en proxy-API). Filerne er splittet pr. kalenderår og prisområde / DMI-område, så det er nemt at hente lige præcis det datasæt en analyse skal bruge — også fra miljøer der ikke har adgang til at kalde Energinet og DMI direkte.

## Struktur

```
df-data/
├── spot/        — Energinet Elspotprices (DKK/MWh, time + 15-min)
│   └── {zone}_{år}.csv               (DK1, DK2 fuldt vedligeholdt; DE/NO2/SE3/SE4/SYSTEM som biprodukt)
├── afrr/        — aFRR reservekapacitet (Up/Down, DKK/MW/h)
│   └── DK1_{år}.csv                  (DK1 fra okt 2024; DK2 har ikke aFRR-marked)
├── mfrr_cap/    — mFRR reservekapacitet (Up/Down, DKK/MW/h)
│   └── DK{1,2}_{år}.csv
├── mfrr_act/    — mFRR aktiveret energi (15-min, DKK/MWh + MW)
│   └── DK{1,2}_{år}.csv
├── imbalance/   — Ubalancepris + aFRR aktiverede VWA-priser (15-min)
│   └── DK{1,2}_{år}.csv
├── dmi/         — DMI vejrobservationer (timeopløst)
│   └── {område}_{år}.csv             (område fx 'fyn', 'vestkyst')
└── scripts/
    └── update_data.py                — Henter ny måned fra API og opdaterer repo
```

## Datadækning

Aktuelle dækningsgrænser (se `DATA_VERSION.md` for præcise datoer pr. kørsel):

| Datasæt | Områder | Periode |
|---|---|---|
| spot | DK1, DK2 | 2023-01 → 2026-Q1 |
| spot | DE, NO2, SE3, SE4 | 2023-01 → 2025-09 (fryses ved ISP15-overgangen) |
| afrr | DK1 | 2024-10 → løbende |
| mfrr_cap | DK1, DK2 | 2023-06 → løbende |
| mfrr_act | DK1, DK2 | 2025-03 → løbende |
| imbalance | DK1, DK2 | 2025-03 → løbende |
| dmi | fyn, vestkyst | 2023-01 → løbende |

Energinet leverer typisk ny data med få dages forsinkelse for spot/aFRR/mFRR, mens mfrr_act og imbalance som 15-min-datasæt først blev publiceret fra marts 2025. aFRR-markedet i DK1 startede oktober 2024.

## Filformater

Råformater fra hver kilde bevares — ingen omformatering, så data også kan bruges til andre formål end fjernvarme-optimering.

**spot** (Energinet `Elspotprices`):
```
id, hour_utc, hour_dk, price_area, spot_price_dkk, spot_price_eur, created_at, updated_at
```

**afrr** (Energinet `AfrrReservesNordic`):
```
TimeUTC, TimeDK, PriceArea, UpDemandMW, UpProcuredMW, UpPriceEUR, UpPriceDKK,
DownDemandMW, DownProcuredMW, DownPriceEUR, DownPriceDKK
```

**mfrr_cap** (Energinet `MfrrCapacityMarket`): samme kolonnesæt som aFRR.

**mfrr_act** (Energinet `MfrrEnergyActivationMarket`):
```
TimeUTC, TimeDK, PriceArea, mFRRSAUpReqMW, mFRRSAUpEUR, mFRRSADownReqMW, 
mFRRSADownEUR, mFRRDAUpMW, mFRRDAUpEUR, mFRRDADownMW, mFRRDADownEUR,
TotalmFRRUpMW, TotalmFRRDownMW, mFRROfferedUpMW, mFRROfferedDownMW, …
```

**imbalance** (Energinet `ImbalancePrice`, 15-min):
```
TimeUTC, TimeDK, PriceArea, SatisfiedDemand, ImbalancePriceEUR, ImbalancePriceDKK,
SpotPriceEUR, DominatingDirection, aFRRUpMW, aFRRVWAUpEUR, aFRRVWAUpDKK,
aFRRDownMW, aFRRVWADownEUR, aFRRVWADownDKK, mFRRMarginalPriceUpEUR, …
```

**dmi** (DMI Frie Data via sysapp.dk-proxy, time-opløst):
```
hour_utc, hour_dk, temp_mean_past1h, radia_glob_past1h, wind_speed_past1h,
precip_past1h, pressure, humidity_past1h
```

Alle vejrvariable er bevaret, ikke kun temperatur — solindstråling og vindhastighed er relevante for værker med solfangere eller luft-vand varmepumper.

## Brug

### Som download

Hent en enkelt fil direkte:
```bash
curl -O https://raw.githubusercontent.com/skj-1964/df-data/main/spot/DK1_2025.csv
```

### Som data-kilde i en model

Eksempel for [fjernvarme-businesscase](https://github.com/skj-1964/fjernvarme-businesscase):
```bash
python run_case.py cases/min_case.yaml --data-source github --year 2025
```

(kræver at `data_loader_github.py` er aktiveret i modellen.)

### Klone hele repo'et

```bash
git clone https://github.com/skj-1964/df-data.git
```

Repo'et er ~50 MB med fuld 2023–2026-dækning, vokser ~13 MB pr. fuldt år DK1-data.

## Opdatering

Repo'et opdateres månedligt via `scripts/update_data.py`, der kører på en serverside-cronjob hos vedligeholderen. Hver opdatering:
1. Henter seneste måneds data fra Energinet og DMI
2. Tilføjer rækker til relevante årsfiler (deduplikeret på tidsstempel + område)
3. Opdaterer `DATA_VERSION.md`
4. Committer og pusher

Energinet leverer typisk balancedata med få dages forsinkelse, så seneste filer kan have hul i den allernyeste tid. `DATA_VERSION.md` viser præcist hvor langt hver data-strøm er ført frem.

## Licens

Data er offentlige fra Energinet (CC BY 4.0) og DMI (CC BY 4.0). Dette repo gør ingen krav på dem og frigiver dem under samme vilkår.

Scripts: MIT.

## Kontakt

Vedligeholdes af Steen Kramer Jensen, Dansk Fjernvarme.
