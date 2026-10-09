from datetime import date

import pytest

from torinoalert.text import (
    dates_in_text,
    distance_km,
    extract_line,
    lines_in_text,
    normalize_body,
    strip_html,
    title_has_past_date,
    title_with_line,
)

TODAY = date(2026, 10, 5)


@pytest.mark.parametrize(
    "title, today, expected",
    [
        # Bug storico: la data di oggi veniva confrontata con la mezzanotte e scartata.
        ("Limitazioni al traffico per lunedì 5 ottobre", TODAY, False),
        ("Limitazioni al traffico per domenica 4 ottobre", TODAY, True),
        # Bug storico: veniva presa la prima data in ordine di mese (aprile) invece dell'ultima.
        ("Misure antismog dal 15 settembre al 15 aprile", TODAY, False),
        ("Blocco del traffico dal 1° ottobre al 31 marzo", TODAY, False),
        ("Semaforo antismog: 31 dicembre", date(2027, 1, 10), True),
        ("Limitazioni del 2 gennaio", date(2026, 12, 30), False),
        ("Limitazioni 5 ottobre 2025", TODAY, True),
        ("Misure antismog in vigore", TODAY, False),
        ("Livello 1 del 31 febbraio", TODAY, False),  # data impossibile ignorata
    ],
)
def test_title_has_past_date(title, today, expected):
    assert title_has_past_date(title, today) is expected


def test_dates_in_text_rolls_year_forward():
    assert dates_in_text("dal 15 settembre al 15 aprile", TODAY) == [date(2026, 9, 15), date(2027, 4, 15)]


def test_strip_html_keeps_inline_text_together():
    html = "<p>Le linee <strong>riprendono i percorsi</strong>. Altro.</p><ul><li>uno</li><li>due</li></ul>"
    assert strip_html(html) == "Le linee riprendono i percorsi. Altro.\n• uno\n• due"


def test_normalize_body_removes_read_more_and_summarizes():
    body = "Prima frase. Seconda frase. " + "Riempitivo lavori. " * 100 + "Leggi tutto..."
    out = normalize_body("TRASPORTO PUBBLICO (GTT)", "Titolo", body, max_len=200)
    assert out.startswith("Prima frase. Seconda frase.")
    assert "Dettagli nel link." in out
    assert "Leggi tutto" not in out
    assert len(out) <= 201


def test_extract_line_ignores_numbers_in_dates():
    assert extract_line("Deviazione dalle 21 del 5 ottobre") is None
    assert extract_line("Linea 4: deviazione") == "LINEA 4"
    assert extract_line("Guasto in metropolitana") == "METROPOLITANA"


def test_title_with_line_does_not_duplicate():
    assert title_with_line("Linee 13 e 15 deviate") == "Linee 13 e 15 deviate"
    assert title_with_line("Falchera: la linea 4 torna") == "LINEA 4 — Falchera: la linea 4 torna"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Linee 13 e 15 deviate in entrambe le direzioni.", {"13", "15"}),
        ("Linee 65 e 3382 deviate.", {"65", "3382"}),
        (
            "ripristinati i percorsi delle linee 10N – 11 – 67 Festiva – 77 – N10 GIALLA – B1 dal 24",
            {"10N", "11", "67", "77", "N10", "B1"},
        ),
        ("Linea SE2: modifica di percorso", {"SE2"}),
        ("la linea 4 torna dal 14 settembre", {"4"}),
        ("Guasto in metropolitana, servizio sostitutivo", {"METRO"}),
        ("linee urbane e suburbane", set()),
    ],
)
def test_lines_in_text(text, expected):
    assert lines_in_text(text) == expected


def test_distance_km():
    assert distance_km(45.0703, 7.6869) < 0.01
    assert 25 < distance_km(45.055, 7.3627) < 27  # Giaveno


def test_dates_share_month_in_ranges():
    today = date(2026, 10, 9)
    assert dates_in_text("Da Martedi' 13 a sabato 24 ottobre 2026", today) == [date(2026, 10, 13), date(2026, 10, 24)]
    assert dates_in_text("lunedì 5 e martedì 6 ottobre", today) == [date(2026, 10, 5), date(2026, 10, 6)]
    assert dates_in_text("giovedì 8 ottobre2026", today) == [date(2026, 10, 8)]
    assert dates_in_text("Linee 13 e 15 deviate", today) == []
