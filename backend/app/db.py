from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import firebase_admin
from firebase_admin import credentials, firestore, storage

from .auth import hash_password, issue_session_token, normalize_email, verify_password
from .config import settings

_FIRESTORE_CLIENT: firestore.Client | None = None
DATASET_CHUNK_SIZE = 700_000


def ensure_storage() -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.datasets_dir, exist_ok=True)


def _load_firebase_credential() -> credentials.Base | None:
    if settings.firebase_service_account_json:
        payload = json.loads(settings.firebase_service_account_json)
        return credentials.Certificate(payload)
    if settings.firebase_service_account_path:
        return credentials.Certificate(settings.firebase_service_account_path)
    return None


def get_firestore_client() -> firestore.Client:
    global _FIRESTORE_CLIENT

    if _FIRESTORE_CLIENT is not None:
        return _FIRESTORE_CLIENT

    try:
        app = firebase_admin.get_app()
    except ValueError:
        credential = _load_firebase_credential()
        init_kwargs: dict[str, Any] = {}
        if settings.firebase_database_url:
            init_kwargs["databaseURL"] = settings.firebase_database_url
        if settings.firebase_storage_bucket:
            init_kwargs["storageBucket"] = settings.firebase_storage_bucket
        if credential is not None:
            app = firebase_admin.initialize_app(credential, init_kwargs)
        else:
            app = firebase_admin.initialize_app(options=init_kwargs or None)

    _FIRESTORE_CLIENT = firestore.client(app=app)
    return _FIRESTORE_CLIENT


def init_db() -> None:
    ensure_storage()
    try:
        get_firestore_client()
    except Exception as exc:  # pragma: no cover - startup validation
        raise RuntimeError(
            "Unable to initialize Firestore. Set FIREBASE_SERVICE_ACCOUNT_JSON or "
            "FIREBASE_SERVICE_ACCOUNT_PATH for deployment-ready cloud persistence."
        ) from exc


def _users():
    return get_firestore_client().collection(settings.firestore_users_collection)


def _sessions():
    return get_firestore_client().collection(settings.firestore_sessions_collection)


def _datasets():
    return get_firestore_client().collection(settings.firestore_datasets_collection)


def _dataset_manifests():
    return get_firestore_client().collection(settings.firestore_dataset_manifests_collection)


def _dataset_chunks():
    return get_firestore_client().collection(settings.firestore_dataset_chunks_collection)


def get_storage_bucket():
    if not settings.firebase_storage_bucket:
        return None
    try:
        app = firebase_admin.get_app()
    except ValueError:
        get_firestore_client()
        app = firebase_admin.get_app()
    return storage.bucket(app=app)


def _serialize_document(snapshot: firestore.DocumentSnapshot) -> dict[str, Any]:
    payload = snapshot.to_dict() or {}
    payload["id"] = snapshot.id
    return payload


def _parse_gs_path(file_path: str) -> tuple[str, str] | None:
    if not file_path.startswith("gs://"):
        return None
    remainder = file_path[5:]
    bucket_name, _, blob_name = remainder.partition("/")
    if not bucket_name or not blob_name:
        return None
    return bucket_name, blob_name


def _parse_firestore_blob_path(file_path: str) -> str | None:
    prefix = "firestore://"
    if not file_path.startswith(prefix):
        return None
    object_id = file_path[len(prefix):].strip("/")
    return object_id or None


def store_dataset_bytes(payload: bytes) -> str:
    object_id = uuid.uuid4().hex
    chunk_count = 0
    for index, start in enumerate(range(0, len(payload), DATASET_CHUNK_SIZE)):
        chunk = payload[start:start + DATASET_CHUNK_SIZE]
        _dataset_chunks().document(f"{object_id}_{index:06d}").set(
            {
                "object_id": object_id,
                "chunk_index": index,
                "payload": chunk,
            }
        )
        chunk_count += 1

    _dataset_manifests().document(object_id).set(
        {
            "chunk_count": chunk_count,
            "size_bytes": len(payload),
            "created_at": time.time(),
        }
    )
    return f"firestore://{object_id}"


def load_dataset_bytes(file_path: str) -> bytes:
    object_id = _parse_firestore_blob_path(file_path)
    if not object_id:
        raise ValueError("Not a Firestore dataset blob path.")

    manifest = _dataset_manifests().document(object_id).get()
    if not manifest.exists:
        raise FileNotFoundError(f"Dataset manifest not found for {object_id}.")

    chunk_count = int((manifest.to_dict() or {}).get("chunk_count", 0))
    payload = bytearray()
    for index in range(chunk_count):
        chunk_snapshot = _dataset_chunks().document(f"{object_id}_{index:06d}").get()
        if not chunk_snapshot.exists:
            raise FileNotFoundError(f"Dataset chunk {index} missing for {object_id}.")
        chunk_payload = (chunk_snapshot.to_dict() or {}).get("payload", b"")
        payload.extend(chunk_payload)
    return bytes(payload)


def delete_dataset_bytes(file_path: str) -> None:
    object_id = _parse_firestore_blob_path(file_path)
    if not object_id:
        return

    manifest = _dataset_manifests().document(object_id).get()
    if manifest.exists:
        chunk_count = int((manifest.to_dict() or {}).get("chunk_count", 0))
        for index in range(chunk_count):
            _dataset_chunks().document(f"{object_id}_{index:06d}").delete()
        _dataset_manifests().document(object_id).delete()


def _remove_dataset_file(file_path: str) -> None:
    gs_parts = _parse_gs_path(file_path)
    if gs_parts:
        bucket_name, blob_name = gs_parts
        bucket = get_storage_bucket()
        if bucket and bucket.name == bucket_name:
            blob = bucket.blob(blob_name)
            if blob.exists():
                blob.delete()
        return
    if _parse_firestore_blob_path(file_path):
        delete_dataset_bytes(file_path)
        return
    if file_path and os.path.exists(file_path):
        os.remove(file_path)


