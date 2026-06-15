from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    app_name: str
    auth_mode: str
    allow_signups: bool
    llm_configured: bool


class UserAuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    full_name: str | None = Field(default=None, max_length=120)


class AuthResponse(BaseModel):
    token: str
    user: dict[str, Any]
    expires_at: float


class UrlLoadRequest(BaseModel):
    url: str


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class FilterRequest(BaseModel):
    filters: list[dict[str, Any]]


class DatasetSummary(BaseModel):
    dataset_id: str
    source_label: str
    row_count: int
    column_count: int
    schema_text: str
    preview: list[dict[str, Any]]
    health: dict[str, Any]
    columns: list[dict[str, Any]]
    suggested_questions: list[str]


class DatasetListItem(BaseModel):
    id: str
    source_label: str
    row_count: int
    column_count: int
    created_at: float


class QueryResponse(BaseModel):
    question: str
    answer: str
    sql: str
    explanation: str
    attempts: int
    runtime_ms: float
    rows_returned: int
    result: list[dict[str, Any]]
