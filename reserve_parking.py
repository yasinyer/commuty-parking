#!/usr/bin/env python3
"""
Commuty - Automatische parkeerreservering
- Leest planning via iCal (alleen dagen met een shift)
- Reserveert voor alle werkdagen in de komende 7 dagen met een shift
- Respecteert het credit-plafond: reserveert nooit meer openstaande dagen dan
  er credits beschikbaar zijn (een credit komt pas vrij als je de dag zelf de
  parking binnenrijdt — "automatic rolling credit return")
- Telegram-melding bij succes, falen én crashes
- Google Calendar-event (🅿️) 30 min voor de shift
- Modi: --login (token cachen), --reserve (reserveren), --discover (API verkennen)

Commuty gebruikt Keycloak (realm 'commuty', publieke client 'commuty-web') met
Authorization Code + PKCE. De REST-API staat op https://api.commuty.net/ en is
per organisatie ge-scoped: https://api.commuty.net/{organisationExternalId}/...
"""

import requests, re, json, os, sys, time, traceback, warnings
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.request import urlopen
from urllib.parse import quote, urlencode

warnings.filterwarnings("ignore")

BRUSSELS  = ZoneInfo("Europe/Brussels")
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))

# ── Configuratie: env vars eerst, dan secrets.local.json (gitignored) ──
def _load_local_secrets():
    try:
        with open(os.path.join(BASE_DIR, "secrets.local.json")) as f:
            return json.load(f)
    except Exception:
        return {}

_LOCAL = _load_local_secrets()

def cfg(name, default=""):
    return os.environ.get(name) or _LOCAL.get(name, default)

COMMUTY_REFRESH_TOKEN = cfg("COMMUTY_REFRESH_TOKEN")
# Organisatie-id (het pad-segment in app.commuty.net/<org>/home). Verplicht
# via env/secret — geen persoonlijke default in de (publieke) broncode.
COMMUTY_ORG = cfg("COMMUTY_ORG")

# Voorkeursvolgorde van parking-sites: komma-gescheiden lijst van site-namen
# of site-ID's (zoals ze in --discover verschijnen). Standaard: dezelfde volgorde
# als vroeger bij MyCapacity (Put → Reyers → Oost → West).
SITE_PREFERENCE = [s.strip() for s in
                   cfg("COMMUTY_PARKING_SITES", "VRT Put,VRT Reyers,VRT Oost,VRT West").split(",")
                   if s.strip()]

GOOGLE_CLIENT_ID     = cfg("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = cfg("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = cfg("GOOGLE_REFRESH_TOKEN")
GOOGLE_CALENDAR_ID   = cfg("GOOGLE_CALENDAR_ID")

TELEGRAM_TOKEN   = cfg("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = cfg("TELEGRAM_CHAT_ID")

# Reserveringsvenster (Belgische tijd). Commuty staat max 14u per dag toe op de
# VRT-terreinen, dus 07:00–19:00 (12u) zit ruim binnen de grens.
START_HOUR  = 7
END_HOUR    = 19
TOKEN_FILE  = os.path.join(BASE_DIR, ".token_cache")

# Persoonlijke iCal-feed van het dienstrooster. Verplicht via env/secret
# (COMMUTY_ICAL_URL) — staat NIET in de broncode omdat de URL een geheime token
# bevat die toegang geeft tot het rooster.
ICAL_URL = cfg("COMMUTY_ICAL_URL")

# ── Commuty / Keycloak ──────────────────────────────────────────
KC_BASE      = "https://auth.commuty.net/auth"
KC_REALM     = "commuty"
KC_CLIENT_ID = "commuty-web"
KC_TOKEN_URL = f"{KC_BASE}/realms/{KC_REALM}/protocol/openid-connect/token"

API_BASE = "https://api.commuty.net/"
UA       = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) commuty-parking-bot/1.0"
# ──────────────────────────────────────────────────────────────


def api_json(r, context=""):
    """Response → JSON, met duidelijke fout bij HTML-foutpagina's of niet-2xx."""
    if not r.ok:
        raise RuntimeError(f"{context}: HTTP {r.status_code} — {r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"{context}: geen JSON (HTTP {r.status_code}) — {r.text[:200]}")


def with_retry(fn, context="", attempts=3, delay=0.5):
    """Korte retry voor transiente netwerkfouten — het reserveerpad mag niet
    sneuvelen op één connection reset, maar moet wel snel blijven."""
    for i in range(attempts):
        try:
            return fn()
        except requests.exceptions.RequestException as e:
            if i == attempts - 1:
                raise
            print(f"  {context}: netwerkfout ({e.__class__.__name__}), retry {i + 1}...")
            time.sleep(delay)


def belgian_now():
    return datetime.now(BRUSSELS)


# ── Keycloak-auth via offline refresh token ─────────────────────
# Commuty-login loopt via Microsoft-SSO (met MFA), wat headless niet veilig
# na te bootsen is. In de plaats gebruiken we een offline refresh token dat je
# één keer aanmaakt met get_commuty_token.py (zie README). Het script wisselt
# dat token bij elke run in voor een kortstondig access token, zonder ooit nog
# langs Microsoft te moeten.

def refresh_access_token(refresh_token):
    """Wissel een refresh token in voor een access token. Geeft
    (access_token, refresh_token) of None bij mislukking."""
    try:
        r = requests.post(KC_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "client_id":     KC_CLIENT_ID,
            "refresh_token": refresh_token,
            "scope":         "openid offline_access",
        }, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=20)
        if not r.ok:
            print(f"  Refresh geweigerd (HTTP {r.status_code}): {r.text[:150]}")
            return None
        tok = r.json()
        if "access_token" not in tok:
            return None
        # Keycloak kan het refresh token roteren; bewaar het nieuwe zodat een
        # volgende run niet op een verlopen token draait.
        return tok["access_token"], tok.get("refresh_token", refresh_token)
    except Exception as e:
        print(f"  Refresh mislukt: {e}")
        return None


def save_token(access, refresh=""):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": access, "refresh": refresh,
                   "saved_at": datetime.now().isoformat()}, f)
    os.chmod(TOKEN_FILE, 0o600)


