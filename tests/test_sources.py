import re
from datetime import UTC, date, datetime

from torinoalert.sources import arpa, comune, gtt, rfi

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _arpa_with_zone_levels(xml: str, zone: str, levels: dict[str, str]) -> bytes:
    """Imposta i livelli di alcuni parametri nel blocco <info> di una zona."""
    blocks = re.split(r"(?=<info>)", xml)
    for i, block in enumerate(blocks):
        if f"<areaDesc>{zone}</areaDesc>" in block:
            for name, level in levels.items():
                block = re.sub(
                    rf"(<valueName>{name}</valueName>\s*<value>)[^<]*",
                    rf"\g<1>{level}",
                    block,
                )
            blocks[i] = block
    return "".join(blocks).encode()


# ---------- ARPA ----------

def test_arpa_green_is_silent_and_stable(fixture_bytes):
    events = arpa.parse(fixture_bytes("arpa_verde.xml"), zones=("Piem-L",), now=NOW)
    assert len(events) == 1
    ev = events[0]
    assert ev.id == "arpa:Piem-L"
    assert ev.fingerprint == "VERDE"
    assert ev.silent_if_new
    assert ev.severity == "INFO"


def test_arpa_alert_lists_days_and_levels(fixture_text):
    xml = _arpa_with_zone_levels(
        fixture_text("arpa_verde.xml"), "Piem-L",
        {"TEMPORALI_1224": "GIALLO", "IDROGEOLOGICO_2436": "ARANCIONE"},
    )
    [ev] = arpa.parse(xml, zones=("Piem-L",), now=NOW)
    assert ev.severity == "HIGH"
    assert ev.title.startswith("Allerta ARANCIONE — zona L")
    assert "5 ottobre: Temporali — GIALLA" in ev.body
    assert "6 ottobre: Idrogeologico — ARANCIONE" in ev.body
    assert ev.fingerprint == "IDROGEOLOGICO=2;TEMPORALI=1"
    assert not ev.silent_if_new


def test_arpa_fingerprint_ignores_day_rollover(fixture_text):
    base = fixture_text("arpa_verde.xml")
    tomorrow = arpa.parse(_arpa_with_zone_levels(base, "Piem-L", {"NEVE_2436": "GIALLO"}), ("Piem-L",), NOW)
    today = arpa.parse(_arpa_with_zone_levels(base, "Piem-L", {"NEVE_1224": "GIALLO"}), ("Piem-L",), NOW)
    assert tomorrow[0].fingerprint == today[0].fingerprint


def test_arpa_other_zones_and_expired_are_ignored(fixture_bytes):
    xml = fixture_bytes("arpa_verde.xml")
    assert arpa.parse(xml, zones=("Piem-Z",), now=NOW) == []
    assert arpa.parse(xml, zones=("Piem-L",), now=datetime(2026, 10, 8, tzinfo=UTC)) == []


# ---------- GTT ----------

# Mercoledì 7 ottobre 2026, 20:00 a Roma: tutti gli avvisi della fixture sono attivi.
GTT_NOW = 1791396000


def test_gtt_alerts_lines_from_routes_and_text(fixture_bytes):
    events = {e.title: e for e in gtt.parse_alerts(fixture_bytes("gtt_alerts.pb"), now=GTT_NOW)}
    assert events["Linea 17 deviata in entrambe le direzioni"].lines == ("17",)
    assert events["Linea 36N deviata in entrambe le direzioni"].lines == ("36N",)
    # Avviso senza route_id: le linee si ricavano dal titolo.
    assert events["Linee 13 e 15 deviate in entrambe le direzioni."].lines == ("13", "15")


def test_gtt_alerts_severity_and_periods(fixture_bytes):
    events = {e.title: e for e in gtt.parse_alerts(fixture_bytes("gtt_alerts.pb"), now=GTT_NOW)}
    # La descrizione dice "riprende regolare percorso", ma resta una deviazione.
    # Deviazione urbana: sul canale ma silenziosa (sono decine al giorno).
    assert events["Linea 17 deviata in entrambe le direzioni"].severity == "LOW"
    assert events["Linea 17 deviata in entrambe le direzioni"].topic == ""
    assert "Quando: dal mer 07/10 10:11 al dom 11/10 21:59" in events["Linea 17 deviata in entrambe le direzioni"].body
    elevator = next(e for t, e in events.items() if "ascensori" in t)
    assert elevator.title.startswith("🛗") and elevator.severity == "LOW" and elevator.lines == ("METRO",)
    assert "fino a nuova comunicazione" in elevator.body and not elevator.digest_line


