import json
from datetime import date, datetime

import pytest

from torinoalert.config import Settings
from torinoalert.sources import all_sources as registry
from torinoalert.sources import aria, cittametro, due_sources, ingv, muoversi, scioperi, smat, trenitalia
from torinoalert.text import ROME

TODAY = date(2026, 10, 6)


# ---------- SCIOPERI ----------

def test_scioperi_filters_sector_and_region(fixture_bytes):
    events = scioperi.parse(fixture_bytes("scioperi.xml"), today=TODAY)
    titles = [e.title for e in events]
    assert titles, "attesi scioperi rilevanti"
    assert not any("Sicilia" in t or "Lombardia" in t for t in titles)  # altre regioni
    assert not any("merci" in t for t in titles)  # settore non rilevante
    assert any(e.severity == "HIGH" and "generale" in e.title for e in events)


def test_scioperi_reminder_the_day_before(fixture_bytes):
    xml = fixture_bytes("scioperi.xml")
    first = scioperi.parse(xml, today=TODAY)[0]
    start = date(int(first.title[-4:]), int(first.title[-7:-5]), int(first.title[-10:-8]))
    on_eve = scioperi.parse(xml, today=start.replace(day=start.day - 1))
    reminders = [e for e in on_eve if e.id.startswith("sciopero-promemoria:")]
    assert reminders and reminders[0].title.startswith("⏰ Domani")
    assert all(e.digest_line for e in on_eve if e.id == first.id)


def test_scioperi_skips_finished(fixture_bytes):
    assert scioperi.parse(fixture_bytes("scioperi.xml"), today=date(2027, 6, 1)) == []


# ---------- INGV ----------

def _quake(mag, lat, lon, event_id=1):
    return json.dumps({"features": [{
        "properties": {"eventId": event_id, "time": "2026-10-06T10:00:00.000000", "mag": mag, "place": "Test"},
        "geometry": {"coordinates": [lon, lat, 10.0]},
    }]}).encode()


def test_ingv_distance_magnitude_rules():
    assert ingv.parse(_quake(2.6, 45.05, 7.36)) != []  # Giaveno, 25 km
    assert ingv.parse(_quake(2.6, 44.0, 10.5)) == []  # Toscana, debole
    assert ingv.parse(_quake(4.8, 44.0, 10.5))[0].severity == "HIGH"
    assert ingv.parse(_quake(4.2, 45.05, 7.36))[0].severity == "CRIT"


def test_ingv_fixture_and_empty_response(fixture_bytes):
    for e in ingv.parse(fixture_bytes("ingv.json")):
        assert e.title.startswith("Terremoto M")
    assert ingv.parse(b"") == []


def test_ingv_magnitude_revision_changes_fingerprint():
    a, b = ingv.parse(_quake(3.0, 45.05, 7.36))[0], ingv.parse(_quake(3.4, 45.05, 7.36))[0]
    assert a.id == b.id and a.fingerprint != b.fingerprint


# ---------- TRENITALIA ----------

def test_trenitalia_piemonte_works(fixture_text):
    events = trenitalia.parse(fixture_text("trenitalia.html"), today=TODAY)
    titles = [e.title for e in events]
    assert any("Torino - Milano" in t for t in titles)
    assert any("Torino - Modane" in t for t in titles)
    assert not any("Chiasso" in t for t in titles)  # lombarda, nella sezione Piemonte
    assert not any("Fiumicino" in t for t in titles)  # tempo reale fuori zona
    modane = next(e for e in events if "Modane" in e.title)
    assert "Bussoleno" in modane.body and modane.link.endswith(".pdf")


def test_trenitalia_digest_only_on_affected_days(fixture_text):
    events = trenitalia.parse(fixture_text("trenitalia.html"), today=date(2026, 10, 11))
    novara = [e for e in events if "Novara" in e.body and "11 ottobre" in e.body]
    assert novara and all(e.digest_line for e in novara)


