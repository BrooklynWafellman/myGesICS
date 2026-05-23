from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import datetime
import json
import requests

# Remplace par l'URL de base de l'API MyGES issue de la doc communautaire
MYGES_API_URL = "https://api.ges-network.com/v1"


class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        # 1. Extraction des paramètres de l'URL
        query_components = parse_qs(urlparse(self.path).query)
        username = query_components.get("user", [None])[0]
        password = query_components.get("password", [None])[0]

        if not username or not password:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing user or password parameter")
            return

        try:
            # 2. Authentification et récupération du token
            # Note : À adapter selon le endpoint exact d'auth de MyGES
            auth_res = requests.post(
                f"{MYGES_API_URL}/auth", json={"login": username, "password": password}, timeout=5
            )
            auth_res.raise_for_status()
            token = auth_res.json().get("access_token")

            headers = {"Authorization": f"Bearer {token}"}

            # 3. Récupération du planning (ex: sur le mois en cours)
            # Adapte les query params selon la structure réelle de l'API MyGES
            now = datetime.datetime.now()
            start_date = now.strftime("%Y-%m-%d")
            end_date = (now + datetime.timedelta(days=30)).strftime("%Y-%m-%d")

            agenda_res = requests.get(
                f"{MYGES_API_URL}/me/agenda?start={start_date}&end={end_date}",
                headers=headers,
                timeout=5,
            )
            agenda_res.raise_for_status()
            events = agenda_res.json()

            # 4. Génération brute du format iCalendar
            ics_lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//MyGES Vercel App//NONSGML//FR"]

            for ev in events:
                # Modifie les clés ('start', 'end', 'name', 'room') selon le format JSON de MyGES
                dtstart = ev.get("start", "").replace("-", "").replace(":", "")[:15]
                dtend = ev.get("end", "").replace("-", "").replace(":", "")[:15]
                summary = ev.get("name", "Cours")
                location = ev.get("room", "N/A")

                ics_lines.append("BEGIN:VEVENT")
                ics_lines.append(f"DTSTART:{dtstart}")
                ics_lines.append(f"DTEND:{dtend}")
                ics_lines.append(f"SUMMARY:{summary}")
                ics_lines.append(f"LOCATION:{location}")
                ics_lines.append("END:VEVENT")

            ics_lines.append("END:VCALENDAR")
            ics_content = "\r\n".join(ics_lines)

            # 5. Réponse avec mise en cache (1 heure) pour ne pas spammer MyGES
            self.send_response(200)
            self.send_header("Content-Type", "text/calendar; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="calendar.ics"')
            self.send_header("Cache-Control", "s-maxage=3600, stale-while-revalidate=600")
            self.end_headers()

            self.wfile.write(ics_content.encode("utf-8"))

        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Error: {str(e)}".encode("utf-8"))