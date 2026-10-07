"""
Lettore minimo di GTFS-realtime "Service Alerts" (protobuf), senza dipendenze:
decodifica solo i campi che servono agli avvisi. Specifica:
https://gtfs.org/realtime/reference/
"""
from dataclasses import dataclass, field

# Numeri di campo della specifica GTFS-realtime.
_FEED_ENTITY = 2
_ENTITY_ID, _ENTITY_DELETED, _ENTITY_ALERT = 1, 2, 5
_ALERT_PERIOD, _ALERT_INFORMED, _ALERT_CAUSE, _ALERT_EFFECT = 1, 5, 6, 7
_ALERT_URL, _ALERT_HEADER, _ALERT_DESCRIPTION = 8, 10, 11
_RANGE_START, _RANGE_END = 1, 2
_SELECTOR_ROUTE, _SELECTOR_STOP = 2, 5
_TRANSLATION, _TRANSLATION_TEXT, _TRANSLATION_LANG = 1, 1, 2

CAUSES = {
    1: "causa sconosciuta", 2: "altro", 3: "problema tecnico", 4: "sciopero", 5: "manifestazione",
    6: "incidente", 7: "festività", 8: "maltempo", 9: "manutenzione", 10: "lavori",
    11: "intervento di polizia", 12: "emergenza sanitaria",
}
EFFECTS = {
    1: "servizio sospeso", 2: "servizio ridotto", 3: "ritardi significativi", 4: "deviazione",
    5: "servizio aggiuntivo", 6: "servizio modificato", 7: "altro", 8: "sconosciuto",
    9: "fermata spostata", 10: "nessun effetto", 11: "accessibilità",
}


@dataclass
class Alert:
    id: str
    periods: list[tuple[int, int]] = field(default_factory=list)  # (inizio, fine) epoch, 0 = aperto
    routes: list[str] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    cause: int = 0
    effect: int = 0
    header: str = ""
    description: str = ""
    url: str = ""


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def fields(buf: bytes) -> dict[int, list]:
    """Campo -> valori: int per varint/fixed, bytes per length-delimited."""
    out: dict[int, list] = {}
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 5:
            value, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        else:
            raise ValueError(f"wire type {wire} non supportato")
        out.setdefault(num, []).append(value)
    return out


def _text(buf: bytes, lang: str = "it") -> str:
    """TranslatedString: preferisce l'italiano, altrimenti la prima traduzione."""
    texts = []
    for tr in fields(buf).get(_TRANSLATION, []):
        f = fields(tr)
        text = f.get(_TRANSLATION_TEXT, [b""])[0].decode("utf-8", "replace")
        language = f.get(_TRANSLATION_LANG, [b""])[0].decode("utf-8", "replace")
        texts.append((language, text))
    for language, text in texts:
        if language in (lang, ""):
            return text
    return texts[0][1] if texts else ""


def parse_alerts(data: bytes) -> list[Alert]:
    alerts = []
    for raw in fields(data).get(_FEED_ENTITY, []):
        entity = fields(raw)
        if entity.get(_ENTITY_DELETED, [0])[0] or _ENTITY_ALERT not in entity:
            continue
        a = fields(entity[_ENTITY_ALERT][0])
        alert = Alert(id=entity.get(_ENTITY_ID, [b""])[0].decode("utf-8", "replace"))
        for p in a.get(_ALERT_PERIOD, []):
            r = fields(p)
            alert.periods.append((r.get(_RANGE_START, [0])[0], r.get(_RANGE_END, [0])[0]))
        for sel in a.get(_ALERT_INFORMED, []):
            s = fields(sel)
            if s.get(_SELECTOR_ROUTE):
                alert.routes.append(s[_SELECTOR_ROUTE][0].decode("utf-8", "replace"))
            if s.get(_SELECTOR_STOP):
                alert.stops.append(s[_SELECTOR_STOP][0].decode("utf-8", "replace"))
        alert.cause = a.get(_ALERT_CAUSE, [0])[0]
        alert.effect = a.get(_ALERT_EFFECT, [0])[0]
        alert.header = _text(a[_ALERT_HEADER][0]) if _ALERT_HEADER in a else ""
        alert.description = _text(a[_ALERT_DESCRIPTION][0]) if _ALERT_DESCRIPTION in a else ""
        alert.url = _text(a[_ALERT_URL][0]) if _ALERT_URL in a else ""
        alerts.append(alert)
    return alerts
