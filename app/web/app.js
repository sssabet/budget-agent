import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.5/firebase-app.js";
import {
  browserLocalPersistence,
  getAuth,
  getRedirectResult,
  GoogleAuthProvider,
  onAuthStateChanged,
  setPersistence,
  signInWithPopup,
  signInWithRedirect,
  signOut,
} from "https://www.gstatic.com/firebasejs/10.12.5/firebase-auth.js";

const state = {
  config: null,
  // Bearer token is now only set by the manual-token escape hatch. Real
  // sessions ride on a server-set HttpOnly cookie that JS can't read.
  token: "",
  me: null,
  categories: [],
  members: [],
  sessionId: localStorage.getItem("budget.chatSession") || null,
  txList: { offset: 0, pageSize: 50, hasMore: false, month: null, allMonths: false, categoryId: "" },
  txCache: new Map(),       // transaction id -> last-seen full row (for prefilling edit)
  editingTxId: null,
  editingCategoryId: null,
  returnTab: "dashboard",   // where to go back to after an edit screen
  addMode: "single",
  bulkInitialized: false,
  // Per-session defaults that flow into newly-added bulk rows so the user
  // doesn't re-pick date/category/payer/owner for every row.
  bulkDefaults: { date: null, category_id: "", paid_by_user_id: "", belongs_to_user_id: "" },
};

const BULK_INITIAL_ROWS = 8;
let bulkRowSeq = 0;