def seed_admin_user() -> None:
    if not settings.admin_email or not settings.admin_password:
        return
    if find_user_by_email(settings.admin_email):
        return
    create_user(settings.admin_email, settings.admin_password, "Admin")


def create_user(email: str, password: str, full_name: str) -> dict[str, Any]:
    normalized_email = normalize_email(email)
    if find_user_by_email(normalized_email):
        raise ValueError("User already exists.")

    user_id = uuid.uuid4().hex
    created_at = time.time()
    payload = {
        "email": normalized_email,
        "full_name": full_name.strip(),
        "password_hash": hash_password(password),
        "created_at": created_at,
    }
    _users().document(user_id).set(payload)
    return {"id": user_id, **payload}


def find_user_by_email(email: str) -> dict[str, Any] | None:
    normalized_email = normalize_email(email)
    docs = _users().where("email", "==", normalized_email).limit(1).stream()
    snapshot = next(iter(docs), None)
    return _serialize_document(snapshot) if snapshot else None


def find_user_by_id(user_id: str) -> dict[str, Any] | None:
    snapshot = _users().document(user_id).get()
    if not snapshot.exists:
        return None
    return _serialize_document(snapshot)


def count_users() -> int:
    return sum(1 for _ in _users().stream())


def authenticate_user(email: str, password: str) -> dict[str, Any] | None:
    user = find_user_by_email(email)
    if not user:
        return None
    if not verify_password(password, str(user["password_hash"])):
        return None
    return {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "created_at": user["created_at"],
    }


def create_session(user_id: str) -> dict[str, Any]:
    created_at = time.time()
    expires_at = created_at + max(settings.session_hours, 1) * 3600
    token = issue_session_token()
    _sessions().document(token).set(
        {
            "user_id": user_id,
            "created_at": created_at,
            "expires_at": expires_at,
        }
    )
    return {"token": token, "expires_at": expires_at}


def get_user_by_token(token: str) -> dict[str, Any] | None:
    if not token:
        return None

    session_snapshot = _sessions().document(token).get()
    if not session_snapshot.exists:
        return None

    session = _serialize_document(session_snapshot)
    if float(session["expires_at"]) < time.time():
        _sessions().document(token).delete()
        return None

    user = find_user_by_id(str(session["user_id"]))
    if not user:
        _sessions().document(token).delete()
        return None

    return {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "created_at": user["created_at"],
    }


def delete_session(token: str) -> None:
    if not token:
        return
    _sessions().document(token).delete()


def persist_dataset(
    user_id: str,
    source_label: str,
    file_path: str,
    schema_text: str,
    row_count: int,
    column_count: int,
    column_mapping: dict[str, str],
) -> dict[str, Any]:
    dataset_id = uuid.uuid4().hex
    created_at = time.time()
    payload = {
        "user_id": user_id,
        "source_label": source_label,
        "file_path": file_path,
        "schema_text": schema_text,
        "row_count": row_count,
        "column_count": column_count,
        "column_mapping": column_mapping,
        "created_at": created_at,
    }
    _datasets().document(dataset_id).set(payload)
    return {"id": dataset_id, **payload}


def delete_datasets_by_source_label(user_id: str, source_label: str) -> int:
    removed = 0
    for snapshot in _datasets().where("user_id", "==", user_id).where("source_label", "==", source_label).stream():
        payload = _serialize_document(snapshot)
        snapshot.reference.delete()
        _remove_dataset_file(str(payload.get("file_path", "")))
        removed += 1
    return removed


def cleanup_dataset_inventory(remove_sample_datasets: bool = False) -> int:
    removed = 0
    kept_keys: set[tuple[str, str]] = set()
    snapshots = sorted(
        list(_datasets().stream()),
        key=lambda item: (
            str((item.to_dict() or {}).get("user_id", "")),
            str((item.to_dict() or {}).get("source_label", "")),
            -float((item.to_dict() or {}).get("created_at", 0)),
            item.id,
        ),
    )

    for snapshot in snapshots:
        payload = _serialize_document(snapshot)
        key = (str(payload["user_id"]), str(payload["source_label"]))
        should_remove = key in kept_keys
        if remove_sample_datasets and str(payload["source_label"]) == "Sample dataset":
            should_remove = True
        if should_remove:
            snapshot.reference.delete()
            _remove_dataset_file(str(payload.get("file_path", "")))
            removed += 1
            continue
        kept_keys.add(key)

    return removed


def get_dataset(dataset_id: str, user_id: str) -> dict[str, Any] | None:
    snapshot = _datasets().document(dataset_id).get()
    if not snapshot.exists:
        return None
    payload = _serialize_document(snapshot)
    if str(payload["user_id"]) != user_id:
        return None
    return payload


def list_datasets(user_id: str) -> list[dict[str, Any]]:
    docs = sorted(
        [_serialize_document(snapshot) for snapshot in _datasets().where("user_id", "==", user_id).stream()],
        key=lambda item: float(item.get("created_at", 0)),
        reverse=True,
    )

    seen_labels: set[str] = set()
    unique_rows: list[dict[str, Any]] = []
    for item in docs:
        label = str(item["source_label"])
        if label in seen_labels:
            continue
        seen_labels.add(label)
        unique_rows.append(
            {
                "id": item["id"],
                "source_label": item["source_label"],
                "row_count": item["row_count"],
                "column_count": item["column_count"],
                "created_at": item["created_at"],
            }
        )
    return unique_rows
