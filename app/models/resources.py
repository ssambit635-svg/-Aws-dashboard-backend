"""Domain models for AWS resources: S3 buckets, EC2 instances, CloudWatch alarms."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.models.common import Money


class StorageClass(StrEnum):
    STANDARD = "STANDARD"
    STANDARD_IA = "STANDARD_IA"
    INTELLIGENT_TIERING = "INTELLIGENT_TIERING"
    GLACIER = "GLACIER"
    DEEP_ARCHIVE = "DEEP_ARCHIVE"


class Bucket(BaseModel):
    name: str
    region: str
    size_bytes: int = Field(ge=0)
    object_count: int = Field(ge=0)
    storage_class: StorageClass = StorageClass.STANDARD
    created_at: datetime
    versioning: bool = False
    encrypted: bool = True
    public_access_blocked: bool = True
    monthly_cost: Money

    @property
    def size_human(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
            if size < 1024 or unit == "PB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} PB"


InstanceState = Literal["running", "stopped", "pending", "stopping", "terminated"]


class Instance(BaseModel):
    id: str = Field(pattern=r"^i-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
    type: str
    state: InstanceState
    availability_zone: str
    private_ip: str
    public_ip: str | None = None
    launch_time: datetime
    cpu_utilization_pct: float = Field(ge=0, le=100)
    memory_used_pct: float = Field(ge=0, le=100)
    tags: dict[str, str] = {}
    monthly_cost: Money

    @property
    def name(self) -> str:
        return self.tags.get("Name", self.id)


class AlarmState(StrEnum):
    OK = "OK"
    ALARM = "ALARM"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Severity(StrEnum):
    critical = "critical"
    warning = "warning"
    info = "info"


class Alarm(BaseModel):
    name: str
    state: AlarmState
    severity: Severity
    namespace: str
    metric: str
    threshold: float
    current_value: float
    comparison: Literal["GreaterThanThreshold", "LessThanThreshold"] = "GreaterThanThreshold"
    updated_at: datetime


class BucketList(BaseModel):
    items: list[Bucket]
    total_size_bytes: int
    total_objects: int
    total_monthly_cost: Money


class InstanceList(BaseModel):
    items: list[Instance]
    by_state: dict[str, int]


class AlarmList(BaseModel):
    items: list[Alarm]
    by_state: dict[str, int]
