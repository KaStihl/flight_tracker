# Flight Deal Watcher

Skript, ktorý denne skenuje najlacnejšie letenky z vybraných európskych letísk
(SK, AT, DE, HU, PL, CZ, FR) kamkoľvek na svete, porovnáva ceny s vlastným
historickým baseline danej trasy, a posiela email, keď cena výrazne klesne pod
normál (možná chybová cena / genuinný deal). Dopĺňa to o druhú vrstvu — RSS
feedy z manuálne kurátorovaných deal-stránok (The Flight Deal, Secret Flying).

## Ako to funguje

```
GitHub Actions (cron, 2x denne)
  │
  ├─ Travelpayouts Data API  →  ceny z každého letiska "kamkoľvek"
  │       │
  │       ├─ ulož do price_history (Supabase)
  │       └─ porovnaj s rolling mediánom danej trasy
  │             → ak cena <= 55 % mediánu A úspora >= 30 EUR → deal
  │
  ├─ RSS feedy (The Flight Deal, Secret Flying)
  │       └─ filter podľa sledovaných miest/krajín → rss_seen (dedup)
  │
  └─ Ak sa niečo našlo → email cez Gmail SMTP (s priamym linkom na let)
```

Prečo baseline namiesto fixnej ceny: fixný prah ("pod 100 EUR") by ignoroval
dobré diaľkové ponuky a zavalil by ťa šumom na lacných krátkych trasách.
Porovnanie ceny s jej vlastným historickým mediánom zachytí anomáliu bez
ohľadu na vzdialenosť.

## Súbory

| Súbor | Účel |
|---|---|
| `flight_deal_watcher.py` | Hlavný skript |
| `requirements.txt` | Python závislosti |
| `.github/workflows/flight_deal_watcher.yml` | GitHub Actions cron (06:00 a 16:00 UTC) |

## Sledované letiská

BTS, VIE, FRA, MUC, BER, DUS, BUD, WAW, KRK, PRG, CDG, ORY, LYS
(uprav v `ORIGINS` na začiatku skriptu)

## Setup

### 1. Supabase (databáza)
1. Vytvor projekt na [supabase.com](https://supabase.com) (free tier, EU región)
2. **Settings → Database → Connection string → URI** (variant "Session pooler")
3. Username musí byť v tvare `postgres.PROJECT_REF` (nie len `postgres`) — inak
   dostaneš `password authentication failed`
4. Ak heslo obsahuje špeciálne znaky, radšej cez **Reset database password**
   necháš Supabase vygenerovať bezpečné heslo automaticky

### 2. Gmail App Password (odosielanie mailu)
1. Google Account → Security → zapni **2-Step Verification** (ak ešte nie je)
2. Tam istom mieste → **App Passwords** → vygeneruj pre "Mail"
3. Použi toto 16-znakové heslo, nie bežné heslo k účtu

### 3. Travelpayouts token
1. Zaregistruj sa na [travelpayouts.com](https://www.travelpayouts.com)
2. **Profile → API token** (nie marker/partner ID — iná hodnota)
3. Over si ho priamo v prehliadači pred nasadením:
   `https://api.travelpayouts.com/v1/prices/cheap?origin=VIE&destination=-&currency=eur&token=TVOJ_TOKEN`

### 4. GitHub Secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Povinný | Poznámka |
|---|---|---|
| `TRAVELPAYOUTS_TOKEN` | áno | z kroku 3 |
| `GMAIL_ADDRESS` | áno | odosielajúci účet |
| `GMAIL_APP_PASSWORD` | áno | z kroku 2, nie bežné heslo |
| `ALERT_TO` | nie | kam chodí mail (default = GMAIL_ADDRESS) |
| `DATABASE_URL` | áno | z kroku 1 |
| `AVIASALES_MARKER` | nie | tvoje Travelpayouts partner ID, pre affiliate odkazy |

Pri copy-paste hodnôt do secretov daj pozor na medzeru/nový riadok na konci —
najčastejšia príčina "funguje v prehliadači, nefunguje v Actions".

### 5. Prvý test
Actions → Flight Deal Watcher → **Run workflow** (manuálne spustenie).
Skontroluj log a Supabase Table Editor (`price_history` by mal rásť).

## Kedy príde prvý email

- **RSS vrstva**: môže prísť hneď, ak niektorý feed spomenie sledované mesto —
  feedy sú ale prevažne US-centrické, takže zhoda je náhodná
- **Cenová anomália**: potrebuje min. 5 predchádzajúcich pozorovaní na danú
  trasu (`MIN_SAMPLES_FOR_BASELINE`), kým vôbec začne vyhodnocovať. Pri behu
  2x denne realisticky **týždeň+**, kým má z čoho počítať baseline.

## Nastaviteľné prahy (v skripte)

| Konštanta | Default | Význam |
|---|---|---|
| `MIN_SAMPLES_FOR_BASELINE` | 5 | koľko pozorovaní na trasu pred prvým vyhodnotením |
| `DEAL_RATIO_THRESHOLD` | 0.55 | alert ak cena <= 55 % mediánu |
| `MIN_ABSOLUTE_SAVING_EUR` | 30 | ignoruj anomálie s úsporou pod 30 EUR |

## Známe obmedzenia / TODO

- **Round-trip vs one-way**: Travelpayouts Data API správanie s `destination=-`
  wildcardom a reálnym `return_date` nie je oficiálne zdokumentované pre
  wildcard destinácie — over si to na reálnych odpovediach (skript to loguje).
- **Secret Flying RSS URL** nebola priamo overená, len nájdená cez tretiu
  stranu — ak feed hlási `broken or unreachable` v logu, nájdi aktuálnu na
  secretflying.com alebo ju vyhoď z `RSS_FEEDS`.
- **RSS keyword matching** je jednoduchý substring match (nie NLP) — zachytí
  "Vienna", nezachytí len skratku "VIE" v texte.
- **Aviasales odkazy** v maile nie sú affiliate-tracked, kým nenastavíš
  `AVIASALES_MARKER`.
- Toto **nenahrádza** platené služby ako Going.com premium — tie majú
  ľudské overenie a komunitu, tento skript len štatistickú anomáliu.

## Prečo nie automatizovaný scraping FB skupín

Facebook zakazuje automatizovaný zber dát vo svojich podmienkach používania,
aj len na čítanie. Riziko je zablokovanie osobného účtu. Namiesto toho tento
projekt používa oficiálne API (Travelpayouts) a verejné RSS feedy.
