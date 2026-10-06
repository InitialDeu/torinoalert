"""SMAT (acqua): news dal sito WordPress, solo avvisi all'utenza e disservizi."""
import json
import re

from ..events import Event
from ..text import strip_html

URL = "https://www.smatorino.it/wp-json/wp/v2/posts?per_page=20&_fields=id,date,link,title,excerpt,categories"
SOURCE = "ACQUA (SMAT)"
AVVISI_CATEGORY = 106

SERIOUS = ("non potabil", "crisi idrica", "mancanza d", "senza acqua")
# Solo disservizi concreti: le news SMAT sono soprattutto comunicati istituzionali.
KEYWORDS = ("interruz", "sospension", "mancanza d", "senz'acqua", "senza acqua", "guasto", "non potabil",
            "torbid", "bassa pressione", "crisi idrica", "ordinanza")
# "Sportelli SMAT di Pinerolo chiusi", "chiusura degli sportelli"
_OFFICES_CLOSED = re.compile(r"sportell\w*\b.{0,60}\bchius|chiusur\w*\b.{0,30}\bsportell", re.IGNORECASE)


def parse(data: bytes) -> list[Event]:
    events = []
    for post in json.loads(data):
        title = strip_html((post.get("title") or {}).get("rendered", ""))
        excerpt = strip_html((post.get("excerpt") or {}).get("rendered", ""))
        text = f"{title} {excerpt}".lower()
        relevant = any(k in text for k in KEYWORDS) or _OFFICES_CLOSED.search(text)
        if AVVISI_CATEGORY not in (post.get("categories") or []) and not relevant:
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