def _cached_refresh_token():
    try:
        with open(TOKEN_FILE) as f:
            return json.load(f).get("refresh") or ""
    except Exception:
        return ""


def get_access_token():
    """Geeft een geldig access token. Gebruikt eerst het gecachede access token
    (< 4 min oud), anders een refresh met het (gecachede of geconfigureerde)
    refresh token."""
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        age = (datetime.now() - datetime.fromisoformat(data["saved_at"])).total_seconds() / 60
        if age < 4 and data.get("token"):
            print(f"Access token uit cache ({age:.1f} min oud).")
            return data["token"]
    except Exception:
        pass

    refresh = _cached_refresh_token() or COMMUTY_REFRESH_TOKEN
    if not refresh:
        raise RuntimeError("Geen refresh token. Draai eenmalig get_commuty_token.py en zet "
                           "de uitvoer als COMMUTY_REFRESH_TOKEN (env var of secrets.local.json).")
    result = refresh_access_token(refresh)
    if not result:
        raise RuntimeError("Refresh token verlopen of ongeldig. Draai get_commuty_token.py "
                           "opnieuw en werk COMMUTY_REFRESH_TOKEN bij.")
    print("Access token vernieuwd via refresh token.")
    save_token(*result)
    return result[0]


# ── Commuty REST-API helpers ────────────────────────────────────
def _auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": UA}


def api_get(token, path, org=None, params=None, context=""):
    base = f"{API_BASE}{org + '/' if org else ''}"
    r = with_retry(lambda: requests.get(base + path, headers=_auth_headers(token),
                                        params=params, timeout=20), context or path)
    return api_json(r, context or f"GET {path}")


def get_organisation(token):
    """Bepaalt de organisationExternalId en userId van de ingelogde gebruiker.
    'memberships' is niet org-ge-scoped en levert de organisatie(s)."""
    data = api_get(token, "memberships", context="memberships")
    items = data if isinstance(data, list) else data.get("data") or data.get("memberships") or []
    if not items:
        raise RuntimeError(f"Geen memberships gevonden: {str(data)[:200]}")
    m = items[0]
    org = ((m.get("organisation") or {}).get("id")
           or (m.get("organisation") or {}).get("externalId")
           or m.get("organisationExternalId"))
    user_id = (m.get("user") or {}).get("id") or m.get("userId")
    if not org:
        raise RuntimeError(f"Kon organisationExternalId niet afleiden uit membership: {str(m)[:200]}")
    return org, user_id


def resolve_org(token):
    """De organisatie-id: uit COMMUTY_ORG (env/secret), anders automatisch
    afgeleid via 'memberships'."""
    if COMMUTY_ORG:
        return COMMUTY_ORG
    org, _ = get_organisation(token)
    return org


def get_parking_sites(token, org):
    data = api_get(token, "parking-sites", org=org, context="parking-sites")
    items = data if isinstance(data, list) else data.get("data") or data.get("parkingSites") or []
    sites = []
    for s in items:
        sid = s.get("id") or s.get("parkingSiteId")
        name = s.get("name") or sid
        if sid:
            sites.append({"id": sid, "name": name})
    return sites


def ordered_sites(sites):
    """Sites in voorkeursvolgorde (SITE_PREFERENCE op naam of id), rest erachter."""
    if not SITE_PREFERENCE:
        return sites
    def rank(s):
        for i, pref in enumerate(SITE_PREFERENCE):
            if pref.lower() in (str(s["id"]).lower(), str(s["name"]).lower()):
                return i
        return len(SITE_PREFERENCE) + 1
    return sorted(sites, key=rank)


def get_credit_balance(token, org):
    """Beschikbare regular parking-credits (freelancer = doorgaans 3), afgeleid
    uit de meest recente budget-transactie. None als het niet bepaald kan worden."""
    try:
        data = api_get(token, "budget-transactions", org=org, context="budget-transactions")
    except Exception:
        return None
    items = data if isinstance(data, list) else data.get("data") or []
    if not items:
        return None
    # Meest recente transactie eerst; nextRegularCreditBudgetBalance = saldo erna.
    try:
        latest = max(items, key=lambda t: t.get("valueDatetime", ""))
        return int(float(latest.get("nextRegularCreditBudgetBalance")))
    except (TypeError, ValueError):
        return None


