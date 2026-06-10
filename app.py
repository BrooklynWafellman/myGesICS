#!/usr/bin/env python3
"""
MyGES / Skolae Calendar — Vercel Serverless Endpoint
GET /api/calendar  →  retourne le fichier .ics en direct, sans S3 ni cron.
"""

import os
import base64
import calendar
import logging
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler

import requests
import pytz
from icalendar import Calendar, Event

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("MyGESCalendar")

# ---------------------------------------------------------------------------
# OkHttp headers — contourne le WAF Kordis
# ---------------------------------------------------------------------------
OKHTTP_HEADERS = {
    "User-Agent": "okhttp/3.13.1",
    "Connection": "Keep-Alive",
    "Accept-Encoding": "gzip",
}


def _create_session() -> requests.Session:
    session = requests.Session()
    session.headers.clear()
    session.headers.update(OKHTTP_HEADERS)
    return session


def _login(session: requests.Session, username: str, password: str) -> str:
    """OAuth 2.0 implicit flow → Bearer token."""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    response = session.get(
        "https://authentication.kordis.fr/oauth/authorize",
        params={"response_type": "token", "client_id": "skolae-app"},
        headers={"Authorization": f"Basic {credentials}"},
        allow_redirects=False,
        timeout=15,
    )

    if response.status_code in (401, 403):
        raise ValueError(f"Authentification refusée (HTTP {response.status_code}). Vérifiez vos identifiants.")

    location = response.headers.get("Location", "")
    if "access_token" not in location:
        raise ValueError(f"Token introuvable dans la redirection (HTTP {response.status_code}).")

    fragment = location.split("#")[1]
    token_data = dict(pair.split("=") for pair in fragment.split("&"))
    return token_data["access_token"]


def _get_agenda(session: requests.Session, token: str, start: datetime, end: datetime) -> list:
    """Récupère les événements entre start et end."""
    response = session.get(
        "https://api.kordis.fr/me/agenda",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
        },
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()
    if "result" not in data:
        code = data.get("response_code") or data.get("responseCode")
        exc = data.get("exception", "Erreur inconnue")
        raise ValueError(f"Erreur API Skolae : code {code} — {exc}")

    return data["result"]


def _sync_range():
    """Plage : 1 mois en arrière → 4 mois en avant (pour conserver un peu d'historique)."""
    now = datetime.now(pytz.utc)
    start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)

    month = now.month - 1 + 4
    year = now.year + month // 12
    month = month % 12 + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    end = datetime(year, month, day, 23, 59, 59, tzinfo=pytz.utc)

    return start, end


def _build_ical(entries: list) -> bytes:
    """Convertit la liste d'événements Skolae en fichier iCal."""
    cal = Calendar()
    cal.add("prodid", "-//MyGES Skolae Calendar//FR")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "GES Agenda")
    cal.add("x-wr-timezone", "Europe/Paris")
    cal.add("refresh-interval;value=duration", "P1D")  # rafraîchissement tout les jours
    cal.add("x-published-ttl", "P1D")

    for entry in entries:
        reservation_id = entry.get("reservation_id")
        if not reservation_id:
            continue

        event = Event()
        event.add("uid", f"skolae-{reservation_id}@mygescalendar")
        event.add("summary", entry.get("name", "Cours"))

        start_dt = datetime.fromtimestamp(entry["start_date"] / 1000.0, tz=pytz.utc)
        end_dt = datetime.fromtimestamp(entry["end_date"] / 1000.0, tz=pytz.utc)
        event.add("dtstart", start_dt)
        event.add("dtend", end_dt)

        # Lieu
        rooms = entry.get("rooms") or []
        room_strs = []
        for r in rooms:
            name, bld = r.get("name", ""), r.get("building", "")
            room_strs.append(f"{name} ({bld})" if name and bld else name)
        location = ", ".join(filter(None, room_strs)) or entry.get("modality", "")
        if location:
            event.add("location", location)

        # Description
        parts = []
        for label, key in [
            ("Type", "type"),
            ("Enseignant", "teacher"),
            ("Promotion", "promotion"),
            ("Modalité", "modality"),
            ("Statut", "state"),
            ("Note", "comment"),
        ]:
            if entry.get(key):
                parts.append(f"{label} : {entry[key]}")
        if parts:
            event.add("description", "\n".join(parts))

        event.add("dtstamp", datetime.now(pytz.utc))
        if entry.get("lastUpdateDate"):
            event.add(
                "last-modified",
                datetime.fromtimestamp(entry["lastUpdateDate"] / 1000.0, tz=pytz.utc),
            )

        cal.add_component(event)

    return cal.to_ical()


# ---------------------------------------------------------------------------
# Handler Vercel (interface BaseHTTPRequestHandler)
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        username = os.environ.get("SKOLAE_USERNAME")
        password = os.environ.get("SKOLAE_PASSWORD")

        if not username or not password:
            self._respond(500, "text/plain", b"SKOLAE_USERNAME et SKOLAE_PASSWORD doivent etre definis.")
            return

        try:
            session = _create_session()
            token = _login(session, username, password)
            start, end = _sync_range()
            entries = _get_agenda(session, token, start, end)
            ical_bytes = _build_ical(entries)

            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="ges-agenda.ics"')
            # Cache 1h côté client / CDN Vercel
            self.send_header("Cache-Control", "public, max-age=86400, s-maxage=86400")
            self.end_headers()
            self.wfile.write(ical_bytes)

        except ValueError as e:
            self._respond(401, "text/plain", str(e).encode())
        except Exception as e:
            logger.exception("Erreur inattendue")
            self._respond(500, "text/plain", f"Erreur serveur : {e}".encode())

    def _respond(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    # Silence les logs de requête par défaut de BaseHTTPRequestHandler
    def log_message(self, *args):
        pass
