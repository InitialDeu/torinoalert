import os
from dataclasses import dataclass


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Un evento resta "visto" finché compare nelle fonti, più questo margine.
    dedup_ttl_days: int = 7
    # Tetto di messaggi al canale per esecuzione: evita raffiche e i 429 di Telegram.
    max_sends_per_run: int = 10
    # Tetto di messaggi privati agli iscritti per esecuzione.
    max_dm_per_run: int = 30
    # Aggiornamenti dello stesso avviso senza cambio di gravità: al massimo uno ogni N minuti.
    update_min_interval_minutes: int = 60
    send_interval: float = 1.0
    max_retry_wait: float = 10.0
    http_timeout: float = 8.0
    collect_timeout: float = 20.0
    # Minuti di errori consecutivi di una fonte prima di avvisare la chat admin.
    health_alert_minutes: int = 30
    # Alla prima raccolta di una fonte registra gli eventi senza inviarli.
    bootstrap_silent: bool = True
    # Zone di allerta ARPA (Torino città = Piem-L).
    arpa_zones: tuple[str, ...] = ("Piem-L",)
    # Raggio attorno a Torino per gli eventi di traffico 5T.
    traffic_radius_km: float = 15.0
    # Fascia notturna (ora di Roma) in cui solo i CRIT suonano.
    quiet_start: int = 23
    quiet_end: int = 7
    # Ora (di Roma) del riepilogo mattutino.
    digest_hour: int = 7
    # Frequenza dello schedule: serve a decidere quali fonti "lente" interrogare.
    schedule_rate_minutes: int = 2

    @property
    def dedup_ttl_seconds(self) -> int:
        return self.dedup_ttl_days * 86400

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        d = cls()

        def get(name, cast, default):
            return cast(env[name]) if env.get(name, "") != "" else default

        return cls(
            dedup_ttl_days=get("DEDUP_TTL_DAYS", int, d.dedup_ttl_days),
            max_sends_per_run=get("MAX_SENDS_PER_RUN", int, d.max_sends_per_run),
            max_dm_per_run=get("MAX_DM_PER_RUN", int, d.max_dm_per_run),
            update_min_interval_minutes=get("UPDATE_MIN_INTERVAL_MINUTES", int, d.update_min_interval_minutes),
            send_interval=get("SEND_INTERVAL", float, d.send_interval),
            max_retry_wait=get("MAX_RETRY_WAIT", float, d.max_retry_wait),
            http_timeout=get("HTTP_TIMEOUT", float, d.http_timeout),
            collect_timeout=get("COLLECT_TIMEOUT", float, d.collect_timeout),
            health_alert_minutes=get("HEALTH_ALERT_MINUTES", int, d.health_alert_minutes),
            bootstrap_silent=get("BOOTSTRAP_SILENT", _bool, d.bootstrap_silent),
            arpa_zones=tuple(
                z.strip() for z in env.get("ARPA_ZONES", ",".join(d.arpa_zones)).split(",") if z.strip()
            ),
            traffic_radius_km=get("TRAFFIC_RADIUS_KM", float, d.traffic_radius_km),
            quiet_start=get("QUIET_START", int, d.quiet_start),
            quiet_end=get("QUIET_END", int, d.quiet_end),
            digest_hour=get("DIGEST_HOUR", int, d.digest_hour),
            schedule_rate_minutes=get("SCHEDULE_RATE_MINUTES", int, d.schedule_rate_minutes),
        )