def _activities(token, org, start_date, end_date):
    """Ruwe activities-lijst voor een datumbereik (planner-endpoint)."""
    return api_get(token, "activities", org=org,
                   params={"startDate": start_date.isoformat(), "endDate": end_date.isoformat()},
                   context="activities")


def get_existing_requests(token, org):
    """Dict {datum: site_naam} van komende, niet-geannuleerde parkingreserveringen."""
    today = belgian_now().date()
    reserved = {}
    try:
        items = _activities(token, org, today, today + timedelta(days=14))
    except Exception:
        return reserved
    for it in (items if isinstance(items, list) else []):
        psr = it.get("parkingSpotRequest")
        # Geannuleerde reserveringen dragen de titel "Geannuleerd" en tellen niet.
        if not psr or (it.get("title") or "").strip().lower() == "geannuleerd":
            continue
        day = (it.get("startTime") or "")[:10]
        site = (psr.get("parkingSite") or {}).get("name") or (psr.get("parkingSite") or {}).get("id") or "?"
        if day:
            try:
                reserved[datetime.strptime(day, "%Y-%m-%d").date()] = site
            except ValueError:
                pass
    return reserved


def _utc_z(dt):
    """Belgische datetime → UTC-timestamp met Z-suffix (Commuty eist UTC)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# Commuty-grenzen voor de duur van een parkeeraanvraag op de VRT-terreinen.
MIN_MINUTES = 240   # 4 uur
MAX_MINUTES = 840   # 14 uur


def _floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(dt):
    if dt.minute or dt.second or dt.microsecond:
        return dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return dt.replace(second=0, microsecond=0)


def _reservation_window(date, shift=None):
    """(startTime, endTime) als UTC-Z-strings. Gebruikt de shift-uren als die er
    zijn, AFGEROND op hele uren (Commuty aanvaardt enkel hele-uur-tijdstippen:
    de app rondt bv. 18:21 → 19:00). Start naar beneden, einde naar boven,
    geklemd op 4–14u. Geen shift bekend → standaardvenster 07:00–19:00."""
    if shift and shift[0] and shift[1] and shift[1] > shift[0]:
        start = _floor_hour(shift[0])
        end   = _ceil_hour(shift[1])
        dur = (end - start).total_seconds() / 60
        if dur < MIN_MINUTES:
            end = start + timedelta(minutes=MIN_MINUTES)
        elif dur > MAX_MINUTES:
            end = start + timedelta(minutes=MAX_MINUTES)
        return _utc_z(start), _utc_z(end)
    start = datetime(date.year, date.month, date.day, START_HOUR, 0, tzinfo=BRUSSELS)
    end   = datetime(date.year, date.month, date.day, END_HOUR, 0, tzinfo=BRUSSELS)
    return _utc_z(start), _utc_z(end)


def get_user_id(token, org):
    """De eigen user-id, nodig als 'companion' in de reservering.
    Overschrijfbaar via COMMUTY_USER_ID."""
    override = cfg("COMMUTY_USER_ID")
    if override:
        return int(override)
    try:
        me = api_get(token, "me", org=org, context="me")
        return me.get("id")
    except Exception:
        return None


def _activities_body(user_id, site_id, start_ts, end_ts):
    """POST-body voor één parkingreservering op het 'activities'-endpoint,
    exact zoals de webapp die verstuurt: een parkingSpotRequest met de gebruiker
    zelf als companion. start_ts/end_ts zijn UTC-Z-strings."""
    return {
        "commutes": [],
        "outOfOffices": [],
        "parkingSpotRequests": [{
            "period": {
                "startTime": start_ts,
                "endTime":   end_ts,
                "isAllDay":  False,
            },
            "isOneWay":   False,
            "reminder":   False,
            "companions": [{"id": user_id, "driver": False}],
            "parkingSpot": {
                "vehicleType":             "car",
                "requestsEvCharger":       False,
                "requestsSpotInGroups":    False,
                "requestsSpotForDisabled": False,
                "requestsLargeSpot":       False,
                "supportsSpotInGroups":    False,
                "parkingSite":             {"id": site_id},
            },
        }],
        "resourceBookings": [],
        "resourceRequests": [],
        "parkingVisits":    [],
    }


def _confirm_reserved(token, org, date):
    """Grond-waarheid: staat er op 'date' écht een niet-geannuleerde
    parkingreservering in de planner? Commuty geeft namelijk soms een 2xx terug
    op een POST zonder dat er een reservering ontstaat (bv. een site waar je geen
    recht op hebt), dus een 2xx-status alleen is geen bewijs."""
    try:
        items = _activities(token, org, date, date + timedelta(days=1))
    except Exception:
        return False
    for it in (items if isinstance(items, list) else []):
        if (it.get("startTime") or "")[:10] != date.isoformat():
            continue
        if (it.get("title") or "").strip().lower() == "geannuleerd":
            continue
        if it.get("parkingSpotRequest"):
            return True
    return False


def reserve_for_date(token, org, date, sites, user_id, shift=None):
    """Reserveert een parking voor één dag via POST activities. Probeert de sites
    in voorkeursvolgorde. Geeft (status, site_naam), status ∈
    {ok, existing, unavailable, failed, no_credit, not_open}."""
    headers = {**_auth_headers(token), "Content-Type": "application/json"}
    start_ts, end_ts = _reservation_window(date, shift)
    # 'out_of_bound' kan betekenen: die site zit vol/niet toegelaten die dag, OF
    # de dag ligt nog te ver vooruit. We proberen daarom ALLE sites; enkel als
    # elke site out_of_bound geeft, beschouwen we de dag als (nog) niet boekbaar.
    all_out_of_bound = True
    # Sommige sites geven een 2xx terug zonder dat er écht geboekt wordt
    # (spookboeking). We verifiëren zulke 2xx'en in de planner en, als er niets
    # blijkt te staan, gaan we door naar de volgende site i.p.v. vals 'ok' te
    # melden. Blijft ALLES een spook/out_of_bound, dan is de dag (nog) niet
    # boekbaar → 'not_open' (stille retry), geen valse succes- of foutmelding.
    phantom = False
    for site in ordered_sites(sites):
        body = _activities_body(user_id, site["id"], start_ts, end_ts)
        r = with_retry(lambda: requests.post(
            f"{API_BASE}{org}/activities", headers=headers, json=body, timeout=20,
        ), f"reserveren {site['name']}")
        if r.status_code in (200, 201):
            if _confirm_reserved(token, org, date):
                print(f"  {date}: ✅ Gereserveerd — {site['name']}")
                return "ok", site["name"]
            # 2xx maar geen reservering in de planner → spookboeking.
            phantom = True
            print(f"  {date}: {site['name']} gaf 2xx maar geen echte reservering "
                  f"(spookboeking) — volgende site proberen...")
            continue
        txt = r.text[:200]
        low = txt.lower()
        if "already_exists" in low or "already" in low or "reeds" in low or r.status_code == 409:
            print(f"  {date}: al gereserveerd")
            return "existing", None
        if "credit" in low or "budget" in low:
            print(f"  {date}: ❌ Onvoldoende credits ({txt})")
            return "no_credit", None
        if "out_of_bound" in low or "verboden" in low or "forbidden" in low:
            print(f"  {date}: {site['name']} vol/niet toegelaten — volgende site proberen...")
            continue
        all_out_of_bound = False
        print(f"  {date}: {site['name']} mislukt ({r.status_code}: {txt}), volgende site proberen...")
    if all_out_of_bound or phantom:
        # Alle sites vol, spook, of de dag ligt nog buiten het boekingsvenster;
        # latere runs proberen het opnieuw.
        print(f"  {date}: (nog) geen enkele site beschikbaar — later opnieuw")
        return "not_open", None
    print(f"  {date}: ❌ Alle sites geweigerd")
    return "failed", None


# ── Planning (iCal) ─────────────────────────────────────────────
def _parse_ical_date(dtstart):
    """DTSTART → Belgische datum. iCal-tijden van VRT zijn UTC (Z-suffix);
    zonder conversie belandt een vroege nachtshift op de verkeerde dag."""
    m = re.match(r"^(\d{8})(?:T(\d{6}))?(Z?)$", dtstart.strip())
    if not m:
        return None
    if m.group(2) and m.group(3) == "Z":
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc).astimezone(BRUSSELS).date()
    return datetime.strptime(m.group(1), "%Y%m%d").date()


def _parse_ical_dt(val):
    """iCal DTSTART/DTEND → Belgische datetime (UTC-tijden met Z worden omgezet).
    Zonder tijd (all-day) → None."""
    m = re.match(r"^(\d{8})(?:T(\d{6}))?(Z?)$", val.strip())
    if not m or not m.group(2):
        return None
    dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    if m.group(3) == "Z":
        return dt.replace(tzinfo=timezone.utc).astimezone(BRUSSELS)
    return dt.replace(tzinfo=BRUSSELS)


def get_shift_times(days_ahead=7):
    """Dict {datum: (start_dt, end_dt)} met de shift-uren binnen de komende
    werkdagen, of None bij een ophaalfout."""
    if not ICAL_URL:
        print("COMMUTY_ICAL_URL ontbreekt (env var of secret) — kan planning niet ophalen.")
        return None
    try:
        ical = with_retry(lambda: urlopen(ICAL_URL, timeout=15).read().decode("utf-8", errors="ignore"),
                          "iCal ophalen", attempts=3, delay=2)
    except Exception as e:
        print(f"Kon planning niet ophalen: {e}")
        return None

    today  = belgian_now().date()
    window = {today + timedelta(days=d) for d in range(1, days_ahead + 1)}

    shifts = {}
    in_event, event = False, {}
    for line in ical.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            in_event, event = True, {}
        elif line == "END:VEVENT":
            if in_event:
                d = _parse_ical_date(event.get("DTSTART", ""))
                if d and d in window:
                    shifts[d] = (_parse_ical_dt(event.get("DTSTART", "")),
                                 _parse_ical_dt(event.get("DTEND", "")))
            in_event = False
        elif in_event and ":" in line:
            key, _, val = line.partition(":")
            event[key.split(";")[0]] = val
    return shifts


# ── Google Calendar ─────────────────────────────────────────────
def get_google_token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    }, timeout=15)
    data = api_json(r, "Google token")
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Google gaf geen access_token: {str(data)[:200]}")
    return token


def _day_bounds(date):
    """Belgische dag → (timeMin, timeMax) met correcte zomer/winter-offset."""
    start = datetime(date.year, date.month, date.day, tzinfo=BRUSSELS)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _list_parking_events(date, google_token):
    """Alle 🅿️-events op een dag in de hoofdagenda."""
    t0, t1 = _day_bounds(date)
    r = requests.get(
        f"https://www.googleapis.com/calendar/v3/calendars/{quote(GOOGLE_CALENDAR_ID, safe='')}/events",
        headers={"Authorization": f"Bearer {google_token}"},
        params={"timeMin": t0, "timeMax": t1, "q": "🅿️", "singleEvents": "true"},
        timeout=15,
    )
    items = api_json(r, "Parking-events ophalen").get("items", [])
    return [ev for ev in items if (ev.get("summary") or "").strip().startswith("🅿️")]


def sync_google_calendar(token, org, google_token, extra=None):
    """Controleert elke run dat de 🅿️-events in Google Calendar overeenkomen met
    de echte reserveringen (site én tijd): ontbrekend event aanmaken, verkeerde
    site/tijd corrigeren, events zonder reservering verwijderen."""
    zones = dict(get_existing_requests(token, org))
    if extra:
        zones.update(extra)

    # Shift-begintijden uit het Commuty-rooster (dezelfde iCal die we al inlezen);
    # zo is er geen aparte Google-agenda (GOOGLE_ICAL_CALENDAR_ID) meer nodig.
    shift_starts = {d: se[0] for d, se in (get_shift_times(days_ahead=8) or {}).items()}

    today    = belgian_now().date()
    cal_base = f"https://www.googleapis.com/calendar/v3/calendars/{quote(GOOGLE_CALENDAR_ID, safe='')}/events"
    headers  = {"Authorization": f"Bearer {google_token}", "Content-Type": "application/json"}
    changes  = []

    for offset in range(0, 8):
        date = today + timedelta(days=offset)
        desired = zones.get(date)
        events = _list_parking_events(date, google_token)

        if not desired:
            for ev in events:
                requests.delete(f"{cal_base}/{ev['id']}", headers=headers, timeout=15)
                changes.append(f"{date.strftime('%d/%m')}: '{ev.get('summary')}' verwijderd (geen reservering die dag)")
            continue

        want_summary = f"🅿️ {desired}"
        shift_start  = shift_starts.get(date)
        want_start   = shift_start - timedelta(minutes=30) if shift_start else None

        if not events:
            if not shift_start:
                continue
            r = requests.post(cal_base, headers=headers, json={
                "summary":     want_summary,
                "start":       {"dateTime": want_start.isoformat(), "timeZone": "Europe/Brussels"},
                "end":         {"dateTime": shift_start.isoformat(), "timeZone": "Europe/Brussels"},
                "colorId":     "9",  # blauw
                "description": "Automatisch aangemaakt door Commuty parking script.",
            }, timeout=15)
            api_json(r, "Parking-event aanmaken")
            changes.append(f"{date.strftime('%d/%m')}: '{want_summary}' toegevoegd "
                           f"({want_start.strftime('%H:%M')}–{shift_start.strftime('%H:%M')})")
            continue

        keep, extras = events[0], events[1:]
        patch = {}
        if (keep.get("summary") or "").strip() != want_summary:
            patch["summary"] = want_summary
        if want_start:
            ev_start_raw = keep.get("start", {}).get("dateTime")
            if ev_start_raw and datetime.fromisoformat(ev_start_raw.replace("Z", "+00:00")) != want_start:
                patch["start"] = {"dateTime": want_start.isoformat(), "timeZone": "Europe/Brussels"}
                patch["end"]   = {"dateTime": shift_start.isoformat(), "timeZone": "Europe/Brussels"}
        if patch:
            r = requests.patch(f"{cal_base}/{keep['id']}", headers=headers, json=patch, timeout=15)
            api_json(r, "Parking-event corrigeren")
            was = (keep.get("summary") or "?").strip()
            changes.append(f"{date.strftime('%d/%m')}: '{was}' gecorrigeerd naar '{want_summary}'"
                           + (" (incl. tijd)" if "start" in patch else ""))
        for ev in extras:
            requests.delete(f"{cal_base}/{ev['id']}", headers=headers, timeout=15)
            changes.append(f"{date.strftime('%d/%m')}: dubbel parking-event verwijderd")
    return changes


# ── Telegram ────────────────────────────────────────────────────
def notify(title, message, error=False):
    """Pushmelding via Telegram. Mag zelf nooit een crash veroorzaken;
    valt terug op platte tekst als Markdown-parsing faalt."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"(geen Telegram geconfigureerd) {title}: {message}")
        return
    emoji = "❌" if error else "✅"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       f"{emoji} *{title}*\n{message}",
            "parse_mode": "Markdown",
        }, timeout=10)
        if not r.ok:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text":    f"{emoji} {title}\n{message}",
            }, timeout=10)
        if not r.ok:
            print(f"Telegram weigerde de melding: {r.status_code} {r.text[:150]}")
    except Exception as e:
        print(f"Melding kon niet verstuurd worden: {e}")


