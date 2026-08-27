#!/usr/bin/env python3
"""
Eenmalig: een Commuty *offline* refresh token ophalen.

Commuty-login loopt via Microsoft-SSO (met MFA), wat een script niet veilig
headless kan nabootsen. In de plaats logt dit script één keer in via je eigen
browser — waar je al bij Commuty ingelogd bent, dus zonder MFA-herhaling — en
ruilt de authorization code voor een langlevend offline refresh token. Dat token
zet je als COMMUTY_REFRESH_TOKEN; reserve_parking.py vernieuwt zichzelf ermee
zonder ooit nog langs Microsoft te moeten.

Gebruikt alleen de standaardbibliotheek — geen 'pip install' nodig.

Gebruik:
    python get_commuty_token.py
"""

import os, re, json, base64, hashlib, sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

KC_BASE      = "https://auth.commuty.net/auth"
KC_REALM     = "commuty"
KC_CLIENT_ID = "commuty-web"
KC_REDIRECT  = "https://app.commuty.net/"
KC_AUTH_URL  = f"{KC_BASE}/realms/{KC_REALM}/protocol/openid-connect/auth"
KC_TOKEN_URL = f"{KC_BASE}/realms/{KC_REALM}/protocol/openid-connect/token"

BASE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(BASE, "secrets.local.json")


def pkce_pair():
    verifier  = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def post_token(data):
    """POST naar het token-endpoint; geeft (ok, json_of_tekst)."""
    req = Request(KC_TOKEN_URL, data=urlencode(data).encode(),
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urlopen(req, timeout=20) as r:
            return True, json.loads(r.read().decode())
    except HTTPError as e:
        return False, e.read().decode()[:300]
    except Exception as e:
        return False, str(e)


def main():
    verifier, challenge = pkce_pair()
    auth_url = KC_AUTH_URL + "?" + urlencode({
        "client_id":             KC_CLIENT_ID,
        "response_type":         "code",
        "redirect_uri":          KC_REDIRECT,
        "scope":                 "openid offline_access",
        "state":                 base64.urlsafe_b64encode(os.urandom(9)).decode(),
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    })

    print("\n" + "=" * 70)
    print("STAP 1 — Open deze URL in de browser waar je bij Commuty ingelogd bent:")
    print("=" * 70)
    print("\n" + auth_url + "\n")
    print("Je wordt (zonder opnieuw in te loggen) doorgestuurd naar een pagina die")
    print("begint met  https://app.commuty.net/?...&code=....")
    print("\nSTAP 2 — Kopieer de VOLLEDIGE URL uit de adresbalk zodra hij verschijnt")
    print("(of enkel de waarde na 'code=') en plak die hieronder.")
    print("Tip: lukt het niet in één keer (de app 'verbruikt' de code soms)?")
    print("     Herlaad gewoon de URL uit stap 1 — er komt telkens een verse code.\n")

    pasted = input("Geplakte URL of code: ").strip()
    m = re.search(r'[?&#]code=([^&\s]+)', pasted)
    code = m.group(1) if m else pasted
    if not code:
        sys.exit("Geen code gevonden in de invoer.")

    ok, tokens = post_token({
        "grant_type":    "authorization_code",
        "client_id":     KC_CLIENT_ID,
        "code":          code,
        "redirect_uri":  KC_REDIRECT,
        "code_verifier": verifier,
    })
    if not ok:
        sys.exit(f"Token-uitwisseling mislukt: {tokens}\n"
                 "Meestal betekent dit dat de code al verbruikt/verlopen is — "
                 "herhaal stap 1 met een verse code.")
    refresh = tokens.get("refresh_token")
    if not refresh:
        sys.exit(f"Geen refresh token ontvangen: {json.dumps(tokens)[:300]}")

    # Verifieer dat het een bruikbaar (offline) token is via één refresh-ronde.
    vok, _ = post_token({
        "grant_type":    "refresh_token",
        "client_id":     KC_CLIENT_ID,
        "refresh_token": refresh,
        "scope":         "openid offline_access",
    })
    print("\n" + "=" * 70)
    print("✅ Refresh token opgehaald." if vok else "⚠️ Token opgehaald maar refresh-test faalde.")
    print("=" * 70)
    print(f"\nrefresh_expires_in = {tokens.get('refresh_expires_in', 0)}  (0 = niet-verlopend / offline)\n")
    print("COMMUTY_REFRESH_TOKEN (kopieer de volledige regel hieronder):\n")
    print(refresh + "\n")

    if os.path.exists(SECRETS):
        try:
            data = json.load(open(SECRETS))
        except Exception:
            data = {}
        data["COMMUTY_REFRESH_TOKEN"] = refresh
        json.dump(data, open(SECRETS, "w"), indent=2)
        print(f"Ook opgeslagen in {SECRETS}.")
    print("\nZet dit als GitHub-secret COMMUTY_REFRESH_TOKEN "
          "(Settings → Secrets → Actions).")


if __name__ == "__main__":
    main()
