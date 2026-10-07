"""
Livelli idrometrici ARPA in tempo reale con le soglie ufficiali di ogni stazione
(presoglia, guardia, pericolo). Avvisa quando un fiume cambia fascia, rientro compreso.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from ..events import Event
from ..text import ROME

URL = "https://www.arpa.piemonte.it/rischi_naturali/data/tr/idro/{sensor}.geojson"
LINK = "https://www.arpa.piemonte.it/rischi_naturali/snippets_arpa_graphs/dettaglio_stazione?id={sensor}&param=idro"
SOURCE = "FIUMI"

# Stazioni che descrivono il rischio in città (sensore ARPA -> nome leggibile).
STATIONS = {
    "001272703": "Po ai Murazzi",
    "001156901": "Po a Moncalieri",
    "001272701": "Dora Riparia a ponte Washington",
    "001272702": "Stura di Lanzo a corso Giulio Cesare",
    "001272909": "Sangone a corso Unione Sovietica",
    "001292900": "Ceronda a Venaria",
}
BANDS = [
    # (chiave soglia, etichetta, gravità)
    ("pre-alarm_threshold", "presoglia", "MED"),
    ("guard_threshold", "livello di guardia", "HIGH"),
    ("danger_threshold", "livello di pericolo", "CRIT"),
]


def _readings(props: dict) -> list[tuple[str, float]]:
    data = props.get("idro") or {}
    return [(ts, v["value"]) for ts, v in sorted(data.items()) if isinstance(v, dict) and v.get("value") is not None]


def _fmt(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " m"


def parse_station(data: bytes, name: str | None = None) -> Event | None:
    doc = json.loads(data)
    feature = doc["features"][0] if "features" in doc else doc
    props = feature["properties"]
    sensor = props.get("sensor", "")
    name = name or STATIONS.get(sensor) or props.get("name", sensor).title()
    readings = _readings(props)
    if not readings:
        return None

    ts, value = readings[-1]
    band = 0
    for i, (key, _, _) in enumerate(BANDS, start=1):
        threshold = props.get(key)
        if threshold is not None and value >= threshold:
            band = i

    when = datetime.fromisoformat(ts).replace(tzinfo=UTC).astimezone(ROME)
    earlier = [v for t, v in readings if t <= _hours_before(ts, 3)]
    trend = ""
    if earlier:
        delta = value - earlier[-1]
        trend = f" ({'+' if delta >= 0 else '−'}{_fmt(abs(delta))} in 3 ore)"
    thresholds = ", ".join(
        f"{label} {_fmt(props[key])}" for key, label, _ in BANDS if props.get(key) is not None
    )
    body = f"Livello alle {when:%H:%M} del {when:%d/%m}: {_fmt(value)}{trend}\nSoglie: {thresholds or 'non definite'}"

    if band == 0:
        return Event(
            id=f"fiume:{sensor}", source=SOURCE, severity="INFO",
            title=f"{name}: livello rientrato sotto la presoglia",
            body=body, link=LINK.format(sensor=sensor),
            fingerprint="0", silent_if_new=True,
        )
    _, label, severity = BANDS[band - 1]
    return Event(
        id=f"fiume:{sensor}", source=SOURCE, severity=severity,
        title=f"🌊 {name}: superato il {label} ({_fmt(value)})",
        body=body, link=LINK.format(sensor=sensor),
        fingerprint=str(band),
        digest_line=f"{name}: {label} superato, {_fmt(value)}",
    )


def _hours_before(ts: str, hours: int) -> str:
    return (datetime.fromisoformat(ts) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def collect(fetch, timeout: float) -> list[Event]:
    """Scarica le stazioni in parallelo; fallisce solo se non risponde nessuna."""
    def one(sensor: str) -> Event | None:
        return parse_station(fetch(URL.format(sensor=sensor), timeout), STATIONS[sensor])

    with ThreadPoolExecutor(max_workers=len(STATIONS)) as pool:
        futures = [pool.submit(one, s) for s in STATIONS]
    events, errors = [], []
    for fut in futures:
        try:
            ev = fut.result()
            if ev:
                events.append(ev)
        except Exception as e:
            errors.append(e)
    if errors and not events:
        raise errors[0]
    return events
