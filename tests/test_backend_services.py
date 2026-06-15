from __future__ import annotations

import pandas as pd

from backend.app import services as services_module


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, payload: bytes, content_type: str | None = None) -> None:
        self.bucket.objects[self.name] = payload

    def download_as_bytes(self) -> bytes:
        return self.bucket.objects[self.name]

    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def delete(self) -> None:
        self.bucket.objects.pop(self.name, None)


class FakeBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.objects: dict[str, bytes] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)


def test_save_and_load_dataset_uses_firebase_storage(monkeypatch) -> None:
    bucket = FakeBucket("unit-test-bucket")
    persisted_rows = []

    monkeypatch.setattr(services_module, "get_storage_bucket", lambda: bucket)
    monkeypatch.setattr(services_module, "delete_datasets_by_source_label", lambda user_id, source_label: 0)

    def fake_persist_dataset(**kwargs):
        persisted_rows.append(kwargs)
        return {
            "id": "dataset-1",
            "created_at": 123.0,
            **kwargs,
        }

    monkeypatch.setattr(services_module, "persist_dataset", fake_persist_dataset)

    original_df = pd.DataFrame({"City": ["Mumbai", "Delhi"], "AQI": [152, 204]})
    active_df = pd.DataFrame({"city": ["Mumbai", "Delhi"], "aqi": [152, 204]})

    saved = services_module.save_dataset_for_user(
        user_id="user-1",
        source_label="air.csv",
        original_df=original_df,
        active_df=active_df,
        column_mapping={"City": "city", "AQI": "aqi"},
        schema_text="- city (VARCHAR)\n- aqi (BIGINT)",
    )

    assert saved["file_path"].startswith("gs://unit-test-bucket/datasets/user-1/")
    assert len(bucket.objects) == 1
    stored_row = {
        "file_path": saved["file_path"],
        "column_mapping": {"City": "city", "AQI": "aqi"},
        "schema_text": "- city (VARCHAR)\n- aqi (BIGINT)",
    }

    loaded_df, column_mapping, schema_text = services_module.load_persisted_dataset(stored_row)

    assert loaded_df.to_dict(orient="records") == active_df.to_dict(orient="records")
    assert column_mapping == {"City": "city", "AQI": "aqi"}
    assert schema_text == "- city (VARCHAR)\n- aqi (BIGINT)"


def test_save_dataset_falls_back_to_firestore_blob_storage(monkeypatch) -> None:
    stored_payloads = {}

    monkeypatch.setattr(services_module, "get_storage_bucket", lambda: None)
    monkeypatch.setattr(services_module, "delete_datasets_by_source_label", lambda user_id, source_label: 0)
    def fake_store_dataset_bytes(payload: bytes) -> str:
        stored_payloads["payload"] = payload
        return "firestore://blob-1"

    monkeypatch.setattr(services_module, "store_dataset_bytes", fake_store_dataset_bytes)
    monkeypatch.setattr(services_module, "load_dataset_bytes", lambda file_path: stored_payloads["payload"])

    def fake_persist_wrapper(**kwargs):
        return {
            "id": "dataset-2",
            "created_at": 456.0,
            **kwargs,
        }

    monkeypatch.setattr(services_module, "persist_dataset", fake_persist_wrapper)

    original_df = pd.DataFrame({"City": ["London"], "AQI": [73]})
    active_df = pd.DataFrame({"city": ["London"], "aqi": [73]})

    saved = services_module.save_dataset_for_user(
        user_id="user-2",
        source_label="city.csv",
        original_df=original_df,
        active_df=active_df,
        column_mapping={"City": "city", "AQI": "aqi"},
        schema_text="- city (VARCHAR)\n- aqi (BIGINT)",
    )

    assert saved["file_path"] == "firestore://blob-1"
    loaded_df, column_mapping, schema_text = services_module.load_persisted_dataset(
        {
            "file_path": saved["file_path"],
            "column_mapping": {"City": "city", "AQI": "aqi"},
            "schema_text": "- city (VARCHAR)\n- aqi (BIGINT)",
        }
    )
    assert loaded_df.to_dict(orient="records") == active_df.to_dict(orient="records")
    assert column_mapping == {"City": "city", "AQI": "aqi"}
    assert schema_text == "- city (VARCHAR)\n- aqi (BIGINT)"
