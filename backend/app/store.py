from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class DatasetRecord:
    dataset_id: str
    source_label: str
    original_df: pd.DataFrame
    active_df: pd.DataFrame
    column_mapping: dict[str, str]
    schema_text: str
    created_at: float = field(default_factory=time.time)


class DatasetStore:
    def __init__(self) -> None:
        self._datasets: dict[str, DatasetRecord] = {}

    def create(
        self,
        source_label: str,
        original_df: pd.DataFrame,
        active_df: pd.DataFrame,
        column_mapping: dict[str, str],
        schema_text: str,
    ) -> DatasetRecord:
        record = DatasetRecord(
            dataset_id=uuid.uuid4().hex,
            source_label=source_label,
            original_df=original_df,
            active_df=active_df,
            column_mapping=column_mapping,
            schema_text=schema_text,
        )
        self._datasets[record.dataset_id] = record
        return record

    def get(self, dataset_id: str) -> DatasetRecord:
        record = self._datasets.get(dataset_id)
        if not record:
            raise KeyError(dataset_id)
        return record


dataset_store = DatasetStore()