# ── Commando's ──────────────────────────────────────────────────
def cmd_login():
    """Access token vernieuwen en cachen (test dat het refresh token werkt)."""
    print(f"\n=== Login — {belgian_now().strftime('%d/%m/%Y %H:%M:%S')} (Belgische tijd) ===")
    get_access_token()
    print("Klaar, token gecached.")


def cmd_discover():
    """Verkent de Commuty-API met jouw account en print de structuren die het
    reserveerpad nodig heeft (organisatie, sites, credits, bestaande requests).
    Draai dit één keer na het instellen van je refresh token."""
    print(f"\n=== Discover — {belgian_now().strftime('%d/%m/%Y %H:%M:%S')} ===")
    token = get_access_token()

    print("\n--- memberships (organisatie) ---")
    try:
        org, user_id = get_organisation(token)
    except Exception as e:
        print(f"  memberships-call mislukt ({e}); val terug op COMMUTY_ORG={COMMUTY_ORG}")
        org, user_id = COMMUTY_ORG, None
    print(f"organisationExternalId = {org}")
    print(f"userId                 = {user_id}")

    print("\n--- parking-sites ---")
    sites = get_parking_sites(token, org)
    for s in sites:
        print(f"  id={s['id']}  naam={s['name']}")
    print(f"(zet COMMUTY_PARKING_SITES op je voorkeursvolgorde, bv. "
          f"\"{','.join(s['name'] for s in sites[:4])}\")")

    # Brede probe: probeer een reeks kandidaat-endpoints en toon per stuk de
    # HTTP-status en een stukje van de body. Zo zien we welke paden werken en
    # hoe een echte reservering eruitziet (maak er vooraf één in de app!).
    today = belgian_now().date()
    to    = today + timedelta(days=21)
    d0, d1 = today.isoformat(), to.isoformat()
    site_id = sites[0]["id"] if sites else "996"
    # 'activities' is het planner-endpoint (lezen én schrijven). We tonen gericht
    # de parking-velden van elke activiteit (het grote transport-object laten we
    # weg), zodat templateId/period/parkingSite/allocation zichtbaar worden.
    print("\n--- activities (parking-velden per activiteit) ---")
    try:
        r = requests.get(f"{API_BASE}{org}/activities", headers=_auth_headers(token),
                         params={"startDate": d0, "endDate": d1}, timeout=20)
        print(f"[{r.status_code}] GET activities?startDate={d0}&endDate={d1}")
        items = r.json() if r.ok else []
        for it in (items if isinstance(items, list) else []):
            slim = {k: it.get(k) for k in ("id", "startTime", "endTime", "isAllDay",
                                           "type", "title", "status", "state")}
            slim["parkingSpotRequest"] = it.get("parkingSpotRequest")
            slim["parkingSpotAllocation"] = it.get("parkingSpotAllocation")
            slim["timeslot"] = it.get("timeslot")
            print(json.dumps(slim, ensure_ascii=False, default=str))
    except Exception as e:
        print(f"[ERR] activities: {e}")

    print("\n--- parking-sites: fragmenten rond 'template'/'timeslot' ---")
    try:
        r = requests.get(f"{API_BASE}{org}/parking-sites", headers=_auth_headers(token), timeout=20)
        raw = r.text
        low = raw.lower()
        hits = []
        for kw in ("template", "timeslot"):
            i = 0
            while True:
                j = low.find(kw, i)
                if j < 0:
                    break
                hits.append((max(0, j - 60), j + 200))
                i = j + 1
        if hits:
            for a, b in hits[:15]:
                print("…" + raw[a:b].replace("\n", " ") + "…")
        else:
            print("(geen 'template'/'timeslot' in parking-sites; eerste 800 tekens:)")
            print(raw[:800])
    except Exception as e:
        print(f"[ERR] parking-sites: {e}")

    print("\n--- context (ingekort) ---")
    for path, limit in [("budget-transactions", 800), ("me", 1200)]:
        try:
            r = requests.get(f"{API_BASE}{org}/{path}", headers=_auth_headers(token), timeout=20)
            print(f"\n[{r.status_code}] GET {path}\n{r.text.replace(chr(10),' ')[:limit]}")
        except Exception as e:
            print(f"\n[ERR] GET {path}: {e}")


