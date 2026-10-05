from collections.abc import Callable

from ..config import Settings
from ..events import Event
from ..http import fetch_bytes, fetch_text
from . import arpa, comune, gtt, rfi


def default_sources(settings: Settings) -> dict[str, Callable[[], list[Event]]]:
    """Nome fonte -> funzione che scarica e interpreta. Ogni fonte è indipendente."""
    t = settings.http_timeout
    return {
        "ARPA": lambda: arpa.parse(fetch_bytes(arpa.URL, t), zones=settings.arpa_zones),
        "GTT_LIVE": lambda: gtt.parse_live(fetch_text(gtt.LIVE_URL, t)),
        "GTT_NEWS": lambda: gtt.parse_news(fetch_bytes(gtt.NEWS_URL, t)),
        "RFI": lambda: rfi.parse(fetch_bytes(rfi.URL, t)),
        "COMUNE_VIABILITA": lambda: comune.parse_viabilita(fetch_text(comune.COMUNICATI_URL, t)),
        "COMUNE_SMOG": lambda: comune.parse_smog(fetch_text(comune.AVVISI_URL, t)),
    }
