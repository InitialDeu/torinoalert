import json
from datetime import UTC, datetime


def log(event_type: str, **kwargs) -> None:
    """Log strutturato su una riga JSON (leggibile da CloudWatch Logs Insights)."""
    print(json.dumps({
        "type": event_type,
        "ts": datetime.now(UTC).isoformat(),
        **kwargs,
    }, ensure_ascii=False, default=str))