const $ = (id) => document.getElementById(id);
const today = new Date().toISOString().slice(0, 10);
const thisMonth = today.slice(0, 7);
const chatGreeting =
  "Hi, I’m your Budget Coach. I can help spot what changed, where money is leaking, and what to discuss next. Pick a question above if you want a quick start.";

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  setTimeout(() => toast.classList.add("hidden"), 2600);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("authorization", `Bearer ${state.token}`);
  // FormData / Blob bodies need to set their own multipart boundary —
  // don't override their content-type. Only json bodies get the default.
  const isFormBody = options.body instanceof FormData || options.body instanceof Blob;
  if (options.body && !isFormBody && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  // credentials: "include" so the session cookie is sent on every request.
  // Same-origin would also work for our deploy, but include is explicit and
  // future-proofs us if the API ever moves to a different subdomain.
  const res = await fetch(path, { ...options, headers, credentials: "include" });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    const err = new Error(detail.detail || `Request failed: ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

function money(value) {
  return `${value} NOK`;
}

function setSignedIn(isSignedIn) {
  $("auth-view").classList.toggle("hidden", isSignedIn);
  $("shell").classList.toggle("hidden", !isSignedIn);
}

function transactionRow(t) {
  // Cache the full row so the edit form can prefill instantly without a refetch.
  state.txCache.set(t.id, t);
  const category = t.category_name || "Uncategorized";
  const dateLabel = t.date
    ? (t.date_is_estimated ? `${t.date}*` : t.date)
    : "No date";
  const paidBy = t.paid_by ? `${escapeHtml(t.paid_by)} paid` : "Unknown payer";
  const belongs = t.belongs_to ? escapeHtml(t.belongs_to) : "Household";
  const note = t.description
    ? `<span class="tx-note">${escapeHtml(t.description)}</span>`
    : "";
  return `
    <div class="row tx-row" data-tx-id="${t.id}" role="button" tabindex="0">
      <div class="tx-main">
        <div class="tx-headline">
          <strong>${escapeHtml(t.product)}</strong>
          <span class="amount${t.is_income ? " income" : ""}">
            ${t.is_income ? "+" : ""}${money(t.amount_NOK)}
          </span>
        </div>
        <small class="tx-meta">
          <span>${dateLabel}</span>
          <span>·</span>
          <span>${escapeHtml(category)}</span>
        </small>
        <small class="tx-people">
          <span>${paidBy}</span>
          <span>·</span>
          <span>for ${belongs}</span>
        </small>
        ${note}
      </div>
    </div>
  `;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderDashboard(data) {
  $("spent").textContent = money(data.total_expense_NOK);
  $("income").textContent = money(data.total_income_NOK);
  $("net").textContent = money(data.net_NOK);

  const attention = [];
  if (data.uncategorized_count) attention.push(`${data.uncategorized_count} uncategorized`);
  if (data.estimated_date_count) attention.push(`${data.estimated_date_count} estimated dates`);
  if (data.over_budget.length) attention.push(`${data.over_budget.length} categories over budget`);
  $("attention").textContent = attention.length ? `Needs review: ${attention.join(", ")}` : "";
  $("attention").classList.toggle("hidden", !attention.length);

  const categories = Object.entries(data.by_category_NOK)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .map(([name, amount]) => `
      <div class="row">
        <div><strong>${escapeHtml(name)}</strong></div>
        <div class="amount">${money(amount)}</div>
      </div>
    `)
    .join("");
  $("category-list").innerHTML = categories || `<p class="muted">No spending yet for this month.</p>`;
  $("recent-list").innerHTML = data.recent_transactions.map(transactionRow).join("")
    || `<p class="muted">No transactions yet. Add the first one.</p>`;
}

async function loadAppData() {
  state.me = await api("/me");
  $("household-name").textContent = state.me.households[0]?.name || "Household";
  const [categories, members] = await Promise.all([api("/categories"), api("/members")]);
  state.categories = categories;
  state.members = members;
  renderFormOptions();
  await refreshDashboard();
}

function renderFormOptions() {
  $("category").innerHTML = `<option value="">Uncategorized</option>` + state.categories
    .map((c) => `<option value="${c.id}">${escapeHtml(c.name)}${c.is_income ? " (income)" : ""}</option>`)
    .join("");

  $("paid-by").innerHTML = state.members
    .map((m) => `<option value="${m.id}">${escapeHtml(m.display_name)}</option>`)
    .join("");

  $("belongs-to").innerHTML = `<option value="">Household</option>` + state.members
    .map((m) => `<option value="${m.id}">${escapeHtml(m.display_name)}</option>`)
    .join("");

  const current = state.members.find((m) => m.email === state.me?.email);
  if (current) $("paid-by").value = current.id;
}

async function refreshDashboard() {
  const month = $("month").value || thisMonth;
  const data = await api(`/dashboard?month=${encodeURIComponent(month)}`);
  renderDashboard(data);
}

function switchTab(tab) {
  const titles = {
    dashboard: "Today",
    add: "Add transaction",
    plan: "Plan",
    chat: "Assistant",
    settings: "Settings",
    transactions: "Transactions",
    "edit-tx": "Edit transaction",
    "edit-cat": "Edit category",
  };
  $("screen-title").textContent = titles[tab] || "Today";
  for (const name of Object.keys(titles)) {
    $(`${name}-view`).classList.toggle("hidden", name !== tab);
  }
  // Bottom tabs are dashboard / add / plan / chat / settings. Anything else
  // is a drill-in screen — keep the underlying tab highlighted.
  const isBottomTab = ["dashboard", "add", "plan", "chat", "settings"].includes(tab);
  document.querySelectorAll(".tabbar button").forEach((button) => {
    if (isBottomTab) {
      button.classList.toggle("active", button.dataset.tab === tab);
    }
  });
  if (tab === "plan") {
    loadPlan().catch((err) => showToast(err.message));
  }
  if (tab === "settings") {
    loadSettings().catch((err) => showToast(err.message));
  }
  if (tab === "transactions") {
    openTransactionsView().catch((err) => showToast(err.message));
  }
  if (tab === "chat") {
    ensureChatGreeting();
  }
  if (tab === "add") {
    // Refresh categories whenever the user lands on Add — fixes the
    // "I just made a category and don't see it here" report.
    refreshCategoriesAndMembers().catch((err) => showToast(err.message));
  }
}

function categoryOptionsHtml(selectedId = "") {
  return `<option value="">Uncategorized</option>` + (state.categories || [])
    .map(
      (c) =>
        `<option value="${c.id}"${c.id === selectedId ? " selected" : ""}>` +
        `${escapeHtml(c.name)}${c.is_income ? " (income)" : ""}</option>`,
    )
    .join("");
}

function memberOptionsHtml(selectedId = "", { allowEmpty = false, emptyLabel = "Household" } = {}) {
  const prefix = allowEmpty ? `<option value="">${escapeHtml(emptyLabel)}</option>` : "";
  return prefix + (state.members || [])
    .map(
      (m) =>
        `<option value="${m.id}"${m.id === selectedId ? " selected" : ""}>` +
        `${escapeHtml(m.display_name)}</option>`,
    )
    .join("");
}

function currentUserMemberId() {
  const me = (state.members || []).find((m) => m.email === state.me?.email);
  return me?.id || "";
}

function addBulkRow({ focus = false } = {}) {
  const tbody = $("bulk-rows");
  if (!tbody) return null;
  bulkRowSeq += 1;
  const row = document.createElement("tr");
  row.className = "bulk-row";
  row.dataset.rowId = String(bulkRowSeq);
  const paidByDefault = state.bulkDefaults.paid_by_user_id || currentUserMemberId();
  const dateDefault = state.bulkDefaults.date || today;
  row.innerHTML = `
    <td class="col-product"><input class="b-product" maxlength="255" autocomplete="off" placeholder="Rema, parking..." /></td>
    <td class="col-amount"><input class="b-amount" type="number" min="0.01" step="0.01" inputmode="decimal" placeholder="0.00" /></td>
    <td class="col-date"><input class="b-date" type="date" value="${escapeHtml(dateDefault)}" /></td>
    <td class="col-category"><select class="b-category">${categoryOptionsHtml(state.bulkDefaults.category_id)}</select></td>
    <td class="col-paid"><select class="b-paid">${memberOptionsHtml(paidByDefault)}</select></td>
    <td class="col-belongs"><select class="b-belongs">${memberOptionsHtml(state.bulkDefaults.belongs_to_user_id, { allowEmpty: true })}</select></td>
    <td class="col-note"><input class="b-note" maxlength="500" autocomplete="off" placeholder="Optional" /></td>
    <td class="col-remove"><button type="button" class="bulk-row-remove" aria-label="Remove row">×</button></td>
  `;
  tbody.appendChild(row);

  // Auto-grow: when the user starts typing in this row's product field,
  // append a fresh empty row so there's always one ready. Guarded so we
  // don't keep spawning if a successor is later removed.
  let spawnedSuccessor = false;
  const productInput = row.querySelector(".b-product");
  productInput.addEventListener("input", () => {
    if (spawnedSuccessor) return;
    if (row !== tbody.lastElementChild) return;
    if (!productInput.value) return;
    spawnedSuccessor = true;
    addBulkRow();
  });

  if (focus) productInput.focus();
  return row;
}

function refillBulkIfEmpty() {
  const tbody = $("bulk-rows");
  if (tbody && !tbody.children.length) {
    for (let i = 0; i < BULK_INITIAL_ROWS; i++) addBulkRow();
  }
}

function readBulkRow(row) {
  return {
    product: row.querySelector(".b-product").value.trim(),
    amount: row.querySelector(".b-amount").value.trim(),
    date: row.querySelector(".b-date").value || null,
    category_id: row.querySelector(".b-category").value || null,
    paid_by_user_id: row.querySelector(".b-paid").value || null,
    belongs_to_user_id: row.querySelector(".b-belongs").value || null,
    description: row.querySelector(".b-note").value.trim() || null,
  };
}

async function saveBulk() {
  const tbody = $("bulk-rows");
  const rows = Array.from(tbody.querySelectorAll(".bulk-row")).filter(
    (r) => !r.classList.contains("saved"),
  );
  rows.forEach((r) => r.classList.remove("error"));
  const work = [];
  for (const row of rows) {
    const data = readBulkRow(row);
    if (!data.product && !data.amount) continue;
    if (!data.product || !data.amount) {
      row.classList.add("error");
      continue;
    }
    work.push({ row, data });
  }
  if (!work.length) {
    $("bulk-status").textContent = "Fill product and amount in at least one row.";
    return;
  }

  // Carry the most-recent values forward to the next batch of rows.
  const last = work[work.length - 1].data;
  state.bulkDefaults = {
    date: last.date || today,
    category_id: last.category_id || "",
    paid_by_user_id: last.paid_by_user_id || "",
    belongs_to_user_id: last.belongs_to_user_id || "",
  };

  $("bulk-save").disabled = true;
  $("bulk-status").textContent = `Saving ${work.length}…`;
  const results = await Promise.allSettled(
    work.map(({ data }) =>
      api("/transactions", { method: "POST", body: JSON.stringify(data) }),
    ),
  );
  let ok = 0;
  const failures = [];
  results.forEach((res, i) => {
    const { row } = work[i];
    if (res.status === "fulfilled") {
      row.classList.add("saved");
      row.querySelectorAll("input, select, button").forEach((el) => (el.disabled = true));
      ok += 1;
    } else {
      row.classList.add("error");
      failures.push(res.reason?.message || "failed");
    }
  });

  $("bulk-save").disabled = false;
  if (failures.length) {
    $("bulk-status").textContent = `Saved ${ok}, ${failures.length} failed: ${failures[0]}`;
    showToast(`Saved ${ok} · ${failures.length} failed`);
  } else {
    $("bulk-status").textContent = `Saved ${ok} transaction${ok === 1 ? "" : "s"}.`;
    showToast(`Saved ${ok}`);
  }

  // Clear out saved rows after a brief pause so the user sees the success
  // styling, then top the table back up to the initial row count.
  setTimeout(() => {
    tbody.querySelectorAll(".bulk-row.saved").forEach((r) => r.remove());
    refillBulkIfEmpty();
  }, 600);

  await refreshDashboard().catch(() => {});
}

function initBulkIfNeeded() {
  if (state.bulkInitialized) return;
  state.bulkInitialized = true;
  for (let i = 0; i < BULK_INITIAL_ROWS; i++) addBulkRow();
}

function switchAddMode(mode) {
  state.addMode = mode;
  document.querySelectorAll(".add-mode-switch button").forEach((b) => {
    b.classList.toggle("active", b.dataset.mode === mode);
  });
  $("expense-form").classList.toggle("hidden", mode !== "single");
  $("bulk-add").classList.toggle("hidden", mode !== "bulk");
  if (mode === "bulk") initBulkIfNeeded();
}

async function refreshCategoriesAndMembers() {
  const [categories, members] = await Promise.all([api("/categories"), api("/members")]);
  state.categories = categories;
  state.members = members;
  renderFormOptions();
}

async function saveExpense(event) {
  event.preventDefault();
  const payload = {
    product: $("product").value,
    amount: $("amount").value,
    date: $("date").value,
    category_id: $("category").value || null,
    paid_by_user_id: $("paid-by").value || null,
    belongs_to_user_id: $("belongs-to").value || null,
    description: $("description").value || null,
  };
  await api("/transactions", { method: "POST", body: JSON.stringify(payload) });
  event.target.reset();
  $("date").value = today;
  renderFormOptions();
  showToast("Transaction saved");
  await refreshDashboard();
  switchTab("dashboard");
}

function openTransactionEdit(t, returnTab) {
  state.editingTxId = t.id;
  state.returnTab = returnTab || "dashboard";
  $("edit-tx-product").value = t.product || "";
  $("edit-tx-amount").value = t.amount_NOK || "";
  $("edit-tx-date").value = t.date || "";
  $("edit-tx-estimated").checked = Boolean(t.date_is_estimated);
  $("edit-tx-description").value = t.description || "";

  const cats = state.categories || [];
  $("edit-tx-category").innerHTML =
    `<option value="">Uncategorized</option>` +
    cats
      .map((c) => `<option value="${c.id}">${escapeHtml(c.name)}</option>`)
      .join("");
  $("edit-tx-category").value = t.category_id || "";

  const members = state.members || [];
  $("edit-tx-paid-by").innerHTML =
    `<option value="">Unknown</option>` +
    members.map((m) => `<option value="${m.id}">${escapeHtml(m.display_name)}</option>`).join("");
  $("edit-tx-belongs-to").innerHTML =
    `<option value="">Household</option>` +
    members.map((m) => `<option value="${m.id}">${escapeHtml(m.display_name)}</option>`).join("");

  // Match by display name since the row only has names, not user ids.
  const paidByMember = members.find((m) => m.display_name === t.paid_by);
  $("edit-tx-paid-by").value = paidByMember ? paidByMember.id : "";
  const belongsMember = members.find((m) => m.display_name === t.belongs_to);
  $("edit-tx-belongs-to").value = belongsMember ? belongsMember.id : "";

  switchTab("edit-tx");
}

async function saveTransactionEdit(event) {
  event.preventDefault();
  if (!state.editingTxId) return;
  const payload = {
    product: $("edit-tx-product").value,
    amount: $("edit-tx-amount").value,
    date: $("edit-tx-date").value || null,
    date_is_estimated: $("edit-tx-estimated").checked,
    category_id: $("edit-tx-category").value || null,
    paid_by_user_id: $("edit-tx-paid-by").value || null,
    belongs_to_user_id: $("edit-tx-belongs-to").value || null,
    description: $("edit-tx-description").value || null,
  };
  const updated = await api(`/transactions/${state.editingTxId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  state.txCache.set(updated.id, updated);
  showToast("Transaction saved");
  await postEditRefresh();
  goBackFromEdit();
}

async function deleteTransactionEdit() {
  if (!state.editingTxId) return;
  if (!confirm("Delete this transaction? This can't be undone.")) return;
  await api(`/transactions/${state.editingTxId}`, { method: "DELETE" });
  state.txCache.delete(state.editingTxId);
  showToast("Transaction deleted");
  await postEditRefresh();
  goBackFromEdit();
}

function openCategoryEdit(category, returnTab) {
  state.editingCategoryId = category.id;
  state.returnTab = returnTab || "plan";
  $("edit-cat-name").value = category.name || "";
  $("edit-cat-income").checked = Boolean(category.is_income);
  $("edit-cat-status").textContent = "";
  switchTab("edit-cat");
}

async function saveCategoryEdit(event) {
  event.preventDefault();
  if (!state.editingCategoryId) return;
  const name = $("edit-cat-name").value.trim();
  const isIncome = $("edit-cat-income").checked;
  if (!name) {
    showToast("Name is required.");
    return;
  }
  const updated = await api(`/categories/${state.editingCategoryId}`, {
    method: "PATCH",
    body: JSON.stringify({ name, is_income: isIncome }),
  });
  // Reflect locally.
  state.categories = (state.categories || []).map((c) =>
    c.id === updated.id ? updated : c,
  );
  showToast("Category saved");
  await postEditRefresh();
  goBackFromEdit();
}

async function deleteCategoryEdit() {
  if (!state.editingCategoryId) return;
  if (!confirm(
    "Delete this category? Transactions in it will become uncategorized.",
  )) return;
  const result = await api(`/categories/${state.editingCategoryId}`, { method: "DELETE" });
  state.categories = (state.categories || []).filter((c) => c.id !== state.editingCategoryId);
  const msg = result.transactions_uncategorized
    ? `Deleted. ${result.transactions_uncategorized} transactions are now uncategorized.`
    : "Category deleted.";
  $("edit-cat-status").textContent = msg;
  showToast("Category deleted");
  await postEditRefresh();
  goBackFromEdit();
}

async function postEditRefresh() {
  // After any edit, the dashboard numbers and any open list might be stale.
  // Refresh the things that are most likely to be looked at next.
  await Promise.allSettled([
    refreshDashboard(),
    state.returnTab === "transactions" ? reloadTransactionsView({ append: false }) : Promise.resolve(),
    state.returnTab === "plan" ? loadPlan() : Promise.resolve(),
  ]);
}

function goBackFromEdit() {
  state.editingTxId = null;
  state.editingCategoryId = null;
  switchTab(state.returnTab || "dashboard");
}

async function openTransactionsView() {
  const dashMonth = $("month").value || thisMonth;
  $("tx-month").value = dashMonth;
  $("tx-all-months").checked = false;
  state.txList.month = dashMonth;
  state.txList.allMonths = false;
  state.txList.offset = 0;
  state.txList.hasMore = false;
  state.txList.categoryId = "";
  renderTxCategoryFilter();
  $("tx-list").innerHTML = "";
  await reloadTransactionsView({ append: false });
}

function renderTxCategoryFilter() {
  const select = $("tx-category");
  if (!select) return;
  // "" = all, "none" = uncategorized, otherwise a category UUID. Keep names
  // sorted alphabetically (income last) so the dropdown stays scannable.
  const sorted = [...(state.categories || [])].sort((a, b) => {
    if (a.is_income !== b.is_income) return a.is_income ? 1 : -1;
    return a.name.localeCompare(b.name);
  });
  select.innerHTML =
    `<option value="">All categories</option>` +
    `<option value="none">Uncategorized</option>` +
    sorted
      .map(
        (c) =>
          `<option value="${c.id}">${escapeHtml(c.name)}${c.is_income ? " (income)" : ""}</option>`,
      )
      .join("");
  select.value = state.txList.categoryId || "";
}

async function reloadTransactionsView({ append }) {
  if (!append) state.txList.offset = 0;
  const params = new URLSearchParams();
  params.set("limit", String(state.txList.pageSize));
  params.set("offset", String(state.txList.offset));
  if (!state.txList.allMonths && state.txList.month) {
    params.set("month", state.txList.month);
  }
  if (state.txList.categoryId) {
    params.set("category_id", state.txList.categoryId);
  }
  $("tx-status").textContent = append ? "Loading more…" : "Loading…";
  let rows;
  try {
    rows = await api(`/transactions?${params.toString()}`);
  } catch (err) {
    $("tx-status").textContent = `Failed to load: ${err.message}`;
    throw err;
  }
  if (!append) $("tx-list").innerHTML = "";
  if (!rows.length && !append) {
    $("tx-status").textContent = state.txList.allMonths
      ? "No transactions yet."
      : "No transactions in this month.";
    $("tx-load-more").classList.add("hidden");
    return;
  }
  const html = rows.map(transactionRow).join("");
  $("tx-list").insertAdjacentHTML("beforeend", html);
  state.txList.offset += rows.length;
  state.txList.hasMore = rows.length >= state.txList.pageSize;
  $("tx-load-more").classList.toggle("hidden", !state.txList.hasMore);
  $("tx-status").textContent = `${state.txList.offset} loaded${state.txList.hasMore ? " — more available" : ""}`;
}

async function loadSettings() {
  // Pull the latest household + members so we don't show stale state after
  // a rename or a recent add. /me is light enough to call on every tab open.
  const [me, members] = await Promise.all([api("/me"), api("/members")]);
  state.me = me;
  state.members = members;
  const household = me.households[0];
  if (household) {
    $("household-name-input").value = household.name;
    $("household-name").textContent = household.name;
  }
  renderMemberList(members);
  $("member-status").textContent = "";
}

function renderMemberList(members) {
  const list = $("member-list");
  if (!members.length) {
    list.innerHTML = `<p class="muted">No members yet.</p>`;
    return;
  }
  list.innerHTML = members
    .map((member) => `
      <div class="row">
        <div class="label">
          <strong>${escapeHtml(member.display_name)}</strong>
          <small>${escapeHtml(member.email)}</small>
        </div>
        ${member.email === state.me?.email ? `<small class="muted">you</small>` : ""}
      </div>
    `)
    .join("");
}

async function saveHouseholdName() {
  const name = $("household-name-input").value.trim();
  if (!name) {
    showToast("Household name is required.");
    return;
  }
  if (name === state.me?.households?.[0]?.name) {
    showToast("Name unchanged.");
    return;
  }
  const updated = await api("/household", {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
  if (state.me?.households?.[0]) {
    state.me.households[0].name = updated.name;
  }
  $("household-name").textContent = updated.name;
  showToast("Household renamed");
}

async function addMember() {
  const email = $("new-member-email").value.trim();
  if (!email) {
    showToast("Email is required.");
    return;
  }
  const display_name = $("new-member-name").value.trim() || null;
  const result = await api("/household/members", {
    method: "POST",
    body: JSON.stringify({ email, display_name }),
  });
  $("new-member-email").value = "";
  $("new-member-name").value = "";
  $("member-status").textContent = result.membership_created
    ? `Added ${result.member.email}. They can sign in with Google to access this household.`
    : `${result.member.email} is already a member.`;
  showToast(result.membership_created ? "Member added" : "Already a member");
  await loadSettings();
}

function planMonthValue() {
  return $("plan-month").value || $("month").value || thisMonth;
}

async function loadPlan() {
  const month = planMonthValue();
  const [categories, budgets] = await Promise.all([
    api("/categories"),
    api(`/budgets?month=${encodeURIComponent(month)}`),
  ]);
  state.categories = categories; // keep the Add-expense form in sync
  renderFormOptions();
  const budgetByCat = new Map(budgets.map((b) => [b.category_id, b.amount_NOK]));
  renderPlanList(categories, budgetByCat);
  $("plan-status").textContent = "";
  $("plan-save").disabled = true;
}

function renderPlanList(categories, budgetByCat) {
  const list = $("plan-list");
  list.innerHTML = "";
  if (!categories.length) {
    list.innerHTML = `<p class="muted">No categories yet — add one below.</p>`;
    return;
  }
  // Expense first, alphabetical; income last, alphabetical.
  const sorted = [...categories].sort((a, b) => {
    if (a.is_income !== b.is_income) return a.is_income ? 1 : -1;
    return a.name.localeCompare(b.name);
  });
  for (const cat of sorted) {
    const row = document.createElement("div");
    row.className = `row editable${cat.is_income ? " income" : ""}`;
    row.dataset.catId = cat.id;
    row.dataset.original = budgetByCat.get(cat.id) || "0.00";
    const label = cat.is_income ? "Income" : "Expense";
    row.innerHTML = `
      <div class="label" role="button" tabindex="0">
        <strong>${escapeHtml(cat.name)}</strong>
        <small>${label} · tap to edit</small>
      </div>
      ${cat.is_income
        ? `<div class="amount muted">—</div>`
        : `<input type="number" min="0" step="100" inputmode="decimal" value="${row.dataset.original}" />`
      }
    `;
    if (!cat.is_income) {
      const input = row.querySelector("input");
      input.addEventListener("input", () => {
        row.classList.toggle("dirty", input.value !== row.dataset.original);
        $("plan-save").disabled = !planHasChanges();
      });
      // Don't trigger row-edit when interacting with the budget input.
      input.addEventListener("click", (e) => e.stopPropagation());
    }
    row.querySelector(".label").addEventListener("click", () => {
      openCategoryEdit(cat, "plan");
    });
    list.appendChild(row);
  }
}

function planHasChanges() {
  return Boolean($("plan-list").querySelector(".row.dirty"));
}

function planDirtyRows() {
  return Array.from($("plan-list").querySelectorAll(".row.dirty"));
}

async function savePlan() {
  const dirty = planDirtyRows();
  if (!dirty.length) return;
  const month = planMonthValue();
  $("plan-save").disabled = true;
  $("plan-status").textContent = "Saving…";
  let saved = 0;
  for (const row of dirty) {
    const input = row.querySelector("input");
    const amount = Number(input.value || 0);
    if (Number.isNaN(amount) || amount < 0) {
      showToast(`Invalid amount for ${row.querySelector("strong").textContent}`);
      continue;
    }
    await api("/budgets", {
      method: "PUT",
      body: JSON.stringify({
        category_id: row.dataset.catId,
        month,
        amount: amount.toFixed(2),
      }),
    });
    row.dataset.original = amount.toFixed(2);
    row.classList.remove("dirty");
    saved += 1;
  }
  $("plan-status").textContent = `${saved} budget${saved === 1 ? "" : "s"} saved.`;
  showToast("Budget saved");
  await refreshDashboard().catch(() => {});
}

async function addCategoryFromForm() {
  const name = $("new-cat-name").value.trim();
  if (!name) {
    showToast("Category name is required.");
    return;
  }
  const isIncome = $("new-cat-income").checked;
  const initialBudget = isIncome ? null : Number($("new-cat-budget").value || 0) || null;
  await api("/categories", {
    method: "POST",
    body: JSON.stringify({
      name,
      is_income: isIncome,
      initial_budget: initialBudget != null ? initialBudget.toFixed(2) : null,
    }),
  });
  $("new-cat-name").value = "";
  $("new-cat-income").checked = false;
  $("new-cat-budget").value = "";
  showToast(`Added ${name}`);
  await loadPlan();
}

async function importCsvFromForm() {
  const fileInput = $("csv-file");
  const file = fileInput.files?.[0];
  if (!file) {
    showToast("Pick a CSV file first.");
    return;
  }
  const wipeFirst = $("csv-wipe").checked;
  const form = new FormData();
  form.append("file", file);
  form.append("wipe_first", wipeFirst ? "true" : "false");
  $("csv-status").textContent = "Importing…";
  try {
    const result = await api("/csv-import", { method: "POST", body: form });
    const parts = [`Imported ${result.inserted} rows.`];
    if (result.deleted) parts.push(`Deleted ${result.deleted} previous.`);
    if (result.created_categories.length) {
      parts.push(`New categories: ${result.created_categories.join(", ")}.`);
    }
    if (result.rejected_count) parts.push(`Rejected ${result.rejected_count}.`);
    $("csv-status").textContent = parts.join(" ");
    fileInput.value = "";
    $("csv-wipe").checked = false;
    showToast(`Imported ${result.inserted} rows`);
    await Promise.all([loadPlan(), refreshDashboard()]);
  } catch (err) {
    $("csv-status").textContent = `Import failed: ${err.message}`;
    throw err;
  }
}

function appendChat(role, text) {
  const node = document.createElement("div");
  node.className = `bubble ${role}`;
  node.textContent = text;
  $("chat-log").appendChild(node);
  node.scrollIntoView({ block: "end" });
}

function ensureChatGreeting() {
  if ($("chat-log").children.length) return;
  appendChat("agent", chatGreeting);
}

async function sendChatPrompt(prompt) {
  const cleanPrompt = prompt.trim();
  if (!cleanPrompt) return;
  $("chat-prompt").value = "";
  appendChat("user", cleanPrompt);
  const response = await api("/chat", {
    method: "POST",
    body: JSON.stringify({ prompt: cleanPrompt, session_id: state.sessionId }),
  });
  state.sessionId = response.session_id;
  localStorage.setItem("budget.chatSession", response.session_id);
  appendChat("agent", response.reply);
}

async function sendChat(event) {
  event.preventDefault();
  await sendChatPrompt($("chat-prompt").value);
}

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replaceAll("-", "+").replaceAll("_", "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

async function enableReminders() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    $("notification-status").textContent = "This browser does not support Web Push.";
    return;
  }
  if (!state.config?.vapid_public_key) {
    $("notification-status").textContent = "Reminder keys are not configured on the server.";
    return;
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    $("notification-status").textContent = "Notifications were not allowed.";
    return;
  }
  const registration = await navigator.serviceWorker.ready;
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(state.config.vapid_public_key),
  });
  await api("/notification-subscriptions", {
    method: "POST",
    body: JSON.stringify({
      subscription: subscription.toJSON(),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      reminder_time: $("reminder-time").value || "20:00",
      enabled: true,
    }),
  });
  $("notification-status").textContent = "Daily reminder enabled.";
  showToast("Daily reminder enabled");
}

async function trySilentSession() {
  // Cookie-first: ask the server "do I have a valid session?" before
  // spinning up Firebase. If yes, we're done — no Firebase, no popup,
  // no flashing the sign-in screen. If no, fall through to the Firebase
  // sign-in flow.
  try {
    await loadAppData();
    setSignedIn(true);
    return true;
  } catch (err) {
    if (err && err.status !== 401 && err.status !== 403) {
      // Real error (DB down, etc.) — show it but still let the user try
      // to sign in.
      console.error("trySilentSession failed", err);
      showToast(err.message);
    }
    return false;
  }
}

async function initAuth() {
  const firebaseConfig = state.config.firebase || {};
  const canUseFirebase = Boolean(firebaseConfig.apiKey && firebaseConfig.projectId);

  // Try the cookie path first. If it succeeds we never even initialize
  // Firebase on this page load — fewer surfaces for ITP / private mode to
  // break, faster startup, and no flash of the sign-in screen.
  if (await trySilentSession()) {
    if (canUseFirebase) {
      // Still wire sign-out so it can revoke Firebase auth too.
      const app = initializeApp(firebaseConfig);
      const auth = getAuth(app);
      $("sign-out").addEventListener("click", () => signOutEverywhere(auth));
    } else {
      $("sign-out").addEventListener("click", () => signOutEverywhere(null));
    }
    return;
  }

  if (!canUseFirebase) {
    $("sign-in").classList.add("hidden");
    return;
  }

  const app = initializeApp(firebaseConfig);
  const auth = getAuth(app);
  // Belt-and-braces: keep Firebase's own persistence too, in case the
  // session cookie ever fails. Doesn't hurt; user just won't have to
  // re-popup Google during a single browser session.
  await setPersistence(auth, browserLocalPersistence).catch(() => {});
  try {
    const redirectResult = await getRedirectResult(auth);
    if (redirectResult?.user) {
      await exchangeIdTokenForCookie(redirectResult.user);
    }
  } catch (err) {
    console.error("getRedirectResult failed", err);
    showToast(err.message || "Google sign-in failed");
  }

  $("sign-in").addEventListener("click", async () => {
    const provider = new GoogleAuthProvider();
    provider.addScope("email");
    try {
      const result = await signInWithPopup(auth, provider);
      if (result?.user) await exchangeIdTokenForCookie(result.user);
    } catch (err) {
      const popupBlocked =
        err?.code === "auth/popup-blocked" ||
        err?.code === "auth/operation-not-supported-in-this-environment";
      if (popupBlocked) {
        await signInWithRedirect(auth, provider);
        return;
      }
      console.error("signInWithPopup failed", err);
      showToast(err?.message || "Google sign-in failed");
    }
  });
  $("sign-out").addEventListener("click", () => signOutEverywhere(auth));

  // Fallback observer: if Firebase resolves a user but somehow we missed
  // exchanging it for a cookie above (browser quirks), do it here.
  onAuthStateChanged(auth, async (user) => {
    if (!user) return;
    if (state.me) return; // already signed in via cookie
    try {
      await exchangeIdTokenForCookie(user);
    } catch (err) {
      console.error("session exchange failed", err);
      showToast(err.message || "Sign-in did not complete");
    }
  });
}

async function exchangeIdTokenForCookie(firebaseUser) {
  const idToken = await firebaseUser.getIdToken(/* forceRefresh */ true);
  await api("/session", {
    method: "POST",
    body: JSON.stringify({ id_token: idToken }),
  });
  await loadAppData();
  setSignedIn(true);
}

async function signOutEverywhere(auth) {
  // Always try to clear the server cookie, regardless of Firebase state.
  try { await api("/session/logout", { method: "POST" }); } catch { /* ignore */ }
  if (auth) {
    try { await signOut(auth); } catch { /* ignore */ }
  }
  state.me = null;
  state.token = "";
  localStorage.removeItem("budget.idToken");
  localStorage.removeItem("budget.chatSession");
  location.reload();
}

function bindUi() {
  $("month").value = thisMonth;
  $("plan-month").value = thisMonth;
  $("date").value = today;
  $("refresh").addEventListener("click", () => refreshDashboard().catch((err) => showToast(err.message)));
  $("month").addEventListener("change", () => refreshDashboard().catch((err) => showToast(err.message)));
  $("plan-month").addEventListener("change", () => loadPlan().catch((err) => showToast(err.message)));
  $("plan-save").addEventListener("click", () => savePlan().catch((err) => showToast(err.message)));
  $("add-cat").addEventListener("click", () => addCategoryFromForm().catch((err) => showToast(err.message)));
  $("csv-import").addEventListener("click", () => importCsvFromForm().catch((err) => showToast(err.message)));
  $("save-household-name").addEventListener("click", () => saveHouseholdName().catch((err) => showToast(err.message)));
  $("add-member").addEventListener("click", () => addMember().catch((err) => showToast(err.message)));
  $("show-all-tx").addEventListener("click", () => switchTab("transactions"));
  $("tx-back").addEventListener("click", () => switchTab("dashboard"));
  // Delegate row clicks for both lists — the rendered HTML is the same shape.
  for (const containerId of ["recent-list", "tx-list"]) {
    $(containerId).addEventListener("click", (event) => {
      const row = event.target.closest(".tx-row");
      if (!row || !row.dataset.txId) return;
      const cached = state.txCache.get(row.dataset.txId);
      if (!cached) {
        showToast("Transaction details unavailable — refresh and try again.");
        return;
      }
      const returnTab = containerId === "tx-list" ? "transactions" : "dashboard";
      openTransactionEdit(cached, returnTab);
    });
  }
  $("edit-tx-back").addEventListener("click", () => goBackFromEdit());
  $("edit-tx-form").addEventListener("submit", (event) =>
    saveTransactionEdit(event).catch((err) => showToast(err.message)),
  );
  $("edit-tx-delete").addEventListener("click", () =>
    deleteTransactionEdit().catch((err) => showToast(err.message)),
  );
  $("edit-cat-back").addEventListener("click", () => goBackFromEdit());
  $("edit-cat-form").addEventListener("submit", (event) =>
    saveCategoryEdit(event).catch((err) => showToast(err.message)),
  );
  $("edit-cat-delete").addEventListener("click", () =>
    deleteCategoryEdit().catch((err) => showToast(err.message)),
  );
  $("tx-load-more").addEventListener("click", () =>
    reloadTransactionsView({ append: true }).catch((err) => showToast(err.message)),
  );
  $("tx-month").addEventListener("change", () => {
    state.txList.month = $("tx-month").value || thisMonth;
    reloadTransactionsView({ append: false }).catch((err) => showToast(err.message));
  });
  $("tx-all-months").addEventListener("change", () => {
    state.txList.allMonths = $("tx-all-months").checked;
    reloadTransactionsView({ append: false }).catch((err) => showToast(err.message));
  });
  $("tx-category").addEventListener("change", () => {
    state.txList.categoryId = $("tx-category").value;
    reloadTransactionsView({ append: false }).catch((err) => showToast(err.message));
  });
  $("expense-form").addEventListener("submit", (event) => saveExpense(event).catch((err) => showToast(err.message)));
  document.querySelectorAll(".add-mode-switch button").forEach((button) => {
    button.addEventListener("click", () => switchAddMode(button.dataset.mode));
  });
  $("bulk-add-row").addEventListener("click", () => addBulkRow({ focus: true }));
  $("bulk-save").addEventListener("click", () => saveBulk().catch((err) => showToast(err.message)));
  $("bulk-rows").addEventListener("click", (event) => {
    const button = event.target.closest(".bulk-row-remove");
    if (!button) return;
    const row = button.closest(".bulk-row");
    if (!row) return;
    row.remove();
    refillBulkIfEmpty();
  });
  $("chat-form").addEventListener("submit", (event) => sendChat(event).catch((err) => showToast(err.message)));
  document.querySelector(".prompt-chips").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-prompt]");
    if (!button) return;
    sendChatPrompt(button.dataset.prompt).catch((err) => showToast(err.message));
  });
  $("enable-reminders").addEventListener("click", () => enableReminders().catch((err) => showToast(err.message)));
  $("use-token").addEventListener("click", async () => {
    state.token = $("manual-token").value.trim();
    localStorage.setItem("budget.idToken", state.token);
    setSignedIn(true);
    await loadAppData();
  });
  document.querySelectorAll(".tabbar button").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  if (location.hash === "#add") switchTab("add");
}

async function init() {
  bindUi();
  state.config = await fetch("/app-config").then((res) => res.json());
  if ("serviceWorker" in navigator) {
    await navigator.serviceWorker.register("/sw.js");
  }
  await initAuth();
}

init().catch((err) => {
  console.error(err);
  showToast(err.message || "App failed to start");
});