# ---------- CITTÀ METROPOLITANA ----------

def test_cittametro_rows(fixture_text):
    events = cittametro.parse(fixture_text("cm_viabilita.html"), today=TODAY)
    assert len(events) > 10
    salassa = next(e for e in events if "Salassa" in e.title)
    assert salassa.title.startswith("Senso unico alternato — S.P. n. 35")
    assert salassa.severity == "LOW"
    assert any(e.title.startswith("Strada chiusa") and e.severity == "MED" for e in events)


def test_cittametro_skips_finished_and_requires_table(fixture_text):
    assert len(cittametro.parse(fixture_text("cm_viabilita.html"), today=date(2028, 1, 1))) < 5
    with pytest.raises(ValueError):
        cittametro.parse("<html><body>manutenzione</body></html>")


# ---------- 5T ----------

def test_5t_traffic_within_radius(fixture_text):
    now = datetime(2026, 10, 6, 12, 0, tzinfo=ROME)
    events = muoversi.parse(fixture_text("5t_home.html"), radius_km=15, now=now)
    traffic = [e for e in events if e.source == muoversi.SOURCE_TRAFFIC]
    assert traffic and all(e.id.startswith("5t:") for e in traffic)
    assert not any("Sempione" in e.title for e in traffic)  # fuori raggio
    assert len(muoversi.parse(fixture_text("5t_home.html"), radius_km=1, now=now)) < len(events)


def test_5t_tpl_news_filtered_to_turin_area(fixture_text):
    props = muoversi.page_props(fixture_text("5t_home.html"))
    titles = [e.title for e in muoversi.parse_tpl_news(props)]
    assert any("Bardonecchia" in t for t in titles)
    assert not any("Alba" in t for t in titles)


def test_5t_weather_line(fixture_text):
    assert "16°/24°" in muoversi.weather_line(fixture_text("5t_home.html"))


# ---------- ARIA ----------

def test_semaforo_torino(fixture_bytes):
    [e] = aria.parse_semaforo(fixture_bytes("arpa_semaforo.json"))
    assert e.fingerprint == "0/0" and e.silent_if_new
    assert "livello 0" in e.body


def test_semaforo_level_change(fixture_bytes):
    doc = json.loads(fixture_bytes("arpa_semaforo.json"))
    for c in doc["C"]:
        if c["I"] == aria.TORINO_ISTAT:
            c["LD"] = 1
    [e] = aria.parse_semaforo(json.dumps(doc).encode())
    assert e.fingerprint == "0/1" and e.severity == "MED" and not e.silent_if_new


def test_caldo_out_of_season_and_in_season(fixture_bytes):
    data = fixture_bytes("arpa_caldo.json")
    assert aria.parse_caldo(data, today=TODAY) == []  # bollettino non aggiornato
    [e] = aria.parse_caldo(data, today=date(2026, 9, 30))
    assert e.id == "caldo:torino" and e.fingerprint.isdigit()


# ---------- SMAT ----------

def test_smat_keeps_only_disservices(fixture_bytes):
    titles = [e.title for e in smat.parse(fixture_bytes("smat_posts.json"))]
    assert not any("RESILIENZA" in t for t in titles)
    assert not any("FESTIVAL" in t for t in titles)
    assert any("SPORTELLI" in t for t in titles)


# ---------- FREQUENZE ----------

def test_due_sources_by_minute():
    sources = registry(Settings())
    at = lambda minute: set(due_sources(sources, minute * 60, 2))  # noqa: E731
    assert "GTT_LIVE" in at(7) and "RFI" in at(7)
    assert "TRAFFICO_5T" in at(10) and "TRAFFICO_5T" not in at(14)
    assert "SCIOPERI" in at(30) and "SCIOPERI" not in at(40)
    # In un'ora ogni fonte gira almeno una volta.
    assert set().union(*(at(m) for m in range(0, 60, 2))) == set(sources)
