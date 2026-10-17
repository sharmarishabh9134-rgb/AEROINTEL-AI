from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    model_config = ConfigDict(from_attributes=True)


class AlertCreate(BaseModel):
    severity: str = Field(pattern="^(low|medium|high|critical)$")
    category: str = Field(min_length=2, max_length=60)
    message: str = Field(min_length=4, max_length=2000)


class AircraftInput(BaseModel):
    resource_id: str = Field(min_length=2, max_length=30, pattern="^[A-Za-z0-9_-]+$")
    category: str = Field(min_length=2, max_length=80)
    availability_status: str = Field(pattern="^(available|standby|maintenance|unavailable)$")
    maintenance_status: str = Field(pattern="^(clear|open|in_progress|completed)$")
    readiness_confirmation: bool = False
    notes: str = Field(default="", max_length=2000)

    @field_validator("resource_id", mode="before")
    @classmethod
    def normalize_resource_id(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class AircraftUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=2, max_length=80)
    availability_status: str | None = Field(default=None, pattern="^(available|standby|maintenance|unavailable)$")
    maintenance_status: str | None = Field(default=None, pattern="^(clear|open|in_progress|completed)$")
    readiness_confirmation: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CrewInput(BaseModel):
    display_id: str = Field(min_length=2, max_length=30, pattern="^[A-Za-z0-9_-]+$")
    group_name: str = Field(min_length=2, max_length=80)
    readiness_status: str = Field(pattern="^(ready|review|unavailable)$")

    @field_validator("display_id", mode="before")
    @classmethod
    def normalize_display_id(cls, value):
        return value.strip().upper() if isinstance(value, str) else value


class CrewUpdate(BaseModel):
    group_name: str | None = Field(default=None, min_length=2, max_length=80)
    readiness_status: str | None = Field(default=None, pattern="^(ready|review|unavailable)$")


class MaintenanceInput(BaseModel):
    aircraft_id: int | None = None
    title: str = Field(min_length=4, max_length=180)
    status: str = Field(pattern="^(open|in_progress|completed|overdue)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high|critical)$")
    due_at: datetime | None = None


class MaintenanceUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(open|in_progress|completed|overdue)$")
    priority: str | None = Field(default=None, pattern="^(low|normal|high|critical)$")
    due_at: datetime | None = None


class ScheduleInput(BaseModel):
    title: str = Field(min_length=4, max_length=180)
    category: str = Field(min_length=2, max_length=60)
    status: str = Field(default="scheduled", pattern="^(scheduled|in_progress|complete|cancelled)$")
    starts_at: datetime
    ends_at: datetime | None = None

    @field_validator("starts_at")
    @classmethod
    def require_timezone(cls, value):
        if value.tzinfo is None:
            raise ValueError("starts_at must include a timezone")
        return value


class ScheduleUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=4, max_length=180)
    category: str | None = Field(default=None, min_length=2, max_length=60)
    status: str | None = Field(default=None, pattern="^(scheduled|in_progress|complete|cancelled)$")
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def update_times_are_aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Schedule times must include a timezone")
        return value

    @field_validator("ends_at")
    @classmethod
    def valid_order(cls, value, info):
        start = info.data.get("starts_at")
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("ends_at must include a timezone")
            if start is not None and value <= start:
                raise ValueError("ends_at must be after starts_at")
        return value


class ReportCreate(BaseModel):
    report_type: str = Field(min_length=2, max_length=60)
    filters: dict = Field(default_factory=dict)


class AnalyzeInput(BaseModel):
    # Retained for older clients, but the backend now builds its own trusted
    # dashboard snapshot and never uses client-supplied metrics for analysis.
    context: dict = Field(default_factory=dict, max_length=40)
    question: str | None = Field(default=None, max_length=1000)


class MaintenanceDemoPrediction(BaseModel):
    aircraft_category: str = Field(min_length=2, max_length=60)
    propulsion_type: str = Field(min_length=2, max_length=40)
    aircraft_age_years: float = Field(ge=0, le=100, allow_inf_nan=False)
    total_flight_hours: float = Field(ge=0, le=200_000, allow_inf_nan=False)
    total_cycles: float = Field(ge=0, le=250_000, allow_inf_nan=False)
    days_since_last_maintenance: float = Field(ge=0, le=3_650, allow_inf_nan=False)
    maintenance_events_last_12_months: int = Field(ge=0, le=100, allow_inf_nan=False)
    unresolved_maintenance_items: int = Field(ge=0, le=100, allow_inf_nan=False)
    inspection_overdue_flag: bool

    @field_validator("aircraft_category", "propulsion_type")
    @classmethod
    def strip_prediction_categories(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value must not be blank")
        return value


class WeatherQuery(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
