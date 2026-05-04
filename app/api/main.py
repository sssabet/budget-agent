"""FastAPI wrapper around the Budget Coach agent.

Why FastAPI on Cloud Run rather than ADK on Agent Runtime?
The plan's recommendation is to *not* spend a whole day fighting Agent Runtime
IAM if you have less than a week. Cloud Run + FastAPI is the boring deploy that
always works and gets you a demoable URL the same afternoon. Agent Runtime is
the better destination once we want managed sessions and platform observability;
deploy notes live in `docs/consulting_story.md`.

Endpoints
- GET /healthz   -> liveness, no DB or model dependency
- GET /me        -> authenticated user + households
- POST /chat     -> body: { prompt, session_id? } -> { reply, session_id, tools, latency_ms, policy_flags }

Sessions live in process memory (one InMemoryRunner per Cloud Run instance).
That's fine for a low-traffic first deployment. For heavier traffic or many
instances, swap in Agent Platform Sessions — that's the path documented in the
consulting one-pager.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.agent import build_agent
from app.agent.turn_log import run_turn_with_logging
from app.api.auth import (
    SESSION_COOKIE_NAME,
    AuthenticatedUser,
    CurrentUser,
    authorized_household,
    make_session_token,
    verify_firebase_id_token,
)
from app.config import settings
from app.db import models as dbm
from app.db.repository import (
    add_household_member_by_email,
    categories_by_id,
    create_category,
    create_transaction,
    delete_all_transactions,
    delete_category,
    delete_transaction,
    ensure_personal_household,
    get_household_by_id,
    get_household_by_name,
    get_or_create_user,
    get_user_by_email,
    list_budgets,
    list_categories,
    list_enabled_notification_subscriptions,
    list_household_members,
    list_transactions,
    list_transaction_rows,
    mark_subscription_reminded,
    update_category,
    update_household_name,
    update_transaction,
    upsert_budget,
    upsert_notification_subscription,
)
from app.db.init_db import DEFAULT_CATEGORIES
from app.tools.csv_import import import_csv as run_csv_import
from app.db.session import session_scope
from app.notifications import due_reminders, send_daily_reminder
from app.tools.budget_math import summarize_month


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("budget_agent.turn")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False


_setup_logging()
app = FastAPI(title="Budget Coach API", version="0.1.0")
WEB_DIR = Path(__file__).resolve().parents[1] / "web"

# In-process session registry: session_id -> {runner, user_id, adk_session_id, household_id}.
# Cleared on instance restart. Document this in the response so callers don't
# treat session_id as durable beyond a single instance's lifetime.
_SESSIONS: dict[str, dict[str, Any]] = {}


def _ensure_default_household_users() -> None:
    cfg = settings()
    default_members = [
        ("Saeed", cfg.seed_user_email, "owner"),
        ("Maryam", cfg.seed_partner_email, "member"),
    ]
    try:
        with session_scope() as s:
            household = get_household_by_name(s, cfg.default_household_name)
            if household is None:
                return
            for display_name, email, role in default_members:
                email = email.strip().lower()
                if not email:
                    continue
                user = get_user_by_email(s, email)
                if user is None:
                    user = s.scalar(
                        select(dbm.User)
                        .join(dbm.HouseholdUser, dbm.HouseholdUser.user_id == dbm.User.id)
                        .where(
                            dbm.HouseholdUser.household_id == household.id,
                            dbm.User.display_name == display_name,
                        )
                    )
                    if user is None:
                        user = dbm.User(email=email, display_name=display_name)
                        s.add(user)
                        s.flush()
                    else:
                        user.email = email
                        user.display_name = display_name

                membership = s.get(dbm.HouseholdUser, (household.id, user.id))
                if membership is None:
                    s.add(dbm.HouseholdUser(
                        household_id=household.id,
                        user_id=user.id,
                        role=role,
                    ))
                else:
                    membership.role = role
    except Exception:
        logging.getLogger(__name__).exception("failed to ensure default household users")


@app.on_event("startup")
def startup() -> None:
    _ensure_default_household_users()


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    household_name: str | None = None


class ToolCallSummary(BaseModel):
    name: str
    args: dict
    latency_ms: float | None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    tools: list[ToolCallSummary]
    latency_ms: float
    policy_flags: list[str]
    request_id: str


class HouseholdSummary(BaseModel):
    id: str
    name: str


class MeResponse(BaseModel):
    email: str
    display_name: str
    households: list[HouseholdSummary]


class FirebaseWebConfig(BaseModel):
    apiKey: str = ""
    authDomain: str = ""
    projectId: str = ""
    appId: str = ""
    messagingSenderId: str = ""


class AppConfigResponse(BaseModel):
    auth_mode: str
    firebase: FirebaseWebConfig
    vapid_public_key: str
    reminders_enabled: bool


class CategoryResponse(BaseModel):
    id: str
    name: str
    is_income: bool


class MemberResponse(BaseModel):
    id: str
    display_name: str
    email: str


class TransactionCreateRequest(BaseModel):
    product: str = Field(min_length=1, max_length=255)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    date: date
    category_id: str | None = None
    paid_by_user_id: str | None = None
    belongs_to_user_id: str | None = None
    description: str | None = Field(default=None, max_length=500)
    date_is_estimated: bool = False
    household_name: str | None = None


class TransactionResponse(BaseModel):
    id: str
    date: str | None
    date_is_estimated: bool
    product: str
    amount_NOK: str
    category_id: str | None
    category_name: str | None
    is_income: bool
    paid_by: str | None
    belongs_to: str | None
    description: str | None
    needs_review: bool


class BudgetVarianceResponse(BaseModel):
    category: str
    budgeted_NOK: str
    actual_NOK: str
    variance_NOK: str
    status: str


class DashboardResponse(BaseModel):
    month: str
    total_income_NOK: str
    total_expense_NOK: str
    net_NOK: str
    by_category_NOK: dict[str, str]
    over_budget: list[BudgetVarianceResponse]
    uncategorized_count: int
    estimated_date_count: int
    recent_transactions: list[TransactionResponse]


class HouseholdRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    household_name: str | None = None


class HouseholdMemberAddRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str | None = Field(default=None, max_length=120)
    household_name: str | None = None


class HouseholdMemberAddResponse(BaseModel):
    member: MemberResponse
    membership_created: bool


class CategoryCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    is_income: bool = False
    initial_budget: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    household_name: str | None = None


class CategoryUpdateRequest(BaseModel):
    """Partial update — omitted fields stay as-is."""
    name: str | None = Field(default=None, min_length=1, max_length=80)
    is_income: bool | None = None
    household_name: str | None = None


class CategoryDeleteResponse(BaseModel):
    deleted: bool
    transactions_uncategorized: int


_DateOnly = date  # alias so the `date` field name doesn't shadow the type


class TransactionUpdateRequest(BaseModel):
    """PATCH: only fields the caller sets are applied. Send `null` to clear
    optional fields (e.g. `category_id: null` makes the row uncategorized)."""
    product: str | None = Field(default=None, min_length=1, max_length=255)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    date: _DateOnly | None = None
    date_is_estimated: bool | None = None
    category_id: str | None = None
    paid_by_user_id: str | None = None
    belongs_to_user_id: str | None = None
    description: str | None = Field(default=None, max_length=500)
    household_name: str | None = None


class BudgetResponse(BaseModel):
    category_id: str
    month: str  # YYYY-MM
    amount_NOK: str


class BudgetUpsertRequest(BaseModel):
    category_id: str
    month: str  # YYYY-MM
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    household_name: str | None = None


class CsvImportRejectedRow(BaseModel):
    row: int
    reason: str
    raw: str


class CsvImportResponse(BaseModel):
    inserted: int
    rejected_count: int
    deleted: int
    created_categories: list[str]
    rejected_samples: list[CsvImportRejectedRow]


class PushSubscriptionRequest(BaseModel):
    subscription: dict[str, Any]
    timezone: str = Field(default="UTC", max_length=80)
    reminder_time: time = time(20, 0)
    enabled: bool = True
    household_name: str | None = None


class NotificationSubscriptionResponse(BaseModel):
    id: str
    enabled: bool
    timezone: str
    reminder_time: str


class ReminderJobResponse(BaseModel):
    checked: int
    sent: int
    failed: int


def _decimal_to_str(x: Decimal) -> str:
    return f"{x:.2f}"


def _parse_month(month: str) -> date:
    try:
        year, month_num = month.split("-")
        return date(int(year), int(month_num), 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM") from exc


def _uuid_or_none(value: str | None, field_name: str) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field_name} must be a UUID") from exc


def _transaction_response(t: dbm.Transaction) -> TransactionResponse:
    return TransactionResponse(
        id=str(t.id),
        date=t.date.isoformat() if t.date else None,
        date_is_estimated=t.date_is_estimated,
        product=t.product,
        amount_NOK=_decimal_to_str(t.amount),
        category_id=str(t.category_id) if t.category_id else None,
        category_name=t.category.name if t.category else None,
        is_income=t.category.is_income if t.category else False,
        paid_by=t.paid_by.display_name if t.paid_by else None,
        belongs_to=t.belongs_to.display_name if t.belongs_to else None,
        description=t.description,
        needs_review=t.needs_review,
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "model": settings().model}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ok", "model": settings().model}


@app.get("/me", response_model=MeResponse)
def me(user: AuthenticatedUser = CurrentUser) -> MeResponse:
    return MeResponse(
        email=user.email,
        display_name=user.display_name,
        households=[HouseholdSummary(id=str(hid), name=name) for hid, name in user.households],
    )


class SessionCreateRequest(BaseModel):
    id_token: str = Field(min_length=10)


class SessionCreateResponse(BaseModel):
    email: str
    display_name: str


@app.post("/session", response_model=SessionCreateResponse)
def create_session(req: SessionCreateRequest, response: Response) -> SessionCreateResponse:
    """Trade a Firebase/Google ID token for a long-lived HttpOnly session
    cookie. Browser auth flow: client signs in via Firebase popup → posts the
    fresh ID token here once → from then on the cookie carries auth on every
    request, surviving page reloads and ITP-style storage clears.
    """
    claims = verify_firebase_id_token(req.id_token)
    email = str(claims["email"]).strip().lower()
    raw_name = claims.get("name") or claims.get("given_name") or ""
    display_name = (str(raw_name).strip() or email.split("@", 1)[0])[:120]

    cfg = settings()
    with session_scope() as s:
        user, created = get_or_create_user(s, email=email, display_name=display_name)
        if created:
            logging.getLogger(__name__).info(
                "provisioned new user via /session email=%s", email
            )
        if not created and display_name and user.display_name != display_name:
            user.display_name = display_name
        # Make sure a household exists so the dashboard isn't empty.
        ensure_personal_household(s, user, default_categories=DEFAULT_CATEGORIES)
        cookie_value = make_session_token(user.id, user.email)
        resp_payload = SessionCreateResponse(
            email=user.email, display_name=user.display_name
        )

    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        max_age=cfg.session_ttl_seconds,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp_payload


@app.post("/session/logout")
def end_session(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/app-config", response_model=AppConfigResponse)
def app_config() -> AppConfigResponse:
    cfg = settings()
    return AppConfigResponse(
        auth_mode=cfg.auth_mode,
        firebase=FirebaseWebConfig(
            apiKey=cfg.firebase_api_key,
            authDomain=cfg.firebase_auth_domain,
            projectId=cfg.firebase_project_id,
            appId=cfg.firebase_app_id,
            messagingSenderId=cfg.firebase_messaging_sender_id,
        ),
        vapid_public_key=cfg.web_push_vapid_public_key,
        reminders_enabled=bool(cfg.web_push_vapid_public_key),
    )


@app.get("/categories", response_model=list[CategoryResponse])
def categories(
    household_name: str | None = None,
    user: AuthenticatedUser = CurrentUser,
) -> list[CategoryResponse]:
    household_id, _ = authorized_household(user, household_name)
    with session_scope() as s:
        return [CategoryResponse(**c.__dict__) for c in list_categories(s, household_id)]


@app.get("/members", response_model=list[MemberResponse])
def members(
    household_name: str | None = None,
    user: AuthenticatedUser = CurrentUser,
) -> list[MemberResponse]:
    household_id, _ = authorized_household(user, household_name)
    with session_scope() as s:
        return [
            MemberResponse(id=str(member.id), display_name=member.display_name, email=member.email)
            for member in list_household_members(s, household_id)
        ]


@app.get("/dashboard", response_model=DashboardResponse)
def dashboard(
    month: str | None = None,
    household_name: str | None = None,
    user: AuthenticatedUser = CurrentUser,
) -> DashboardResponse:
    target_month = _parse_month(month) if month else date.today().replace(day=1)
    household_id, _ = authorized_household(user, household_name)
    with session_scope() as s:
        txs = list_transactions(s, household_id)
        budgets = list_budgets(s, household_id)
        cats = categories_by_id(s, household_id)
        summary = summarize_month(txs, budgets, cats, target_month)
        recent = list_transaction_rows(s, household_id, month=target_month, limit=5)
        return DashboardResponse(
            month=summary.month.isoformat()[:7],
            total_income_NOK=_decimal_to_str(summary.total_income),
            total_expense_NOK=_decimal_to_str(summary.total_expense),
            net_NOK=_decimal_to_str(summary.net),
            by_category_NOK={k: _decimal_to_str(v) for k, v in summary.by_category.items()},
            over_budget=[
                BudgetVarianceResponse(
                    category=row.category_name,
                    budgeted_NOK=_decimal_to_str(row.budgeted),
                    actual_NOK=_decimal_to_str(row.actual),
                    variance_NOK=_decimal_to_str(row.variance),
                    status=row.status,
                )
                for row in summary.over_budget_categories
            ],
            uncategorized_count=summary.uncategorized_count,
            estimated_date_count=summary.estimated_date_count,
            recent_transactions=[_transaction_response(t) for t in recent],
        )


@app.get("/transactions", response_model=list[TransactionResponse])
def transactions(
    month: str | None = None,
    category_id: str | None = None,
    household_name: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=10000),
    user: AuthenticatedUser = CurrentUser,
) -> list[TransactionResponse]:
    target_month = _parse_month(month) if month else None
    household_id, _ = authorized_household(user, household_name)
    # `category_id=none` is the "uncategorized" filter — distinct from no filter at all.
    only_uncategorized = category_id == "none"
    cat_uuid = None if only_uncategorized else _uuid_or_none(category_id, "category_id")
    with session_scope() as s:
        rows = list_transaction_rows(
            s,
            household_id,
            month=target_month,
            category_id=cat_uuid,
            only_uncategorized=only_uncategorized,
            limit=limit,
            offset=offset,
        )
        return [_transaction_response(t) for t in rows]


@app.post("/transactions", response_model=TransactionResponse)
def add_transaction(
    req: TransactionCreateRequest,
    user: AuthenticatedUser = CurrentUser,
) -> TransactionResponse:
    household_id, _ = authorized_household(user, req.household_name)
    paid_by_user_id = _uuid_or_none(req.paid_by_user_id, "paid_by_user_id") or user.id
    try:
        with session_scope() as s:
            row = create_transaction(
                s,
                household_id,
                product=req.product,
                amount=req.amount,
                transaction_date=req.date,
                category_id=_uuid_or_none(req.category_id, "category_id"),
                paid_by_user_id=paid_by_user_id,
                belongs_to_user_id=_uuid_or_none(req.belongs_to_user_id, "belongs_to_user_id"),
                description=req.description,
                date_is_estimated=req.date_is_estimated,
            )
            return _transaction_response(row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/transactions/{tx_id}", response_model=TransactionResponse)
def edit_transaction(
    tx_id: str,
    req: TransactionUpdateRequest,
    user: AuthenticatedUser = CurrentUser,
) -> TransactionResponse:
    household_id, _ = authorized_household(user, req.household_name)
    tx_uuid = _uuid_or_none(tx_id, "transaction id")
    if tx_uuid is None:
        raise HTTPException(status_code=400, detail="invalid transaction id")

    raw = req.model_dump(exclude_unset=True)
    raw.pop("household_name", None)
    fields: dict = {}
    for plain_key in ("product", "amount", "date", "date_is_estimated", "description"):
        if plain_key in raw:
            fields[plain_key] = raw[plain_key]
    for uuid_key in ("category_id", "paid_by_user_id", "belongs_to_user_id"):
        if uuid_key in raw:
            fields[uuid_key] = _uuid_or_none(raw[uuid_key], uuid_key)

    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")

    try:
        with session_scope() as s:
            tx = update_transaction(s, household_id, tx_uuid, fields)
            return _transaction_response(tx)
    except ValueError as exc:
        # "not found" gets a 404; everything else 400.
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.delete("/transactions/{tx_id}")
def remove_transaction(
    tx_id: str,
    household_name: str | None = None,
    user: AuthenticatedUser = CurrentUser,
) -> dict:
    household_id, _ = authorized_household(user, household_name)
    tx_uuid = _uuid_or_none(tx_id, "transaction id")
    if tx_uuid is None:
        raise HTTPException(status_code=400, detail="invalid transaction id")
    with session_scope() as s:
        ok = delete_transaction(s, household_id, tx_uuid)
    if not ok:
        raise HTTPException(status_code=404, detail="transaction not found")
    return {"deleted": True}


@app.patch("/categories/{cat_id}", response_model=CategoryResponse)
def edit_category(
    cat_id: str,
    req: CategoryUpdateRequest,
    user: AuthenticatedUser = CurrentUser,
) -> CategoryResponse:
    household_id, _ = authorized_household(user, req.household_name)
    cat_uuid = _uuid_or_none(cat_id, "category id")
    if cat_uuid is None:
        raise HTTPException(status_code=400, detail="invalid category id")
    raw = req.model_dump(exclude_unset=True)
    raw.pop("household_name", None)
    if not raw:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        with session_scope() as s:
            cat = update_category(
                s,
                household_id,
                cat_uuid,
                name=raw.get("name"),
                is_income=raw.get("is_income"),
            )
            return CategoryResponse(id=str(cat.id), name=cat.name, is_income=cat.is_income)
    except ValueError as exc:
        status = 404 if "not found" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@app.delete("/categories/{cat_id}", response_model=CategoryDeleteResponse)
def remove_category(
    cat_id: str,
    household_name: str | None = None,
    user: AuthenticatedUser = CurrentUser,
) -> CategoryDeleteResponse:
    household_id, _ = authorized_household(user, household_name)
    cat_uuid = _uuid_or_none(cat_id, "category id")
    if cat_uuid is None:
        raise HTTPException(status_code=400, detail="invalid category id")
    try:
        with session_scope() as s:
            affected = delete_category(s, household_id, cat_uuid)
        return CategoryDeleteResponse(deleted=True, transactions_uncategorized=affected)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/household", response_model=HouseholdSummary)
def rename_household(
    req: HouseholdRenameRequest,
    user: AuthenticatedUser = CurrentUser,
) -> HouseholdSummary:
    household_id, _ = authorized_household(user, req.household_name)
    try:
        with session_scope() as s:
            household = update_household_name(s, household_id, name=req.name)
            return HouseholdSummary(id=str(household.id), name=household.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/household/members", response_model=HouseholdMemberAddResponse)
def add_household_member(
    req: HouseholdMemberAddRequest,
    user: AuthenticatedUser = CurrentUser,
) -> HouseholdMemberAddResponse:
    household_id, _ = authorized_household(user, req.household_name)
    if req.email.strip().lower() == user.email.lower():
        raise HTTPException(status_code=400, detail="you are already a member")
    try:
        with session_scope() as s:
            member, created = add_household_member_by_email(
                s,
                household_id,
                email=req.email,
                display_name=req.display_name,
            )
            return HouseholdMemberAddResponse(
                member=MemberResponse(
                    id=str(member.id),
                    display_name=member.display_name,
                    email=member.email,
                ),
                membership_created=created,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/categories", response_model=CategoryResponse)
def add_category(
    req: CategoryCreateRequest,
    user: AuthenticatedUser = CurrentUser,
) -> CategoryResponse:
    household_id, _ = authorized_household(user, req.household_name)
    try:
        with session_scope() as s:
            cat = create_category(
                s, household_id, name=req.name, is_income=req.is_income
            )
            if not req.is_income and req.initial_budget and req.initial_budget > 0:
                upsert_budget(
                    s,
                    household_id,
                    month=date.today().replace(day=1),
                    category_id=cat.id,
                    amount=req.initial_budget,
                )
            return CategoryResponse(id=str(cat.id), name=cat.name, is_income=cat.is_income)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/budgets", response_model=list[BudgetResponse])
def get_budgets(
    month: str | None = None,
    household_name: str | None = None,
    user: AuthenticatedUser = CurrentUser,
) -> list[BudgetResponse]:
    target_month = _parse_month(month) if month else date.today().replace(day=1)
    household_id, _ = authorized_household(user, household_name)
    with session_scope() as s:
        return [
            BudgetResponse(
                category_id=b.category_id,
                month=b.month.isoformat()[:7],
                amount_NOK=_decimal_to_str(b.amount),
            )
            for b in list_budgets(s, household_id, month=target_month)
        ]


@app.put("/budgets", response_model=BudgetResponse)
def set_budget(
    req: BudgetUpsertRequest,
    user: AuthenticatedUser = CurrentUser,
) -> BudgetResponse:
    target_month = _parse_month(req.month)
    household_id, _ = authorized_household(user, req.household_name)
    category_id = _uuid_or_none(req.category_id, "category_id")
    if category_id is None:
        raise HTTPException(status_code=400, detail="category_id is required")
    try:
        with session_scope() as s:
            b = upsert_budget(
                s,
                household_id,
                month=target_month,
                category_id=category_id,
                amount=req.amount,
            )
            return BudgetResponse(
                category_id=str(b.category_id),
                month=b.month.isoformat()[:7],
                amount_NOK=_decimal_to_str(b.amount),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/csv-import", response_model=CsvImportResponse)
async def csv_import(
    file: UploadFile = File(...),
    wipe_first: bool = Form(False),
    household_name: str | None = Form(None),
    user: AuthenticatedUser = CurrentUser,
) -> CsvImportResponse:
    """Import a CSV. Same column tolerance as the local importer:
    Product, amount, paid_by, category, belongs_to, description, date — extra
    columns ignored, unknown categories auto-created. `wipe_first=True`
    deletes existing transactions for the household before import (categories
    and budgets are preserved). Body is multipart/form-data.
    """
    household_id, _ = authorized_household(user, household_name)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="file is empty")
    text = raw.decode("utf-8", errors="replace")

    deleted = 0
    with session_scope() as s:
        if wipe_first:
            deleted = delete_all_transactions(s, household_id)
        result = run_csv_import(s, household_id, text)
        if result.rejected:
            row, reason, _ = result.rejected[0]
            raise HTTPException(
                status_code=400,
                detail=f"CSV import failed on row {row}: {reason}",
            )

    return CsvImportResponse(
        inserted=result.inserted,
        rejected_count=len(result.rejected),
        deleted=deleted,
        created_categories=sorted(set(result.created_categories)),
        rejected_samples=[
            CsvImportRejectedRow(row=i, reason=reason, raw=raw_row[:500])
            for i, reason, raw_row in result.rejected[:10]
        ],
    )


@app.post("/notification-subscriptions", response_model=NotificationSubscriptionResponse)
def save_notification_subscription(
    req: PushSubscriptionRequest,
    user: AuthenticatedUser = CurrentUser,
) -> NotificationSubscriptionResponse:
    household_id, _ = authorized_household(user, req.household_name)
    endpoint = str(req.subscription.get("endpoint", "")).strip()
    keys = req.subscription.get("keys") or {}
    p256dh = str(keys.get("p256dh", "")).strip()
    auth = str(keys.get("auth", "")).strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(status_code=400, detail="subscription endpoint and keys are required")

    with session_scope() as s:
        row = upsert_notification_subscription(
            s,
            user_id=user.id,
            household_id=household_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            timezone=req.timezone,
            reminder_time=req.reminder_time,
            enabled=req.enabled,
        )
        return NotificationSubscriptionResponse(
            id=str(row.id),
            enabled=row.enabled,
            timezone=row.timezone,
            reminder_time=row.reminder_time.isoformat(timespec="minutes"),
        )


@app.post("/jobs/send-daily-reminders", response_model=ReminderJobResponse)
def send_daily_reminders(
    x_cron_secret: str | None = Header(default=None),
) -> ReminderJobResponse:
    cfg = settings()
    if not cfg.reminder_cron_secret:
        raise HTTPException(status_code=503, detail="daily reminders are not configured")
    if x_cron_secret != cfg.reminder_cron_secret:
        raise HTTPException(status_code=401, detail="invalid cron secret")
    if not cfg.web_push_vapid_private_key:
        raise HTTPException(status_code=503, detail="web push private key is not configured")

    sent = 0
    failed = 0
    with session_scope() as s:
        subs = list_enabled_notification_subscriptions(s)
        due = due_reminders(subs, datetime.now(timezone.utc))
        for reminder in due:
            try:
                send_daily_reminder(
                    reminder.subscription,
                    vapid_private_key=cfg.web_push_vapid_private_key,
                    vapid_subject=cfg.web_push_vapid_subject,
                )
            except Exception:
                failed += 1
                continue
            sent += 1
            mark_subscription_reminded(
                s, reminder.subscription.id, reminder.local_now.date()
            )
    return ReminderJobResponse(checked=len(subs), sent=sent, failed=failed)


async def _ensure_session(
    session_id: str | None,
    user: AuthenticatedUser,
    household_name: str | None,
) -> dict[str, Any]:
    if session_id and session_id in _SESSIONS:
        entry = _SESSIONS[session_id]
        if entry["auth_user_id"] != user.id:
            raise HTTPException(status_code=403, detail="session belongs to another user")
        return entry

    from google.adk.runners import InMemoryRunner

    household_id, h_name = authorized_household(user, household_name)
    with session_scope() as s:
        h = get_household_by_id(s, household_id)
        if h is None:
            raise HTTPException(status_code=404, detail=f"household not found: {h_name}")
        agent = build_agent(h.id, h.name)

    runner = InMemoryRunner(agent=agent, app_name="budget-agent-api")
    user_id = str(user.id)
    adk_sess = await runner.session_service.create_session(
        app_name="budget-agent-api", user_id=user_id
    )

    new_id = session_id or uuid.uuid4().hex
    entry = {
        "runner": runner,
        "user_id": user_id,
        "adk_session_id": adk_sess.id,
        "session_id": new_id,
        "auth_user_id": user.id,
        "household_id": household_id,
    }
    _SESSIONS[new_id] = entry
    return entry


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: AuthenticatedUser = CurrentUser) -> ChatResponse:
    sess = await _ensure_session(req.session_id, user, req.household_name)
    cfg = settings()

    record = await run_turn_with_logging(
        runner=sess["runner"],
        user_id=sess["user_id"],
        session_id=sess["adk_session_id"],
        prompt=req.prompt,
        model=cfg.model,
    )

    return ChatResponse(
        reply=record.final_answer or "(no reply)",
        session_id=sess["session_id"],
        tools=[ToolCallSummary(**t) for t in record.tools],
        latency_ms=record.latency_ms,
        policy_flags=record.policy_flags,
        request_id=record.request_id,
    )


_NO_CACHE_HEADERS = {
    # Make the browser revalidate the shell on every load. We don't have
    # hashed asset names yet, so without this a deploy can take hours to
    # propagate to a logged-in user.
    "Cache-Control": "no-cache, must-revalidate",
}


if WEB_DIR.exists():
    # html=False keeps StaticFiles from auto-serving index.html under /static;
    # we want our own shell handlers (with no-cache) at /, /sw.js, etc.
    app.mount("/static", StaticFiles(directory=WEB_DIR, html=False), name="static")


@app.middleware("http")
async def no_cache_for_shell(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if (
        path == "/"
        or path == "/sw.js"
        or path == "/manifest.webmanifest"
        or path.startswith("/static/")
    ):
        for k, v in _NO_CACHE_HEADERS.items():
            response.headers[k] = v
    return response


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html", headers=_NO_CACHE_HEADERS)


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.webmanifest", headers=_NO_CACHE_HEADERS)


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(
        WEB_DIR / "sw.js",
        media_type="application/javascript",
        headers=_NO_CACHE_HEADERS,
    )


# Entrypoint used by the Cloud Run container.
def _serve() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    _serve()
