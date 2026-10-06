"""Terremoti INGV (FDSN web service, GeoJSON) attorno a Torino."""
import json
from datetime import UTC, datetime, timedelta

from ..events import Event
from ..text import ROME, distance_km

SOURCE = "TERREMOTO"
LINK = "https://terremoti.ingv.it/event/{id}"

# (distanza massima km, magnitudo minima): più lontano serve una scossa più forte per essere avvertita.
RULES = ((50, 2.5), (150, 3.5), (300, 4.5))


def url(now: datetime | None = None) -> str:
    since = (now or datetime.now(UTC)) - timedelta(days=2)
    return (
        "https://webservices.ingv.it/fdsnws/event/1/query?lat=45.0703&lon=7.6869"
        f"&maxradiuskm=300&minmag=2.5&starttime={since:%Y-%m-%dT%H:%M:%S}&format=geojson&orderby=time"
    )


def _severity(mag: float, km: float) -> str:
    if mag >= 5 or (mag >= 4 and km < 80):
        return "CRIT"
    if mag >= 4 or (mag >= 3 and km < 50):
        return "HIGH"
    return "MED"


def parse(data: bytes) -> list[Event]:
    if not data.strip():
        return []  # 204 No Content: nessun evento
    events = []
    for feat in json.loads(data).get("features", []):
        p = feat.get("properties", {})
        lon, lat, depth = (feat.get("geometry", {}).get("coordinates") or [None, None, None])[:3]
        mag = p.get("mag")
        if lat is None or mag is None:
            continue
        km = distance_km(lat, lon)
        if not any(km <= dist and mag >= min_mag for dist, min_mag in RULES):
            continue

        when = datetime.fromisoformat(p["time"]).replace(tzinfo=UTC).astimezone(ROME)
        events.append(Event(
            id=f"ingv:{p.get('eventId')}",
            source=SOURCE,
            severity=_severity(mag, km),
            title=f"Terremoto M{mag:.1f} — {p.get('place', '?')}",
            body=(
                f"Ore {when:%H:%M} del {when:%d/%m/%Y}, a {km:.0f} km da Torino, profondità {depth:.0f} km.\n"
                "Dati INGV, la magnitudo può essere rivista nei minuti successivi."
            ),
            link=LINK.format(id=p.get("eventId")),
            # Una revisione di magnitudo di almeno 0.3 genera un aggiornamento.
            fingerprint=f"{round(mag / 0.3)}",
        ))
    return events
