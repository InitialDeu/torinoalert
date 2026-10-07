from collections.abc import Callable
from dataclasses import dataclass

from ..config import Settings
from ..events import Event
from ..http import fetch_bytes, fetch_text
from . import aria, arpa, cittametro, comune, fiumi, gtt, ingv, mato, muoversi, rfi, scioperi, sitaf, smat, trenitalia


@dataclass(frozen=True)
class Source:
    collect: Callable[[], list[Event]]
    # Ogni quanti minuti interrogarla: i siti pesanti o lenti a cambiare non ogni 2 minuti.
    every_minutes: int = 2


def all_sources(settings: Settings) -> dict[str, Source]:
    """Nome fonte -> come scaricarla e interpretarla. Ogni fonte è indipendente."""
    t = settings.http_timeout
    return {
        "ARPA": Source(lambda: arpa.parse(fetch_bytes(arpa.URL, t), zones=settings.arpa_zones), 10),
        "GTT_RT": Source(lambda: gtt.parse_alerts(fetch_bytes(gtt.ALERTS_URL, t)), 2),
        "GTT_NEWS": Source(lambda: gtt.parse_news(fetch_bytes(gtt.NEWS_URL, t)), 10),
        "RFI": Source(lambda: rfi.parse(fetch_bytes(rfi.URL, t)), 2),
        "TRENITALIA": Source(lambda: trenitalia.parse(fetch_text(trenitalia.URL, t)), 10),
        "SCIOPERI": Source(lambda: scioperi.parse(fetch_bytes(scioperi.URL, t)), 30),
        "TRAFFICO_5T": Source(lambda: muoversi.parse(fetch_text(muoversi.URL, t), settings.traffic_radius_km), 10),
        "STRADE_PROVINCIALI": Source(lambda: cittametro.parse(fetch_text(cittametro.URL, t)), 30),
        "COMUNE_VIABILITA": Source(lambda: comune.parse_viabilita(fetch_text(comune.COMUNICATI_URL, t)), 10),
        "COMUNE_SMOG": Source(lambda: comune.parse_smog(fetch_text(comune.AVVISI_URL, t)), 30),
        "SEMAFORO_ANTISMOG": Source(lambda: aria.parse_semaforo(fetch_bytes(aria.SEMAFORO_URL, t)), 30),
        "CALDO": Source(lambda: aria.parse_caldo(fetch_bytes(aria.CALDO_URL, t)), 60),
        "TERREMOTI": Source(lambda: ingv.parse(fetch_bytes(ingv.url(), t)), 2),
        "SMAT": Source(lambda: smat.parse(fetch_bytes(smat.URL, t)), 30),
        "FIUMI": Source(lambda: fiumi.collect(fetch_bytes, t), 10),
        "VIABILITA_TORINO": Source(lambda: mato.parse_viabilita(fetch_text(mato.VIABILITA_URL, t)), 10),
        "AEROPORTO": Source(lambda: mato.parse_aeroporto(fetch_text(mato.HOME_URL, t)), 10),
        "SITAF_A32": Source(lambda: sitaf.parse(fetch_text(sitaf.A32_URL, t), "A32"), 60),
        "SITAF_FREJUS": Source(lambda: sitaf.parse(fetch_text(sitaf.T4_URL, t), "Traforo del Frejus"), 60),
    }


def due_sources(sources: dict[str, Source], now: float, rate_minutes: int) -> dict[str, Callable[[], list[Event]]]:
    """
    Fonti da interrogare in questo giro: una fonte "ogni N minuti" gira quando
    il minuto corrente cade nella finestra iniziale di N (senza stato da salvare).
    """
    minute = int(now // 60)
    return {
        name: src.collect
        for name, src in sources.items()
        if src.every_minutes <= rate_minutes or minute % src.every_minutes < rate_minutes
    }


def default_sources(settings: Settings) -> dict[str, Callable[[], list[Event]]]:
    """Tutte le fonti, senza filtro di frequenza (runner locale, riepilogo, /oggi)."""
    return {name: src.collect for name, src in all_sources(settings).items()}


def weather_line(settings: Settings) -> str:
    return muoversi.weather_line(fetch_text(muoversi.URL, settings.http_timeout))