def cmd_book():
    """Testmodus: reserveert één dag en dumpt de volledige request + response.
    Bedoeld om het reserveerformaat te verifiëren. Datum via env BOOK_DATE
    (YYYY-MM-DD), anders de eerstvolgende werkdag. Site via COMMUTY_PARKING_SITES
    of de eerste beschikbare."""
    print(f"\n=== Book-test — {belgian_now().strftime('%d/%m/%Y %H:%M:%S')} ===")
    token = get_access_token()
    org = resolve_org(token)
    sites = ordered_sites(get_parking_sites(token, org))
    if not sites:
        print("Geen sites."); return
    site = sites[0]

    bd = os.environ.get("BOOK_DATE", "").strip()
    if bd:
        date = datetime.strptime(bd, "%Y-%m-%d").date()
    else:
        date = belgian_now().date() + timedelta(days=1)
        while date.weekday() >= 5:
            date += timedelta(days=1)

    user_id = get_user_id(token, org)
    shifts = get_shift_times(days_ahead=21) or {}
    print(f"Credits vooraf: {get_credit_balance(token, org)}  |  user id: {user_id}")
    start_ts, end_ts = _reservation_window(date, shifts.get(date))
    body = _activities_body(user_id, site["id"], start_ts, end_ts)
    print(f"\nPOST {API_BASE}{org}/activities  (site {site['id']} {site['name']}, {date})")
    print("REQUEST BODY:\n" + json.dumps(body, ensure_ascii=False))
    r = requests.post(f"{API_BASE}{org}/activities",
                      headers={**_auth_headers(token), "Content-Type": "application/json"},
                      json=body, timeout=20)
    print(f"\nRESPONSE [{r.status_code}]:\n{r.text[:2000]}")
    print(f"\nCredits nadien: {get_credit_balance(token, org)}")


