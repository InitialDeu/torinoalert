import os
from dataclasses import dataclass


def _bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Un evento resta "visto" finché compare nelle fonti, più questo margine.
    dedup_ttl_days: int = 7
    # Tetto di messaggi per esecuzione: evita raffiche e i 429 di Telegram.
    max_sends_per_run: int = 10
    send_interval: float = 1.0
    max_retry_wait: float = 10.0
    http_timeout: float = 8.0
    collect_timeout: float = 20.0
    # Esecuzioni consecutive fallite prima di avvisare la chat admin.
    health_alert_after: int = 15
    # Alla prima raccolta di una fonte registra gli eventi senza inviarli.
    bootstrap_silent: bool = True
    # Zone di allerta ARPA (Torino città = Piem-L).
    arpa_zones: tuple[str, ...] = ("Piem-L",)

    @property
    def dedup_ttl_seconds(self) -> int:
        return self.dedup_ttl_days * 86400

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        d = cls()
        return cls(
            dedup_ttl_days=int(env.get("DEDUP_TTL_DAYS", d.dedup_ttl_days)),
            max_sends_per_run=int(env.get("MAX_SENDS_PER_RUN", d.max_sends_per_run)),
            send_interval=float(env.get("SEND_INTERVAL", d.send_interval)),
            max_retry_wait=float(env.get("MAX_RETRY_WAIT", d.max_retry_wait)),
            http_timeout=float(env.get("HTTP_TIMEOUT", d.http_timeout)),
            collect_timeout=float(env.get("COLLECT_TIMEOUT", d.collect_timeout)),
            health_alert_after=int(env.get("HEALTH_ALERT_AFTER", d.health_alert_after)),
            bootstrap_silent=_bool(env.get("BOOTSTRAP_SILENT", str(d.bootstrap_silent))),
            arpa_zones=tuple(
                z.strip() for z in env.get("ARPA_ZONES", ",".join(d.arpa_zones)).split(",") if z.strip()
            ),
        )
