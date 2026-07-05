#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
 BOT DE SURVEILLANCE LOGEMENT CROUS  ->  NOTIFICATION TELEGRAM   (v7)
 Cible : rayon autour de l'ESIEA (Ivry)

 CE FICHIER MARCHE AUX DEUX ENDROITS, il s'adapte tout seul :
   * en LOCAL (sur ton PC) : boucle continue + tableau de bord localhost:8000
   * sur GITHUB ACTIONS     : une seule verif puis il quitte (relance auto
     par GitHub toutes les X min). Les identifiants Telegram sont lus depuis
     les "Secrets" GitHub (variables d'environnement).

 Local     :  pip install requests beautifulsoup4  puis  python bot-telegram.py
 GitHub    :  gere par le fichier .github/workflows/crous.yml
==============================================================================
"""

import time
import json
import os
import re
import sys
import math
import threading
import http.server
import socketserver
import html as html_mod
from collections import deque

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Il manque une librairie. Lance :  pip install beautifulsoup4")
    sys.exit(1)


# =============================================================================
#  CONFIG
# =============================================================================

# Identifiants Telegram :
#  - sur GitHub : mets-les dans les Secrets (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
#  - en local   : soit tu remplis ci-dessous, soit tu laisses (les Secrets priment)
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")   or "COLLE_TON_TOKEN_BOTFATHER_ICI"
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "COLLE_TON_CHAT_ID_ICI"

CROUS_COOKIE = ""   # laisse vide

TOOL_ID = 45

# --- LE CERCLE AUTOUR DE L'ECOLE ---
ECOLE = {"lat": 48.8126, "lon": 2.3872, "nom": "ESIEA Ivry"}
RAYON_KM = 5          # rayon de recherche autour de l'ecole (km)

OCCUPATION_MODE = "alone"   # "alone" = individuel ; "" = tout inclure

CHECK_INTERVAL = 150      # (local) secondes entre 2 verifs
MAX_PAGES = 25            # securite : nb max de pages parcourues
DASHBOARD_PORT = 8000     # (local) http://localhost:8000

APERCU_AU_DEMARRAGE = False   # (local) True 1 fois pour voir le rendu

# =============================================================================
#  Rien a toucher en dessous
# =============================================================================

IS_GITHUB = os.getenv("GITHUB_ACTIONS") == "true"   # detection auto du mode

BASE = "https://trouverunlogement.lescrous.fr"
SEARCH_URL = f"{BASE}/tools/{TOOL_ID}/search"
SEEN_FILE = "logements_vus.json"
esc = html_mod.escape


def bounds_from_center(lat, lon, rayon_km):
    dlat = rayon_km / 111.0
    dlon = rayon_km / (111.320 * math.cos(math.radians(lat)))
    return (f"{lon - dlon:.7f}_{lat + dlat:.7f}_"
            f"{lon + dlon:.7f}_{lat - dlat:.7f}")


SEARCH_PARAMS = {
    "bounds": bounds_from_center(ECOLE["lat"], ECOLE["lon"], RAYON_KM),
    "locationName": f"Autour de {ECOLE['nom']} ({RAYON_KM} km)",
}
if OCCUPATION_MODE:
    SEARCH_PARAMS["occupationModes"] = OCCUPATION_MODE

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
_cookie = CROUS_COOKIE.strip().replace("\n", " ").replace("\r", " ")
if _cookie:
    HEADERS["Cookie"] = _cookie

STATE = {
    "last_check": "jamais", "count": 0, "listings": [],
    "logs": deque(maxlen=50), "next_check_ts": time.time(), "pages": 1,
}


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line)
    STATE["logs"].appendleft(line)


def full_link(href):
    return href if href.startswith("http") else BASE + href


def clean(s):
    return " ".join(s.split())


# =============================================================================
#  Telegram
# =============================================================================
def _tg_ready():
    return "COLLE_TON" not in TELEGRAM_TOKEN and "COLLE_TON" not in TELEGRAM_CHAT_ID


def send_message(text, buttons=None):
    if not _tg_ready():
        log("[Telegram] identifiants absents (Secrets GitHub ou config locale).")
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json=payload, timeout=15)
        if r.status_code != 200:
            log(f"[Telegram] {r.status_code} : {r.text[:150]}")
    except Exception as e:
        log(f"[Telegram] erreur : {e}")


def send_photo(photo_url, caption, buttons=None):
    if not _tg_ready():
        return
    payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url,
               "caption": caption, "parse_mode": "HTML"}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                          json=payload, timeout=15)
        if r.status_code != 200:
            send_message(caption, buttons)
    except Exception as e:
        log(f"[Telegram] photo erreur : {e}")
        send_message(caption, buttons)


def build_caption(x, entete):
    lignes = [entete, "", f"🏠 <b>{esc(x['titre'])}</b>"]
    if x.get("prix"):
        lignes.append(f"💶 {esc(x['prix'])} / mois")
    l2 = " · ".join(p for p in [x.get("surface", ""), x.get("type", "")] if p)
    if l2:
        lignes.append(f"📐 {esc(l2)}")
    if x.get("lit"):
        lignes.append(f"🛏️ {esc(x['lit'])}")
    if x.get("equip"):
        lignes.append(f"🚿 {esc(x['equip'])}")
    if x.get("adresse"):
        lignes.append(f"📍 {esc(x['adresse'])}")
    if x.get("tres_demande"):
        lignes.append("🔥 Logement très demandé !")
    return "\n".join(lignes)


def notifier_logement(x, entete):
    caption = build_caption(x, entete)
    buttons = [[{"text": "👉 Voir / Réserver le logement", "url": full_link(x["lien"])}]]
    if x.get("image"):
        send_photo(x["image"], caption, buttons)
    else:
        send_message(caption, buttons)


# =============================================================================
#  Recuperation + parsing (avec pagination)
# =============================================================================
def fetch_page(page):
    params = dict(SEARCH_PARAMS)
    params["page"] = page
    r = requests.get(SEARCH_URL, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()
    return r.text


def parse_total(html):
    m = re.search(r"(\d+)\s+logements?\s+trouv", html)
    return int(m.group(1)) if m else None


def parse_nb_pages(html):
    m = re.search(r"page\s+\d+\s+sur\s+(\d+)", html)
    return int(m.group(1)) if m else 1


def fetch_all():
    html1 = fetch_page(1)
    total_count = parse_total(html1)
    nb_pages = min(parse_nb_pages(html1), MAX_PAGES)

    listings, ids = [], set()
    for p in range(1, nb_pages + 1):
        html = html1 if p == 1 else fetch_page(p)
        for x in parse_listings(html):
            if x["id"] not in ids:
                ids.add(x["id"])
                listings.append(x)
        if p < nb_pages:
            time.sleep(1)
    return listings, total_count, nb_pages


def parse_listings(html):
    soup = BeautifulSoup(html, "html.parser")
    listings, vus = [], set()

    for a in soup.select('a[href*="/accommodations/"]'):
        href = a.get("href", "")
        if not href or href in vus:
            continue
        vus.add(href)

        titre = clean(a.get_text(" ")) or "Logement CROUS"
        acc_id = href.rstrip("/").split("/")[-1]

        card = None
        node = a
        for _ in range(8):
            node = node.parent
            if node is None:
                break
            if node.name in ("li", "article", "div") and "€" in node.get_text():
                card = node
                break
        if card is None:
            card = a.parent

        card_text = clean(card.get_text(" "))

        prix = ""
        mp = re.search(r"(\d[\d\s]*)\s*€", card_text)
        if mp:
            prix = mp.group(1).replace(" ", "") + " €"

        adresse = ""
        for s in card.stripped_strings:
            s2 = clean(s)
            if re.match(r"^\d{1,4}\s", s2) and re.search(r"\b\d{5}\b", s2):
                adresse = s2
                break
        if not adresse:
            madr = re.search(r"\d{1,4}\s+[^\d]{2,60}?\d{5}\s+[A-Za-zÀ-ÿ'’\-]+", card_text)
            if madr:
                adresse = clean(madr.group(0))

        tres_demande = "très demandé" in card_text.lower()

        surface = typ = lit = equip = ""
        for li in card.find_all("li"):
            t = clean(li.get_text(" "))
            if not t or "€" in t:
                continue
            low = t.lower()
            if "m²" in t and not surface:
                surface = t
            elif re.search(r"\blits?\b", low) and not lit:
                lit = t
            elif re.match(r"^(individuel|couple|colocation)", low) and not typ:
                typ = t
            elif (not equip and any(w in t for w in
                  ["WC", "Douche", "Frigo", "plaque", "Pièce", "Evier",
                   "Lavabo", "Kitchenette", "Balcon", "Lave"])):
                equip = t

        if not surface:
            ms = re.search(r"(de\s+\d+\s+à\s+\d+|\d+)\s*m²", card_text)
            if ms:
                surface = ms.group(0)
        if not typ:
            mt = re.search(r"\b(Individuel(?:,\s*Couple)?|Couple|Colocation)\b", card_text)
            if mt:
                typ = mt.group(0)
        if not lit:
            ml = re.search(r"\d+\s+lits?\s+\w+", card_text)
            if ml:
                lit = ml.group(0)

        img_url = ""
        srcs = [im.get("src", "") for im in card.find_all("img") if im.get("src")]
        for u in srcs:
            if f"/{acc_id}/" in u:
                img_url = u
                break
        if not img_url and srcs:
            img_url = srcs[-1]

        listings.append({
            "id": href, "titre": titre, "lien": href, "prix": prix,
            "adresse": adresse, "surface": surface, "type": typ, "lit": lit,
            "equip": equip, "tres_demande": tres_demande, "image": img_url,
        })
    return listings


def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


# =============================================================================
#  Coeur : une verification (utilisee en local ET sur GitHub)
# =============================================================================
def process(seen):
    listings, total, nb_pages = fetch_all()
    STATE["listings"] = listings
    STATE["count"] = total if total is not None else len(listings)
    STATE["pages"] = nb_pages
    STATE["last_check"] = time.strftime("%H:%M:%S")
    log(f"{STATE['count']} logement(s) sur {nb_pages} page(s).")

    if not seen:
        # tout premier passage : on enregistre la base sans spammer
        for x in listings:
            seen.add(x["id"])
        save_seen(seen)
        send_message(
            f"✅ <b>Bot CROUS activé</b>\n"
            f"Zone : {RAYON_KM} km autour de {esc(ECOLE['nom'])}\n"
            f"Je surveille {STATE['count']} logement(s). Notif <b>uniquement</b> "
            f"quand un NOUVEAU apparaît. 🔔"
        )
        log("Baseline enregistree.")
        return 0

    news = [x for x in listings if x["id"] not in seen]
    for x in news:
        notifier_logement(x, "🚨 <b>NOUVEAU logement CROUS !</b>")
        log(f"NOTIF nouveau : {x['titre']} {x.get('prix','')}")
        seen.add(x["id"])
    save_seen(seen)
    return len(news)


# =============================================================================
#  Tableau de bord (LOCAL uniquement)
# =============================================================================
PAGE_HEAD = """<!doctype html><html lang="fr"><head>
<meta charset="utf-8"><meta http-equiv="refresh" content="3">
<title>Bot CROUS - live</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#0f1420;color:#e6e9ef;margin:0;padding:24px}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:#8a93a6;font-size:13px;margin-bottom:18px}
 .grid{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:6px}
 .card{background:#1a2233;border:1px solid #26304a;border-radius:12px;padding:14px 18px;min-width:140px}
 .card .k{color:#8a93a6;font-size:12px}
 .card .v{font-size:22px;font-weight:600;margin-top:4px}
 .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:13px;font-weight:600;background:#123524;color:#4ade80}
 h2{font-size:15px;margin:22px 0 10px;color:#c7cede}
 ul{list-style:none;padding:0;margin:0}
 li{background:#1a2233;border:1px solid #26304a;border-radius:8px;padding:12px 14px;margin-bottom:8px}
 li a{color:#7dd3fc;text-decoration:none;font-weight:600} li a:hover{text-decoration:underline}
 .meta{color:#9aa4b8;font-size:13px;margin-top:4px}
 .prix{color:#4ade80;font-weight:600}
 .empty{color:#8a93a6}
 pre{background:#0b0f18;border:1px solid #26304a;border-radius:10px;padding:14px;font-size:12px;
     line-height:1.6;color:#a7b0c2;max-height:260px;overflow:auto;white-space:pre-wrap}
</style></head><body>
<h1>🏠 Bot CROUS — en direct</h1>
"""
PAGE_TAIL = "</body></html>"


def render_page():
    remaining = max(0, int(STATE["next_check_ts"] - time.time()))
    sub = (f'<div class="sub">Auto-rafraichi toutes les 3 s • '
           f'rayon {RAYON_KM} km autour de {esc(ECOLE["nom"])}</div>')
    cards = (
        '<div class="grid">'
        '<div class="card"><div class="k">Etat</div><div class="v"><span class="badge">actif</span></div></div>'
        f'<div class="card"><div class="k">Logements</div><div class="v">{STATE["count"]}</div></div>'
        f'<div class="card"><div class="k">Pages</div><div class="v">{STATE["pages"]}</div></div>'
        f'<div class="card"><div class="k">Derniere verif</div><div class="v">{STATE["last_check"]}</div></div>'
        f'<div class="card"><div class="k">Prochaine</div><div class="v">{remaining}s</div></div>'
        '</div>'
    )
    if STATE["listings"]:
        rows = ""
        for x in STATE["listings"]:
            meta = " · ".join(p for p in [x.get("surface", ""), x.get("type", ""),
                                          x.get("adresse", "")] if p)
            rows += (f'<li><a href="{full_link(x["lien"])}" target="_blank">{esc(x["titre"])}</a>'
                     f'<div class="meta"><span class="prix">{esc(x.get("prix",""))}</span> — '
                     f'{esc(meta)}</div></li>')
    else:
        rows = '<li class="empty">Aucun logement en ligne pour le moment. Je surveille…</li>'
    logs_txt = "\n".join(esc(l) for l in STATE["logs"])
    return (PAGE_HEAD + sub + cards
            + "<h2>Logements actuellement en ligne</h2><ul>" + rows + "</ul>"
            + "<h2>Journal (live)</h2><pre>" + logs_txt + "</pre>" + PAGE_TAIL)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = render_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_dashboard():
    try:
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", DASHBOARD_PORT), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"Tableau de bord : http://localhost:{DASHBOARD_PORT}")
    except Exception as e:
        log(f"Tableau de bord indisponible : {e}")


# =============================================================================
#  Points d'entree
# =============================================================================
def run_once():
    """Mode GitHub Actions : une seule verification puis on quitte."""
    print("=== Bot CROUS — verification unique (GitHub Actions) ===")
    seen = load_seen()
    try:
        n = process(seen)
        log(f"Verification terminee : {n} nouveau(x) logement(s).")
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        log(f"HTTP {code} pendant la verification.")
    except Exception as e:
        log(f"Erreur : {e}")


def run_loop():
    """Mode local : boucle continue + tableau de bord."""
    print("=== Bot CROUS (v7) — mode local ===")
    print(f"Zone : rayon {RAYON_KM} km autour de {ECOLE['nom']}")
    start_dashboard()
    print(f"-> Ouvre http://localhost:{DASHBOARD_PORT}\n")

    seen = load_seen()
    apercu_envoye = False
    while True:
        try:
            if APERCU_AU_DEMARRAGE and not apercu_envoye:
                lst, _, _ = fetch_all()
                send_message(f"👀 <b>Aperçu — {len(lst)} logement(s)</b>")
                for x in lst:
                    notifier_logement(x, "🏠 <b>Disponible</b>")
                apercu_envoye = True
            process(seen)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            log(f"HTTP {code} — si 429 : augmente CHECK_INTERVAL.")
        except Exception as e:
            log(f"Erreur : {e}")

        STATE["next_check_ts"] = time.time() + CHECK_INTERVAL
        for _ in range(CHECK_INTERVAL):
            time.sleep(1)


if __name__ == "__main__":
    if IS_GITHUB:
        run_once()
    else:
        run_loop()
