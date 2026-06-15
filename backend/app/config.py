from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(ROOT_DIR, ".env"), override=False)


@dataclass(frozen=True)
class Settings:
    app_name: str = "Conversational Data Intelligence API"
    api_prefix: str = "/api"
    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    cors_origins: tuple[str, ...] = tuple(
        origin.strip() for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if origin.strip()
    )
    root_dir: str = ROOT_DIR
    data_dir: str = os.path.join(ROOT_DIR, "backend", "storage")
    datasets_dir: str = os.path.join(ROOT_DIR, "backend", "storage", "datasets")
    allow_signups: bool = os.getenv("ALLOW_SIGNUPS", "true").strip().lower() in {"1", "true", "yes", "on"}
    session_hours: int = int(os.getenv("SESSION_HOURS", "24") or "24")
    admin_email: str = os.getenv("ADMIN_EMAIL", "").strip()
    admin_password: str = os.getenv("ADMIN_PASSWORD", "").strip()
    firebase_service_account_json: str = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    firebase_service_account_path: str = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "").strip()
    firebase_database_url: str = os.getenv("FIREBASE_DATABASE_URL", "").strip()
    firebase_storage_bucket: str = os.getenv("FIREBASE_STORAGE_BUCKET", "").strip()
    firestore_database_id: str = os.getenv("FIRESTORE_DATABASE_ID", "(default)").strip() or "(default)"
    firestore_users_collection: str = os.getenv("FIRESTORE_USERS_COLLECTION", "users").strip() or "users"
    firestore_sessions_collection: str = os.getenv("FIRESTORE_SESSIONS_COLLECTION", "sessions").strip() or "sessions"
    firestore_datasets_collection: str = os.getenv("FIRESTORE_DATASETS_COLLECTION", "datasets").strip() or "datasets"
    firestore_dataset_manifests_collection: str = os.getenv("FIRESTORE_DATASET_MANIFESTS_COLLECTION", "dataset_manifests").strip() or "dataset_manifests"
    firestore_dataset_chunks_collection: str = os.getenv("FIRESTORE_DATASET_CHUNKS_COLLECTION", "dataset_chunks").strip() or "dataset_chunks"


settings = Settings()
