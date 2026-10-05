import time

from torinoalert.store import DynamoStore, FileStore, _to_ddb


def test_file_store_roundtrip_and_prunes_expired(tmp_path):
    path = tmp_path / "state.json"
    store = FileStore(path)
    now = int(time.time())
    store.put("live", {"fp": "x", "expires_at": now + 100})
    store.put("dead", {"fp": "", "expires_at": now - 1})
    store.put("__meta__:bootstrap:S", {"created_at": now})
    store.flush()

    again = FileStore(path)
    assert set(again.data) == {"live", "__meta__:bootstrap:S"}
    assert again.get_many(["live", "missing"]) == {"live": {"fp": "x", "expires_at": now + 100}}


def test_file_store_ignores_legacy_format(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text('{"abc": "2026-01-01T00:00:00"}', encoding="utf-8")
    assert FileStore(path).data == {}


class FakeDynamo:
    def __init__(self, items, unprocessed_once=False):
        self.items = items
        self.unprocessed_once = unprocessed_once
        self.calls = 0
        self.puts = []

    def batch_get_item(self, RequestItems):
        self.calls += 1
        (table, req), = RequestItems.items()
        keys = [k["event_id"]["S"] for k in req["Keys"]]
        assert len(keys) == len(set(keys)) <= 100
        if self.unprocessed_once and self.calls == 1:
            return {"Responses": {table: []}, "UnprocessedKeys": RequestItems}
        found = [_to_ddb({"event_id": k, **self.items[k]}) for k in keys if k in self.items]
        return {"Responses": {table: found}, "UnprocessedKeys": {}}

    def put_item(self, TableName, Item):
        self.puts.append(Item)


def test_dynamo_store_batches_dedups_and_retries_unprocessed():
    client = FakeDynamo({"k1": {"fp": "a", "expires_at": 5}}, unprocessed_once=True)
    store = DynamoStore("t", client=client)
    keys = [f"k{i}" for i in range(150)] + ["k1"]
    assert store.get_many(keys) == {"k1": {"fp": "a", "expires_at": 5}}
    assert client.calls == 3  # 1 non processata + 2 batch da 100/50


def test_dynamo_store_put_types():
    client = FakeDynamo({})
    DynamoStore("t", client=client).put("k", {"fp": "x", "expires_at": 7})
    assert client.puts == [{"event_id": {"S": "k"}, "fp": {"S": "x"}, "expires_at": {"N": "7"}}]