def cmd_cancel():
    """Annuleert de (niet-geannuleerde) parkeerreservering op een dag. Datum via
    env BOOK_DATE (YYYY-MM-DD, verplicht)."""
    print(f"\n=== Cancel — {belgian_now().strftime('%d/%m/%Y %H:%M:%S')} ===")
    bd = os.environ.get("BOOK_DATE", "").strip()
    if not bd:
        print("Geef een datum via BOOK_DATE (YYYY-MM-DD)."); return
    date = datetime.strptime(bd, "%Y-%m-%d").date()

    token = get_access_token()
    org = resolve_org(token)
    print(f"Credits vooraf: {get_credit_balance(token, org)}")

    items = _activities(token, org, date, date + timedelta(days=1))
    target = None
    for it in (items if isinstance(items, list) else []):
        if (it.get("startTime") or "")[:10] != date.isoformat():
            continue
        if (it.get("title") or "").strip().lower() == "geannuleerd":
            continue
        if it.get("parkingSpotRequest"):
            target = it
            break
    if not target:
        print(f"Geen actieve reservering gevonden op {date}."); return

    cid = target["id"]
    print(f"Annuleren: commute {cid} — '{target.get('title')}' ({date})")
    r = requests.delete(f"{API_BASE}{org}/commutes/{cid}",
                        headers=_auth_headers(token),
                        params={"onlyMe": "true", "withFutureOccurences": "false"},
                        timeout=20)
    print(f"RESPONSE [{r.status_code}]: {r.text[:300]}")
    print(f"Credits nadien: {get_credit_balance(token, org)}")


