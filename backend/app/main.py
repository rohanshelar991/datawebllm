from __future__ import annotations

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import (
    authenticate_user,
    cleanup_dataset_inventory,
    count_users,
    create_session,
    create_user,
    delete_session,
    get_dataset,
    get_user_by_token,
    init_db,
    list_datasets,
    seed_admin_user,
)
from .schemas import (
    AuthResponse,
    DatasetListItem,
    DatasetSummary,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    UrlLoadRequest,
    UserAuthRequest,
)
from .services import (
    build_dataset_summary,
    create_dataset_record_from_upload,
    create_dataset_record_from_url,
    execute_question,
    load_persisted_dataset,
    save_dataset_for_user,
)


app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins) or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    init_db()
    cleanup_dataset_inventory(remove_sample_datasets=True)
    seed_admin_user()


def _public_user_shape(user: dict) -> dict:
    return {
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "created_at": user["created_at"],
    }


def get_current_user(authorization: str | None = Header(default=None)) -> dict:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


@app.get(f"{settings.api_prefix}/health", response_model=HealthResponse)
def health() -> HealthResponse:
    auth_mode = "session"
    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        auth_mode=auth_mode,
        allow_signups=settings.allow_signups,
        llm_configured=bool(settings.groq_api_key),
    )


@app.post(f"{settings.api_prefix}/auth/register", response_model=AuthResponse)
def register(body: UserAuthRequest) -> AuthResponse:
    if not settings.allow_signups and count_users() > 0:
        raise HTTPException(status_code=403, detail="Sign-ups are disabled.")
    try:
        user = create_user(body.email, body.password, body.full_name or body.email.split("@")[0])
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Unable to create account. Email may already exist.") from exc
    session = create_session(user["id"])
    return AuthResponse(token=session["token"], user=_public_user_shape(user), expires_at=session["expires_at"])


@app.post(f"{settings.api_prefix}/auth/login", response_model=AuthResponse)
def login(body: UserAuthRequest) -> AuthResponse:
    user = authenticate_user(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    session = create_session(user["id"])
    return AuthResponse(token=session["token"], user=_public_user_shape(user), expires_at=session["expires_at"])


@app.get(f"{settings.api_prefix}/auth/me")
def me(current_user: dict = Depends(get_current_user)) -> dict:
    return {"user": _public_user_shape(current_user)}


@app.post(f"{settings.api_prefix}/auth/logout")
def logout(authorization: str | None = Header(default=None), current_user: dict = Depends(get_current_user)) -> dict:
    if authorization and authorization.lower().startswith("bearer "):
        delete_session(authorization.split(" ", 1)[1].strip())
    return {"status": "ok"}


@app.get(f"{settings.api_prefix}/datasets", response_model=list[DatasetListItem])
def datasets(current_user: dict = Depends(get_current_user)) -> list[DatasetListItem]:
    return [DatasetListItem(**item) for item in list_datasets(current_user["id"])]


@app.post(f"{settings.api_prefix}/datasets/sample", response_model=DatasetSummary)
def load_sample_dataset(current_user: dict = Depends(get_current_user)) -> DatasetSummary:
    original_df, active_df, mapping, schema_text = create_dataset_record_from_url(
        "https://github.com/Geo-y20/Telco-Customer-Churn-/blob/main/Telco%20Customer%20Churn.csv",
        "Sample dataset",
    )
    dataset_row = save_dataset_for_user(current_user["id"], "Sample dataset", original_df, active_df, mapping, schema_text)
    return DatasetSummary(**build_dataset_summary(dataset_row["id"], dataset_row["source_label"], active_df, schema_text))


@app.post(f"{settings.api_prefix}/datasets/upload", response_model=DatasetSummary)
async def upload_dataset(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)) -> DatasetSummary:
    payload = await file.read()
    source_label = file.filename or "Uploaded file"
    original_df, active_df, mapping, schema_text = create_dataset_record_from_upload(payload, source_label, source_label)
    dataset_row = save_dataset_for_user(current_user["id"], source_label, original_df, active_df, mapping, schema_text)
    return DatasetSummary(**build_dataset_summary(dataset_row["id"], dataset_row["source_label"], active_df, schema_text))


@app.post(f"{settings.api_prefix}/datasets/from-url", response_model=DatasetSummary)
def load_dataset_from_url(body: UrlLoadRequest, current_user: dict = Depends(get_current_user)) -> DatasetSummary:
    original_df, active_df, mapping, schema_text = create_dataset_record_from_url(body.url, "Remote dataset")
    dataset_row = save_dataset_for_user(current_user["id"], "Remote dataset", original_df, active_df, mapping, schema_text)
    return DatasetSummary(**build_dataset_summary(dataset_row["id"], dataset_row["source_label"], active_df, schema_text))


@app.get(f"{settings.api_prefix}/datasets/{{dataset_id}}", response_model=DatasetSummary)
def get_dataset_by_id(dataset_id: str, current_user: dict = Depends(get_current_user)) -> DatasetSummary:
    dataset_row = get_dataset(dataset_id, current_user["id"])
    if not dataset_row:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    active_df, _mapping, schema_text = load_persisted_dataset(dataset_row)
    return DatasetSummary(**build_dataset_summary(dataset_row["id"], dataset_row["source_label"], active_df, schema_text))


@app.post(f"{settings.api_prefix}/datasets/{{dataset_id}}/query", response_model=QueryResponse)
def query_dataset(dataset_id: str, body: QueryRequest, current_user: dict = Depends(get_current_user)) -> QueryResponse:
    dataset_row = get_dataset(dataset_id, current_user["id"])
    if not dataset_row:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    active_df, column_mapping, schema_text = load_persisted_dataset(dataset_row)
    try:
        response = execute_question(active_df, schema_text, column_mapping, body.question)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return QueryResponse(**response)
