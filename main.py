import hmac
import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, JSON, Integer, String, create_engine, select, desc
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def normalize_database_url(value: str) -> str:
    # Render and some other hosts provide postgres:// or postgresql:// URLs.
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


DATABASE_URL = normalize_database_url(
    os.getenv("DATABASE_URL", "sqlite:///./medicare_requests.db")
)
engine_args: dict[str, Any] = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class PublicRequestRecord(Base):
    __tablename__ = "public_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    data: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="MediCare Public Request API",
    version="1.0.0",
    description=(
        "Stores limited public interest/support requests. It does not create patient accounts "
        "or handle clinical records."
    ),
)

origins_raw = os.getenv(
    "CORS_ORIGINS",
    "https://medicare-v1.neocities.org,http://localhost:5500,http://127.0.0.1:5500",
)
allowed_origins = [origin.strip() for origin in origins_raw.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Admin-Key"],
)


class PublicRequestIn(BaseModel):
    request_type: str = Field(min_length=1, max_length=60)
    data: dict[str, str]


RULES: dict[str, tuple[set[str], set[str]]] = {
    "Healthcare request": (
        {"name", "email", "location", "need"},
        {"name", "email", "location", "need", "message"},
    ),
    "Doctor registration": (
        {"name", "email", "specialty", "country", "availability"},
        {"name", "email", "specialty", "country", "availability"},
    ),
    "Volunteer registration": (
        {"name", "email", "help", "location"},
        {"name", "email", "help", "location"},
    ),
    "Partnership request": (
        {"org", "email", "country", "area"},
        {"org", "email", "country", "area"},
    ),
}
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def validate_submission(payload: PublicRequestIn) -> dict[str, str]:
    if payload.request_type not in RULES:
        raise HTTPException(status_code=400, detail="Unknown request type.")
    required, allowed = RULES[payload.request_type]
    values: dict[str, str] = {}
    for key, value in payload.data.items():
        if key not in allowed:
            continue
        if not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"Field '{key}' must be text.")
        cleaned = value.strip()
        max_len = 500 if key == "message" else 254 if key == "email" else 180
        if len(cleaned) > max_len:
            raise HTTPException(status_code=422, detail=f"Field '{key}' is too long.")
        values[key] = cleaned
    missing = sorted(key for key in required if not values.get(key))
    if missing:
        raise HTTPException(status_code=422, detail="Missing required fields: " + ", ".join(missing))
    if not EMAIL_RE.match(values["email"]):
        raise HTTPException(status_code=422, detail="Please provide a valid contact email.")
    if len(values.get("message", "")) > 500:
        raise HTTPException(status_code=422, detail="The note is too long.")
    return values


@app.get("/health")
def health_check():
    return {"ok": True, "service": "medicare-public-request-api"}


@app.post("/api/requests", status_code=201)
def create_request(payload: PublicRequestIn, db: Session = Depends(get_db)):
    clean_data = validate_submission(payload)
    record = PublicRequestRecord(request_type=payload.request_type, data=clean_data)
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "ok": True,
        "request_id": record.id,
        "message": "Request received. This is not a patient account.",
    }


@app.get("/api/admin/requests")
def list_requests(
    limit: int = Query(default=50, ge=1, le=200),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    db: Session = Depends(get_db),
):
    expected_key = os.getenv("ADMIN_API_KEY", "")
    if len(expected_key) < 32:
        raise HTTPException(
            status_code=503,
            detail="Admin access is disabled. Configure a long ADMIN_API_KEY on the server.",
        )
    if not x_admin_key or not hmac.compare_digest(x_admin_key, expected_key):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    rows = db.scalars(
        select(PublicRequestRecord).order_by(desc(PublicRequestRecord.created_at)).limit(limit)
    ).all()
    return {
        "ok": True,
        "items": [
            {
                "id": row.id,
                "request_type": row.request_type,
                "data": row.data,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }
