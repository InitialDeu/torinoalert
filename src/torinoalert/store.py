"""
Stato di deduplica: chiave -> attributi (str/int).
Gli eventi hanno `expires_at` (TTL DynamoDB) e `fp`; le chiavi `__meta__:*`
(bootstrap e salute fonti) non scadono.
"""
import json
import os
import time
from pathlib import Path


class MemoryStore:
    def __init__(self, data: dict | None = None):
        self.data = {k: dict(v) for k, v in (data or {}).items()}

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        return {k: dict(self.data[k]) for k in dict.fromkeys(keys) if k in self.data}

    def put(self, key: str, item: dict) -> None:
        self.data[key] = dict(item)

    def flush(self) -> None:
        pass


class FileStore(MemoryStore):
    """Stato su file JSON, per il runner locale."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        data = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and all(isinstance(v, dict) for v in loaded.values()):
                    data = loaded
            except ValueError:
                pass
        super().__init__(data)

    def flush(self) -> None:
        now = int(time.time())
        live = {k: v for k, v in self.data.items() if int(v.get("expires_at", now + 1)) > now}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(live, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def _to_ddb(item: dict) -> dict:
    return {k: {"N": str(v)} if isinstance(v, int) else {"S": str(v)} for k, v in item.items()}


def _from_ddb(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        if "N" in v:
            out[k] = int(v["N"])
        elif "S" in v:
            out[k] = v["S"]
    return out


class DynamoStore:
    KEY = "event_id"

    def __init__(self, table: str, client=None):
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client("dynamodb", config=Config(retries={"max_attempts": 5, "mode": "adaptive"}))
        self.table = table
        self.client = client

    def get_many(self, keys: list[str]) -> dict[str, dict]:
        """Una BatchGetItem ogni 100 chiavi invece di una GetItem per evento."""
        out = {}
        uniq = list(dict.fromkeys(keys))
        for i in range(0, len(uniq), 100):
            request = {self.table: {"Keys": [{self.KEY: {"S": k}} for k in uniq[i:i + 100]]}}
            for attempt in range(6):
                resp = self.client.batch_get_item(RequestItems=request)
                for item in resp.get("Responses", {}).get(self.table, []):
                    data = _from_ddb(item)
                    out[data.pop(self.KEY)] = data
                request = resp.get("UnprocessedKeys") or {}
                if not request:
                    break
                time.sleep(0.1 * 2 ** attempt)
            else:
                raise RuntimeError("DynamoDB: chiavi non processate dopo i retry")
        return out

    def put(self, key: str, item: dict) -> None:
        self.client.put_item(TableName=self.table, Item=_to_ddb({self.KEY: key, **item}))

    def flush(self) -> None:
        pass