def _in_primary_window():
    """True voor de dagelijkse hoofd-run (heartbeat-venster); catch-up runs
    later op de dag blijven stil als er niets te doen valt."""
    now = belgian_now()
    return now.hour == 14 and now.minute <= 15


def cmd_reserve():
    """Reserveringen aanmaken voor de shiftdagen in de komende 7 werkdagen."""
    now = belgian_now()
    print(f"\n=== Reserveringen — {now.strftime('%d/%m/%Y %H:%M:%S')} (Belgische tijd) ===")

    token = get_access_token()
    org = resolve_org(token)
    sites = get_parking_sites(token, org)
    user_id = get_user_id(token, org)
    print(f"Organisatie {org} — {len(sites)} parking-site(s): {', '.join(s['name'] for s in ordered_sites(sites))}"
          f" — user {user_id}")

    # Planning
    print("\nPlanning controleren (komende 7 dagen)...")
    shifts = get_shift_times(days_ahead=7)
    today    = belgian_now().date()
    days = [today + timedelta(days=d) for d in range(1, 8)]

    if shifts is not None:
        to_reserve = sorted(d for d in days if d in shifts)
        print(f"  Shiftdagen: {', '.join(d.strftime('%a %d/%m') for d in to_reserve) or '—'}")
    else:
        shifts = {}
        notify("⚠️ Planning onbereikbaar",
               "Kon de VRT-planning (iCal) niet ophalen. Als fallback wordt voor ALLE "
               "dagen gereserveerd — annuleer overbodige dagen manueel.", error=True)
        to_reserve = days

    # Bestaande reserveringen
    try:
        existing = get_existing_requests(token, org)
        if existing:
            print(f"Al gereserveerd: {sorted(existing)}")
    except Exception as e:
        print(f"Kon bestaande reserveringen niet ophalen ({e}) — ga door, dubbels worden geweigerd.")
        existing = {}

    needed = [d for d in to_reserve if d not in existing]

    # Credit-plafond: een credit komt pas vrij bij het binnenrijden, dus nooit
    # meer OPENSTAANDE reserveringen dan er credits vrij zijn. We snijden de lijst
    # NIET vooraf af: we proberen alle shiftdagen in volgorde en stoppen zodra we
    # zoveel geslaagde boekingen hebben als er credits waren. Zo blokkeert een
    # dag die (nog) niet boekbaar is geen latere dag die dat wél is.
    credits = get_credit_balance(token, org)
    budget = credits if credits is not None else len(needed)

    new_reservations = []
    if not needed:
        print("Niets te reserveren.")
        if _in_primary_window():
            done = ", ".join(d.strftime("%d/%m") for d in sorted(d for d in to_reserve if d in existing)) or "—"
            msg = "Geen shifts in de komende 7 dagen." if not to_reserve \
                  else f"Alle shiftdagen al gereserveerd: {done}"
            notify("🅿️ Dagelijkse check OK", msg)
    else:
        if credits is not None and len(needed) > budget:
            print(f"\n⚠️ {len(needed)} shiftdagen te reserveren, {budget} credit(s) vrij — "
                  f"we boeken er maximaal {budget}; de rest volgt zodra een credit vrijkomt.")
        print("\nReserveringen aanmaken...")
        failed = []
        booked = 0
        for date in needed:
            if booked >= budget:
                print(f"  {date}: uitgesteld — geen vrije credits meer deze run")
                continue
            try:
                status, site = reserve_for_date(token, org, date, sites, user_id, shifts.get(date))
            except Exception as e:
                print(f"  {date}: fout — {e}")
                failed.append((date, f"fout: {e}"))
                continue
            if status == "ok":
                new_reservations.append((date, site))
                booked += 1
            elif status == "no_credit":
                failed.append((date, "onvoldoende credits"))
                break  # zonder credits heeft verder proberen geen zin
            elif status == "unavailable":
                failed.append((date, "geen plek beschikbaar (vol)"))
            elif status == "failed":
                failed.append((date, "alle sites geweigerd"))
            # 'not_open' en 'existing' → stil overslaan, latere run pikt ze op

        # Hercheck: een parallelle/eerdere run kan de plek intussen al geboekt hebben
        if failed:
            try:
                fresh = get_existing_requests(token, org)
                failed = [(d, why) for d, why in failed if d not in fresh]
            except Exception:
                pass

        if new_reservations:
            lines = "\n".join(f"  {d.strftime('%a %d/%m')} — {z}" for d, z in new_reservations)
            notify("🅿️ Parking gereserveerd", f"Nieuwe reserveringen:\n{lines}")
        if failed:
            lines = "\n".join(f"  {d.strftime('%a %d/%m')} — {why}" for d, why in failed)
            notify("⚠️ Parking mislukt",
                   f"Geen plek voor:\n{lines}\nLatere runs blijven het automatisch proberen "
                   f"(annulaties worden opgepikt) — of check Commuty manueel.", error=True)
        if not new_reservations and not failed:
            print("Geen nieuwe reserveringen nodig.")

    # Google Calendar synchroniseren — als laatste en volledig afgeschermd.
    if not GOOGLE_REFRESH_TOKEN:
        return
    print("\nGoogle Calendar controleren...")
    try:
        google_token = get_google_token()
        changes = sync_google_calendar(token, org, google_token, dict(new_reservations))
        if changes:
            for c in changes:
                print(f"  {c}")
            notify("📅 Agenda bijgewerkt", "\n".join(changes))
        else:
            print("  Agenda klopt met de reserveringen.")
    except Exception as e:
        print(f"  Agenda-sync mislukt: {e}")
        if _in_primary_window() or os.environ.get("GITHUB_EVENT_NAME") != "schedule":
            msg = str(e)
            # Een verlopen/ingetrokken Google-token is de meest voorkomende oorzaak
            # (OAuth-app in 'Testing' laat refresh tokens na 7 dagen verlopen). Dat
            # raakt ENKEL de agenda-sync — het reserveren loopt gewoon door. Maak de
            # melding daarom ondubbelzinnig zodat ze niet als een reservatiefout leest.
            if "invalid_grant" in msg or "expired or revoked" in msg:
                notify("📅 Alleen Google-agenda ligt stil — reserveren werkt gewoon door",
                       "Je parking wordt nog steeds automatisch gereserveerd. Enkel het "
                       "bijwerken van de Google-agenda is gestopt omdat de Google-token verlopen "
                       "is. Genereer een nieuwe GOOGLE_REFRESH_TOKEN en publiceer de OAuth-app "
                       "naar 'In production' zodat de token niet meer om de 7 dagen verloopt.",
                       error=True)
            else:
                notify("📅 Google-agenda niet bijgewerkt — reserveren werkt gewoon door",
                       f"Het reserveren zelf is OK; enkel de agenda-sync faalde: {msg[:200]}",
                       error=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--reserve"
    try:
        if mode == "--login":
            cmd_login()
        elif mode == "--discover":
            cmd_discover()
        elif mode == "--book":
            cmd_book()
        elif mode == "--cancel":
            cmd_cancel()
        else:
            cmd_reserve()
    except SystemExit:
        raise
    except Exception as e:
        print(traceback.format_exc())
        notify("Parking-script gecrasht",
               f"Mode {mode}: {type(e).__name__}: {str(e)[:300]}\nCheck de logs en reserveer manueel.",
               error=True)
        sys.exit(1)
