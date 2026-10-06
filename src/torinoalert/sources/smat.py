"""SMAT (acqua): news dal sito WordPress, solo avvisi all'utenza e disservizi."""
import json

from ..events import Event
from ..text import strip_html

URL = "https://www.smatorino.it/wp-json/wp/v2/posts?per_page=20&_fields=id,date,link,title,excerpt,categories"
SOURCE = "ACQUA (SMAT)"
AVVISI_CATEGORY = 106

SERIOUS = ("non potabil", "crisi idrica", "mancanza d", "senza acqua")
# Solo disservizi concreti: le news SMAT sono soprattutto comunicati istituzionali.
KEYWORDS = ("interruz", "sospension", "mancanza d", "senz'acqua", "senza acqua", "guasto", "non potabil",
            "torbid", "bassa pressione", "crisi idrica", "ordinanza", "sportelli chiusi", "chiusura sportell")


def parse(data: bytes) -> list[Event]:
    events = []
    for post in json.loads(data):
        title = strip_html((post.get("title") or {}).get("rendered", ""))
        excerpt = strip_html((post.get("excerpt") or {}).get("rendered", ""))
        text = f"{title} {excerpt}".lower()
        if AVVISI_CATEGORY not in (post.get("categories") or []) and not any(k in text for k in KEYWORDS):
            continue
        severity = "HIGH" if any(k in text for k in SERIOUS) else "MED"
        events.append(Event(
            id=f"smat:{post.get('id')}",
            source=SOURCE,
            severity=severity,
            title=title,
            body=excerpt,
            link=post.get("link") or "https://www.smatorino.it",
        ))
    return events