def test_gtt_alerts_skip_finished_and_ids_stable(fixture_bytes):
    data = fixture_bytes("gtt_alerts.pb")
    now_ids = [e.id for e in gtt.parse_alerts(data, now=GTT_NOW)]
    assert now_ids == [e.id for e in gtt.parse_alerts(data, now=GTT_NOW)]
    later = gtt.parse_alerts(data, now=GTT_NOW + 30 * 86400)
    assert len(later) < len(now_ids) and all("ascensori" in e.title for e in later)


def test_route_to_line():
    assert gtt.route_to_line("17U") == "17"
    assert gtt.route_to_line("1432E") == "1432"
    assert gtt.route_to_line("36NU") == "36N"
    assert gtt.route_to_line("METROU") == "METRO"


def test_gtt_news_filters_promotions(fixture_bytes):
    events = gtt.parse_news(fixture_bytes("gtt_news.xml"))
    titles = " | ".join(e.title for e in events)
    assert "Trambusto" not in titles
    assert "podcast" not in titles
    assert "Piazza Baldissera" in titles


def test_gtt_news_severity_from_title(fixture_bytes):
    by_title = {e.title: e for e in gtt.parse_news(fixture_bytes("gtt_news.xml"))}
    baldissera = next(e for t, e in by_title.items() if "Baldissera" in t)
    # il body cita "sospesa la linea B1", ma il titolo dice "ripristinati"
    assert baldissera.severity == "INFO"
    se2 = next(e for t, e in by_title.items() if "SE2" in t)
    assert not se2.title.startswith("🅿️")  # "parcheggio" solo nel body


# ---------- RFI ----------

def test_rfi_includes_resolutions_with_fingerprint(fixture_bytes):
    events = rfi.parse(fixture_bytes("rfi.xml"))
    assert len(events) == 2
    assert all(e.severity == "INFO" for e in events)  # "tornata regolare"
    assert all(e.fingerprint for e in events)


def test_rfi_title_update_changes_fingerprint_not_id(fixture_text):
    xml = fixture_text("rfi.xml")
    before = rfi.parse(xml.replace("tornata regolare", "sospesa").encode())
    after = rfi.parse(xml.encode())
    assert before[0].id == after[0].id
    assert before[0].fingerprint != after[0].fingerprint
    assert before[0].severity == "HIGH"


# ---------- COMUNE ----------

def test_comune_viabilita(fixture_text):
    events = comune.parse_viabilita(fixture_text("comune_comunicati.html"))
    titles = [e.title for e in events]
    assert any(t.startswith("Cantieri in città") for t in titles)
    assert all(e.link.startswith("https://www.comune.torino.it/novita/") for e in events)
    assert "Comunicati" not in titles  # voce di menu


def test_comune_smog_skips_past_dates(fixture_text):
    html = fixture_text("comune_avvisi.html")
    [ev] = comune.parse_smog(html, today=date(2026, 10, 5))
    assert "antismog" in ev.title.lower()

    html_past = html.replace(
        "Misure antismog a tutela della salute, in vigore le limitazioni strutturali",
        "Limitazioni antismog del 3 ottobre",
    )
    assert comune.parse_smog(html_past, today=date(2026, 10, 5)) == []


def test_gtt_alerts_topics(fixture_bytes):
    events = gtt.parse_alerts(fixture_bytes("gtt_alerts.pb"), now=GTT_NOW)
    extra = [e for e in events if e.topic == "extraurbane"]
    assert extra and all(all(x.isdigit() and len(x) == 4 for x in e.lines) for e in extra)
    assert any("1432" in e.lines for e in extra)
    urban = [e for e in events if not e.topic]
    assert any("17" in e.lines for e in urban) and any("METRO" in e.lines for e in urban)


def test_gtt_topic_rules():
    from torinoalert.gtfsrt import Alert

    assert gtt._topic(Alert(id="1", routes=["2027E"]), "Linea 2027 deviata", {"2027"}) == "extraurbane"
    assert gtt._topic(Alert(id="2", routes=["17U"]), "Linea 17 deviata", {"17"}) == ""
    assert gtt._topic(Alert(id="3", stops=["13135"]), "Fermata n. 13135 sospesa", set()) == "fermate"
    assert gtt._topic(Alert(id="4"), "Linee 2014, 2016 deviate", {"2014", "2016"}) == "extraurbane"


def test_rfi_topics(fixture_text):
    xml = fixture_text("rfi.xml")
    suspended = rfi.parse(xml.replace("tornata regolare", "sospesa").encode())[0]
    restored = rfi.parse(xml.encode())[0]
    assert suspended.topic == "" and restored.topic == "treni"
