# Commuty parking-automatisering

Automatische parkeerreservering bij **Commuty** op basis van je VRT-dienstrooster.
Voorheen ging dit via MyCapacity; dat is vervangen door Commuty.

## Wat het doet

- Leest je planning via de VRT-iCal (alleen dagen mét een shift).
- Reserveert een parking voor elke werkdag met een shift in de komende 7 dagen.
- Respecteert je **credit-plafond** (freelancer = doorgaans 3 credits): het boekt
  nooit meer openstaande dagen dan er credits vrij zijn. Een credit komt bij
  Commuty pas terug wanneer je de gereserveerde dag zélf de parking binnenrijdt
  (`automatic rolling credit return`), dus vroegste shiftdagen krijgen voorrang;
  latere runs pikken uitgestelde dagen op zodra er een credit vrijkomt.
- Stuurt een **Telegram**-melding bij succes, falen én crashes.
- Zet een **Google Calendar**-event (🅿️) 30 min vóór je shift, en houdt dat
  gesynchroniseerd met je echte reserveringen.

## Hoe Commuty-login werkt

Commuty draait op Keycloak (realm `commuty`, publieke client `commuty-web`) en
de REST-API staat op `https://api.commuty.net/{organisatie}/...`. De login loopt
via **Microsoft-SSO** (met MFA), wat een script niet veilig headless kan
nabootsen. In de plaats gebruik je een **offline refresh token**: je maakt dat
één keer aan via je eigen browser, en het script vernieuwt zich er daarna mee —
zonder ooit nog langs Microsoft te moeten.

## Eenmalige setup

1. **Offline refresh token aanmaken** — draai lokaal:

   ```bash
   python get_commuty_token.py
   ```

   Het script toont een link. Open die in de browser waar je al bij Commuty
   ingelogd bent, kopieer de URL (met `code=...`) waar je op belandt, en plak
   die terug. Je krijgt een `COMMUTY_REFRESH_TOKEN`.

2. **Secrets instellen** (GitHub → Settings → Secrets and variables → Actions),
   of lokaal in een `secrets.local.json` (staat in `.gitignore`):

   | Secret | Verplicht | Uitleg |
   |---|---|---|
   | `COMMUTY_REFRESH_TOKEN` | ✅ | Offline token uit stap 1 |
   | `COMMUTY_ICAL_URL` | ✅ | De iCal-feed-URL van je dienstrooster (bevat een geheime token — daarom een secret, niet in de code) |
   | `COMMUTY_ORG` | – | Organisatie-id (het pad-segment in `app.commuty.net/<org>/home`); wordt anders automatisch afgeleid |
   | `COMMUTY_PARKING_SITES` | – | Voorkeursvolgorde van parking-sites, bv. `VRT Put,VRT Reyers,VRT Oost,VRT West` |
   | `COMMUTY_USER_ID` | – | Eigen user-id (wordt automatisch uit `/me` gehaald; enkel nodig als override) |
   | `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` | – | Voor pushmeldingen |
   | `GOOGLE_CALENDAR_ID` | – | Agenda voor de 🅿️-events, bv. je Gmail-adres |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | – | **Aanbevolen** voor de agenda-sync: de volledige JSON-sleutel van een Google service-account. Verloopt nooit. Deel je agenda (`GOOGLE_CALENDAR_ID`) één keer met het `client_email`-adres uit de JSON, met recht "Wijzigingen aan afspraken aanbrengen". |
   | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` | – | Alternatief voor de agenda-sync via OAuth. Let op: een OAuth-app in "Testing" laat het refresh token na 7 dagen verlopen — gebruik liever een service-account. |

3. **Verkennen** — draai één keer de discover-modus zodat het script jouw
   organisatie, parking-sites, credits en de exacte reservatie-structuur toont:

   ```bash
   python reserve_parking.py --discover
   ```

   of via GitHub → Actions → *Commuty Parking Reservering* → *Run workflow* →
   mode `discover`. Maak eventueel eerst één reservering in de Commuty-app; dan
   toont discover de precieze velden die het script moet nasturen.

4. **Voorkeursvolgorde** zetten via `COMMUTY_PARKING_SITES` op basis van de
   sitenamen uit stap 3.

> Verloopt het refresh token ooit (melding via Telegram)? Draai
> `get_commuty_token.py` opnieuw en werk `COMMUTY_REFRESH_TOKEN` bij.

## Draaien

- **Automatisch:** de GitHub Action draait op weekdagen meerdere keren
  (ochtend → vroege middag). Het script is idempotent: bestaande reserveringen
  worden overgeslagen, dubbels worden geweigerd.
- **Handmatig reserveren:** `python reserve_parking.py --reserve`
- **Alleen inloggen/token cachen:** `python reserve_parking.py --login`

## Bestanden

| Bestand | Rol |
|---|---|
| `reserve_parking.py` | Hoofdscript (`--reserve`, `--login`, `--discover`) |
| `.github/workflows/parking.yml` | GitHub Action (planning + handmatige trigger) |
| `get_commuty_token.py` | Eenmalig een Commuty offline refresh token ophalen |
| `get_refresh_token.py` | Eenmalig een Google refresh token ophalen (voor de agenda-sync) |
