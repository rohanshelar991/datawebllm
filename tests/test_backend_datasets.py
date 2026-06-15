from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.app import db as db_module


class FakeDocumentSnapshot:
    def __init__(self, collection: "FakeCollection", doc_id: str) -> None:
        self._collection = collection
        self.id = doc_id

    @property
    def exists(self) -> bool:
        return self.id in self._collection.documents

    @property
    def reference(self) -> "FakeDocumentReference":
        return FakeDocumentReference(self._collection, self.id)

    def to_dict(self):
        payload = self._collection.documents.get(self.id)
        if payload is None:
            return None
        return dict(payload)


class FakeDocumentReference:
    def __init__(self, collection: "FakeCollection", doc_id: str) -> None:
        self._collection = collection
        self.id = doc_id

    def set(self, payload) -> None:
        self._collection.documents[self.id] = dict(payload)

    def get(self) -> FakeDocumentSnapshot:
        return FakeDocumentSnapshot(self._collection, self.id)

    def delete(self) -> None:
        self._collection.documents.pop(self.id, None)


class FakeQuery:
    def __init__(self, collection: "FakeCollection", filters=None) -> None:
        self._collection = collection
        self._filters = list(filters or [])

    def where(self, field: str, op: str, value):
        assert op == "=="
        return FakeQuery(self._collection, [*self._filters, (field, value)])

    def limit(self, count: int):
        return FakeLimitedQuery(self, count)

    def stream(self):
        for doc_id, payload in self._collection.documents.items():
            if all(payload.get(field) == value for field, value in self._filters):
                yield FakeDocumentSnapshot(self._collection, doc_id)


class FakeLimitedQuery:
    def __init__(self, query: FakeQuery, count: int) -> None:
        self._query = query
        self._count = count

    def stream(self):
        for index, snapshot in enumerate(self._query.stream()):
            if index >= self._count:
                break
            yield snapshot


class FakeCollection:
    def __init__(self) -> None:
        self.documents = {}

    def document(self, doc_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self, doc_id)

    def where(self, field: str, op: str, value):
        return FakeQuery(self).where(field, op, value)

    def stream(self):
        for doc_id in list(self.documents):
            yield FakeDocumentSnapshot(self, doc_id)


class FakeFirestoreClient:
    def __init__(self) -> None:
        self.collections = {}

    def collection(self, name: str) -> FakeCollection:
        if name not in self.collections:
            self.collections[name] = FakeCollection()
        return self.collections[name]


def test_cleanup_dataset_inventory_removes_duplicates_and_sample_data(tmp_path, monkeypatch) -> None:
    storage_dir = tmp_path / "storage"
    datasets_dir = storage_dir / "datasets"
    datasets_dir.mkdir(parents=True)

    monkeypatch.setattr(
        db_module,
        "settings",
        SimpleNamespace(
            data_dir=str(storage_dir),
            datasets_dir=str(datasets_dir),
            session_hours=24,
            admin_email="",
            admin_password="",
            firestore_users_collection="users",
            firestore_sessions_collection="sessions",
            firestore_datasets_collection="datasets",
        ),
    )
    monkeypatch.setattr(db_module, "_FIRESTORE_CLIENT", FakeFirestoreClient())

    db_module.init_db()
    user = db_module.create_user("person@example.com", "very-secret-password", "Person")

    old_file = datasets_dir / "old.parquet"
    old_file.write_text("old", encoding="utf-8")
    db_module.persist_dataset(user["id"], "environment-report.csv", str(old_file), "schema", 4, 8, {"city": "city"})

    new_file = datasets_dir / "new.parquet"
    new_file.write_text("new", encoding="utf-8")
    latest = db_module.persist_dataset(user["id"], "environment-report.csv", str(new_file), "schema", 5, 8, {"city": "city"})

    sample_file = datasets_dir / "sample.parquet"
    sample_file.write_text("sample", encoding="utf-8")
    db_module.persist_dataset(user["id"], "Sample dataset", str(sample_file), "schema", 100, 4, {"x": "x"})

    removed = db_module.cleanup_dataset_inventory(remove_sample_datasets=True)
    datasets = db_module.list_datasets(user["id"])

    assert removed == 2
    assert datasets == [
        {
            "id": latest["id"],
            "source_label": "environment-report.csv",
            "row_count": 5,
            "column_count": 8,
            "created_at": latest["created_at"],
        }
    ]
    assert not old_file.exists()
    assert new_file.exists()
    assert not sample_file.exists()
