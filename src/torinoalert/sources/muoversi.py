"""
Muoversi in Piemonte (5T): la homepage incorpora in __NEXT_DATA__ gli eventi di
traffico regionali (con coordinate), le news del trasporto pubblico e il meteo.
"""
import json
import re
from datetime import datetime

from ..events import Event
from ..text import ROME, distance_km, sha, strip_html

URL = "https://www.muoversinpiemonte.it/"
SOURCE_TRAFFIC = "TRAFFICO"
SOURCE_TPL = "TRASPORTO PUBBLICO REGIONALE"

# News TPL rilevanti per chi vive a Torino e cintura.
TPL_KEYWORDS = ("torino", "gtt", "sfm", "metro", "pinerolo", "chieri", "rivoli", "moncalieri", "settimo",
                "collegno", "nichelino", "venaria", "caselle", "chivasso", "ivrea", "susa", "bardonecchia",
                "lanzo", "carmagnola", "orbassano", "grugliasco", "cirié", "ciriè")


def page_props(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ non trovato")
    return json.loads(m.group(1))["props"]["pageProps"]


# Divisione dei compiti tra le fonti stradali, per non avvisare due volte:
# vie di Torino -> Muoversi a Torino, strade provinciali -> Città metropolitana,
# qui restano autostrade, tangenziale, statali e raccordi.
_PROVINCIAL = re.compile(r"strada provinciale|\bS\.?P\.?\s?\d", re.IGNORECASE)
_CITY_STREET = re.compile(r"\(TO\)")
_MOTORWAY = re.compile(
    r"\b(A ?(?:4|5|6|21|32|55)|T4|tangenziale|raccordo|diramazione|traforo|fr[eé]jus|autostrada)\b",
    re.IGNORECASE,
)
MOTORWAY_RADIUS_KM = 80
# Entro questo raggio le strade ordinarie sono vie di Torino (anche senza "(TO)").
CITY_RADIUS_KM = 7


def _dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def parse_traffic(props: dict, radius_km: float, now: datetime | None = None) -> list[Event]:
    now = now or datetime.now(ROME)
    events = []
    for e in props.get("trafficEventData") or []:
        try:
            km = distance_km(float(e["lat"]), float(e["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        road = (e.get("road") or "").strip()
        if _MOTORWAY.search(road):
            if km > MOTORWAY_RADIUS_KM:
                continue
        elif km > radius_km or km <= CITY_RADIUS_KM or _PROVINCIAL.search(road) or _CITY_STREET.search(road):
            continue
        start, end = _dt(e.get("startDate")), _dt(e.get("endDate"))
        if end and end < now:
            continue

        closure = e.get("style") == "chiusura"
        what = (e.get("what") or "").strip()
        active_today = start is not None and start.date() <= now.date() and (end is None or end.date() >= now.date())
        events.append(Event(
            id=f"5t:{e.get('id')}",
            source=SOURCE_TRAFFIC,
            topic="traffico",
            severity="MED" if closure else "LOW",
            title=f"{'⛔ ' if closure else '🚧 '}{road}: {what}",
            body="\n".join(x for x in (e.get("where"), e.get("when")) if x),
            link=URL + "traffic",
            fingerprint=sha(f"{what}|{e.get('when')}")[:16],
            digest_line=f"{road}: {what} ({e.get('when')})" if closure and active_today else "",
        ))
    return events


def parse_tpl_news(props: dict) -> list[Event]:
    events = []
    for n in (props.get("publicTransportData") or {}).get("news") or []:
        title = (n.get("title") or "").strip()
        desc = strip_html(n.get("description") or "")
        tags = ", ".join(t.get("title", "") for t in n.get("tags") or [])
        text = f"{title} {desc} {tags}".lower()
        if not any(k in text for k in TPL_KEYWORDS):
            continue
        url = n.get("detailsUrl") or ""
        events.append(Event(
            id="5t-tpl:" + sha(url or title),
            source=SOURCE_TPL,
            topic="extraurbane",
            severity="LOW",
            title=title,
            body=desc + (f"\n({tags})" if tags else ""),
            link=URL.rstrip("/") + url if url.startswith("/") else (url or URL),
        ))
    return events


def parse(html: str, radius_km: float, now: datetime | None = None) -> list[Event]:
    props = page_props(html)
    return parse_traffic(props, radius_km, now) + parse_tpl_news(props)


def weather_line(html: str, province: str = "Torino") -> str:
    """Previsione sintetica per il riepilogo del mattino ("mar 6: nuvoloso 16°/24°")."""
    for prov in (page_props(html).get("weatherData") or {}).get("data") or []:
        if prov.get("prov") == province:
            return " · ".join(
                f"{d.get('date')}: {(d.get('icon') or '').replace('-', ' ')} {d.get('temperature')}"
                for d in prov.get("data") or []
            )
    return ""
