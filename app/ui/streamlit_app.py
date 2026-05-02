"""Streamlit UI for the budget agent.

Pages:
- Overview: month picker, summary cards, budget table, by-category bar
- Transactions: table with edit-in-place, add-row, CSV import
- Chat: talk to the Budget Coach agent

Auth is stubbed: a sidebar dropdown picks which household member you are.
Real auth (Firebase Google sign-in) is a later task.

Run:
  streamlit run app/ui/streamlit_app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Streamlit only puts the script's directory on sys.path, not the project root.
# Add the repo root so `from app.* import ...` works.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import asyncio  # noqa: E402
import uuid  # noqa: E402
from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agent.agent import build_agent  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import models as m  # noqa: E402
from app.db.repository import (  # noqa: E402
    categories_by_id,
    get_household_by_name,
    list_budgets,
    list_categories,
    list_transactions,
)
from app.db.session import session_scope  # noqa: E402
from app.tools.budget_math import compare_budget_vs_actual, summarize_month  # noqa: E402
from app.tools.csv_import import import_csv  # noqa: E402

st.set_page_config(page_title="Budget Coach", page_icon=None, layout="wide")

st.markdown(
    """
    <style>
    .block-container { padding-top: 2rem; }
    div[data-testid="stMetric"] {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 1rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------- session bootstrap ----------

def _bootstrap():
    if "household_id" in st.session_state:
        return
    cfg = settings()
    with session_scope() as s:
        h = get_household_by_name(s, cfg.dev_household_name)
        if h is None:
            st.error(
                f"No household named '{cfg.dev_household_name}'. "
                "Run: `python -m app.db.init_db --reset --seed`"
            )
            st.stop()
        members = (
            s.query(m.User)
            .join(m.HouseholdUser, m.HouseholdUser.user_id == m.User.id)
            .filter(m.HouseholdUser.household_id == h.id)
            .order_by(m.User.display_name)
            .all()
        )
        st.session_state.household_id = h.id
        st.session_state.household_name = h.name
        st.session_state.members = [(str(u.id), u.display_name) for u in members]
        st.session_state.current_user_id = st.session_state.members[0][0]
        st.session_state.current_user_name = st.session_state.members[0][1]
    if "selected_month" not in st.session_state:
        st.session_state.selected_month = date(2026, 5, 1)
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []


def _sidebar():
    with st.sidebar:
        st.subheader("Household")
        st.write(f"**{st.session_state.household_name}**")

        names = [name for _, name in st.session_state.members]
        idx = names.index(st.session_state.current_user_name)
        picked = st.selectbox("I am…", names, index=idx)
        st.session_state.current_user_name = picked
        st.session_state.current_user_id = next(
            uid for uid, name in st.session_state.members if name == picked
        )

        st.divider()
        st.subheader("Month")
        m_str = st.session_state.selected_month.strftime("%Y-%m")
        new_str = st.text_input("YYYY-MM", value=m_str)
        try:
            y, mo = new_str.split("-")
            st.session_state.selected_month = date(int(y), int(mo), 1)
        except Exception:
            st.warning("Use YYYY-MM (e.g. 2026-05)")

        st.divider()
        st.caption("Auth is stubbed for local dev. Switch to Firebase later.")


# ---------- pages ----------

def page_overview():
    hid = st.session_state.household_id
    month = st.session_state.selected_month
    with session_scope() as s:
        txs = list_transactions(s, hid)
        budgets = list_budgets(s, hid)
        cats = categories_by_id(s, hid)
    sm = summarize_month(txs, budgets, cats, month)
    reports = compare_budget_vs_actual(txs, budgets, cats, month)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Income", f"{sm.total_income:,.0f} NOK")
    c2.metric("Expense", f"{sm.total_expense:,.0f} NOK")
    c3.metric("Net", f"{sm.net:,.0f} NOK", delta=None)
    c4.metric("Uncategorized", sm.uncategorized_count)

    if sm.estimated_date_count:
        st.info(
            f"{sm.estimated_date_count} transactions in this month have month-precision "
            "dates (day estimated as the 1st)."
        )

    st.subheader("Budget vs actual")
    if not reports:
        st.write("No budget or activity yet for this month.")
    else:
        df = pd.DataFrame(
            [
                {
                    "Category": r.category_name,
                    "Budgeted": float(r.budgeted),
                    "Actual": float(r.actual),
                    "Variance": float(r.variance),
                    "Status": r.status,
                }
                for r in reports
            ]
        )
        order = {"over": 0, "near": 1, "under": 2}
        df = df.sort_values(by=["Status"], key=lambda col: col.map(order))

        chart_df = df.set_index("Category")[["Budgeted", "Actual"]]
        left, right = st.columns([2, 1])
        with left:
            st.caption("Budgeted vs actual by category")
            st.bar_chart(chart_df)
        with right:
            spend_df = (
                df[df["Actual"] > 0][["Category", "Actual"]]
                .sort_values("Actual", ascending=False)
                .set_index("Category")
            )
            st.caption("Top spending categories")
            if spend_df.empty:
                st.write("No spending yet.")
            else:
                st.bar_chart(spend_df)

        st.dataframe(df, use_container_width=True, hide_index=True)


def page_planning():
    hid = st.session_state.household_id
    month = st.session_state.selected_month

    with session_scope() as s:
        cats_list = list_categories(s, hid)
        budgets = list_budgets(s, hid, month=month)

    budget_by_category_id = {b.category_id: b.amount for b in budgets}
    expense_cats = [c for c in cats_list if not c.is_income]

    st.subheader("Categories and monthly budget")
    st.caption(
        f"Manage envelopes for {month:%B %Y}. Income categories are kept out of "
        "budget variance charts. Investment is treated as an outflow, but not a cost."
    )

    left, right = st.columns([1, 2])

    with left:
        st.markdown("**Add category**")
        with st.form("add_category"):
            name = st.text_input("Category name")
            is_income = st.checkbox("Income category", value=False)
            initial_budget = st.number_input(
                "Initial monthly budget (NOK)",
                min_value=0.0,
                step=100.0,
                disabled=is_income,
            )
            if st.form_submit_button("Add category"):
                clean_name = name.strip()
                existing_names = {c.name.lower() for c in cats_list}
                if not clean_name:
                    st.error("Category name is required.")
                elif clean_name.lower() in existing_names:
                    st.error("That category already exists.")
                else:
                    with session_scope() as s:
                        cat = m.Category(
                            household_id=hid,
                            name=clean_name,
                            is_income=is_income,
                        )
                        s.add(cat)
                        s.flush()
                        if not is_income and initial_budget > 0:
                            s.add(
                                m.Budget(
                                    household_id=hid,
                                    month=month.replace(day=1),
                                    category_id=cat.id,
                                    amount=Decimal(str(initial_budget)),
                                )
                            )
                    st.success("Category added.")
                    st.rerun()

        st.markdown("**Set monthly budget**")
        if not expense_cats:
            st.write("Add an expense category first.")
        else:
            cat_names = [c.name for c in expense_cats]
            with st.form("set_budget"):
                cat_pick = st.selectbox("Category", cat_names)
                selected = next(c for c in expense_cats if c.name == cat_pick)
                current_amount = budget_by_category_id.get(selected.id, Decimal("0"))
                amount = st.number_input(
                    "Budget amount (NOK)",
                    min_value=0.0,
                    value=float(current_amount),
                    step=100.0,
                )
                if st.form_submit_button("Save budget"):
                    with session_scope() as s:
                        category_id = uuid.UUID(selected.id)
                        budget = s.scalar(
                            select(m.Budget).where(
                                m.Budget.household_id == hid,
                                m.Budget.month == month.replace(day=1),
                                m.Budget.category_id == category_id,
                            )
                        )
                        if budget is None:
                            s.add(
                                m.Budget(
                                    household_id=hid,
                                    month=month.replace(day=1),
                                    category_id=category_id,
                                    amount=Decimal(str(amount)),
                                )
                            )
                        else:
                            budget.amount = Decimal(str(amount))
                    st.success("Budget saved.")
                    st.rerun()

    with right:
        rows = [
            {
                "Category": c.name,
                "Type": "Income" if c.is_income else "Expense",
                "Monthly budget": float(budget_by_category_id.get(c.id, Decimal("0"))),
            }
            for c in cats_list
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def page_transactions():
    hid = st.session_state.household_id
    month = st.session_state.selected_month

    c1, c2 = st.columns([3, 1])
    with c2:
        show_all = st.toggle("Show all months", value=False, key="tx_show_all")
        show_null_dates = st.toggle("Include null-date rows", value=False, key="tx_show_null")

    with session_scope() as s:
        all_txs = list_transactions(s, hid)
        month_txs = list_transactions(s, hid, month=month)
    null_date_txs = [t for t in all_txs if t.date is None]

    if show_all:
        txs = all_txs if show_null_dates else [t for t in all_txs if t.date is not None]
        header = f"All transactions ({len(txs):,})"
    else:
        txs = list(month_txs)
        if show_null_dates:
            txs += null_date_txs
        header = f"Transactions in {month:%B %Y}"

    with c1:
        st.subheader(header)
        st.caption(
            f"Showing {len(txs):,} of {len(all_txs):,} total. "
            f"{len(null_date_txs)} have no date and are hidden by default."
        )

    if not txs:
        st.write("No transactions match the current filter.")
    else:
        df = pd.DataFrame(
            [
                {
                    "Date": t.date.isoformat() if t.date else "",
                    "Approx?": "yes" if t.date_is_estimated else "",
                    "Product": t.product,
                    "Amount (NOK)": float(t.amount),
                    "Category": t.category.name if t.category else "(uncategorized)",
                    "Paid by": t.paid_by or "",
                    "Belongs to": t.belongs_to or "Household",
                    "Needs review": "yes" if t.needs_review else "",
                }
                for t in txs
            ]
        )
        st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()
    with st.expander("Import CSV"):
        st.caption(
            "Columns: Product, amount, paid_by, category, belongs_to, description, date. "
            "Date can be YYYY-MM-DD, YYYY-MM, or empty. "
            "Unknown categories will be auto-created — you can rename or merge them later."
        )
        uploaded = st.file_uploader("CSV file", type=["csv"])
        wipe_first = st.checkbox(
            "Delete all existing transactions before importing",
            value=False,
            help="Use this when re-importing a corrected CSV. Categories and budgets are preserved.",
        )
        if uploaded is not None and st.button("Import"):
            text = uploaded.read().decode("utf-8")
            with session_scope() as s:
                if wipe_first:
                    deleted = (
                        s.query(m.Transaction)
                        .filter(m.Transaction.household_id == hid)
                        .delete(synchronize_session=False)
                    )
                    st.info(f"Deleted {deleted} existing transactions.")
                result = import_csv(s, hid, text)
            st.success(f"Imported {result.inserted} rows.")
            if result.created_categories:
                st.info(
                    f"Created {len(result.created_categories)} new categories: "
                    + ", ".join(sorted(set(result.created_categories)))
                )
            if result.rejected:
                st.warning(f"{len(result.rejected)} rows rejected:")
                st.json([{"row": i, "reason": r, "raw": raw} for i, r, raw in result.rejected])

    with st.expander("Add a transaction"):
        with session_scope() as s:
            cats_list = list_categories(s, hid)
        with st.form("add_tx"):
            d = st.date_input("Date (leave blank for unknown)", value=date.today())
            product = st.text_input("Product")
            amount = st.number_input("Amount (NOK)", min_value=0.0, step=10.0)
            cat_names = ["(uncategorized)"] + [c.name for c in cats_list]
            cat_pick = st.selectbox("Category", cat_names)
            paid_pick = st.selectbox("Paid by", [name for _, name in st.session_state.members])
            belongs_pick = st.selectbox(
                "Belongs to", ["Household"] + [name for _, name in st.session_state.members]
            )
            description = st.text_input("Description (optional)")
            if st.form_submit_button("Add"):
                if not product or amount <= 0:
                    st.error("Product and a positive amount are required.")
                else:
                    with session_scope() as s:
                        cat = next((c for c in list_categories(s, hid) if c.name == cat_pick), None)
                        member_map = dict(st.session_state.members)
                        # member_map is uid->name, invert for name->uid
                        name_to_uid = {name: uid for uid, name in st.session_state.members}
                        paid_uid = uuid.UUID(name_to_uid[paid_pick])
                        belongs_uid = (
                            None if belongs_pick == "Household" else uuid.UUID(name_to_uid[belongs_pick])
                        )
                        s.add(
                            m.Transaction(
                                household_id=hid,
                                date=d,
                                date_is_estimated=False,
                                product=product,
                                amount=Decimal(str(amount)),
                                paid_by_user_id=paid_uid,
                                belongs_to_user_id=belongs_uid,
                                category_id=uuid.UUID(cat.id) if cat else None,
                                description=description or None,
                                needs_review=cat is None,
                            )
                        )
                    st.success("Added.")
                    st.rerun()


def _agent_send(prompt: str) -> str:
    from google.adk.runners import InMemoryRunner
    from google.genai import types as genai_types

    async def _run() -> str:
        with session_scope() as s:
            h = s.get(m.Household, st.session_state.household_id)
            agent = build_agent(h.id, h.name)

        runner = InMemoryRunner(agent=agent, app_name="budget-agent")
        user_id = str(st.session_state.current_user_id)

        sess = await runner.session_service.create_session(app_name="budget-agent", user_id=user_id)

        msg = genai_types.Content(role="user", parts=[genai_types.Part(text=prompt)])
        out: list[str] = []
        async for event in runner.run_async(
            user_id=user_id,
            session_id=sess.id,
            new_message=msg,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        out.append(part.text)
        return "".join(out).strip() or "(no reply)"

    return asyncio.run(_run())


def page_chat():
    st.subheader("Talk to Budget Coach")
    for role, content in st.session_state.chat_messages:
        with st.chat_message(role):
            st.write(content)

    prompt = st.chat_input("Ask about the budget…")
    if prompt:
        st.session_state.chat_messages.append(("user", prompt))
        with st.chat_message("user"):
            st.write(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                try:
                    reply = _agent_send(prompt)
                except Exception as e:
                    reply = f"Agent error: {e}"
            st.write(reply)
        st.session_state.chat_messages.append(("assistant", reply))


# ---------- main ----------

_bootstrap()
_sidebar()

st.title("Budget Coach")
st.caption("Track household spending, compare it with monthly envelopes, and ask the coach for a calm summary.")

tab_overview, tab_plan, tab_tx, tab_chat = st.tabs(["Overview", "Planning", "Transactions", "Chat"])
with tab_overview:
    page_overview()
with tab_plan:
    page_planning()
with tab_tx:
    page_transactions()
with tab_chat:
    page_chat()
