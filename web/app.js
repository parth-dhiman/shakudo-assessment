"use strict";

// ========== Constants ==========

const POLL_MS = 5000;
const STATUSES = ["HEALTHY", "PROGRESSING", "DEGRADED", "FAILING", "UNKNOWN"];

// Waiting reasons that mean a container cannot start; shown in the failing colour.
const FAILING_REASONS = new Set([
  "CrashLoopBackOff",
  "ImagePullBackOff",
  "ErrImagePull",
  "CreateContainerConfigError",
  "InvalidImageName",
]);

// Request field -> input id in the New deployment form.
const CREATE_FIELDS = new Map([
  ["name", "new-name"],
  ["namespace", "new-namespace"],
  ["image", "new-image"],
  ["replicas", "new-replicas"],
]);

const $ = (id) => document.getElementById(id);

// ========== State ==========

const state = {
  route: { view: "dashboard", namespace: null },
  lastDashboardNamespace: null, // the filter the back link returns to
  namespaces: new Set(),
  lastSuccessAt: null,
  connectionError: null,
};

// Rows are keyed by UID and updated in place, so polling never replaces a focused link.
const dashboard = { namespace: undefined, hasData: false, rows: new Map() };

// signatures: last rendered data per table, so unchanged tables are not rebuilt.
const detail = { uid: null, data: null, scaleDirty: false, signatures: new Map() };

const deletion = { target: null, busy: false };

let activeModal = null;
let modalReturnFocus = null;

// ========== API client ==========

class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details || [];
  }
}

async function request(method, path, body, signal) {
  const options = { method, headers: { Accept: "application/json" }, signal };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }

  let response;
  let text = "";
  try {
    response = await fetch(path, options);
    if (response.status !== 204) text = await response.text();
  } catch (err) {
    // fetch rejects with a TypeError when the server cannot be reached; aborts pass through.
    if (err instanceof TypeError) throw new ApiError(0, "UNREACHABLE", "Cannot reach the API.");
    throw err;
  }
  if (response.status === 204) return null;

  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (_) {
    payload = null;
  }

  if (!response.ok) {
    const error = (payload && payload.error) || {};
    throw new ApiError(response.status, error.code || "HTTP_ERROR", error.message || `HTTP ${response.status}`, error.details);
  }
  if (payload === null) throw new ApiError(response.status, "BAD_RESPONSE", "Unexpected response from the API.");
  return payload;
}

function deploymentPath(uid) {
  return `/api/deployments/${encodeURIComponent(uid)}`;
}

const api = {
  // "All namespaces" omits the parameter: an empty ?namespace= is a 400.
  list: (namespace, signal) => request(
    "GET",
    namespace ? `/api/deployments?namespace=${encodeURIComponent(namespace)}` : "/api/deployments",
    undefined,
    signal,
  ),
  get: (uid, signal) => request("GET", deploymentPath(uid), undefined, signal),
  create: (body) => request("POST", "/api/deployments", body),
  patch: (uid, body) => request("PATCH", deploymentPath(uid), body),
  restart: (uid) => request("POST", `${deploymentPath(uid)}/restart`),
  remove: (uid) => request("DELETE", deploymentPath(uid)),
};

function isAbort(err) {
  return Boolean(err) && err.name === "AbortError";
}

function cleanMessage(message) {
  return String(message || "").replace(/^Value error, /, "");
}

function formatDetail(item) {
  const message = cleanMessage(item.message);
  return item.field && item.field !== "body" ? `${item.field}: ${message}` : message;
}

function detailLines(err) {
  return (err.details || []).map(formatDetail);
}

function errorSummary(err) {
  return [err.message, ...detailLines(err)].join(" ");
}

// ========== Formatting helpers ==========

function relativeTime(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "—";
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 3) return "just now";
  if (seconds < 60) return `${seconds} s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.floor(hours / 24)} d ago`;
}

function absoluteTime(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "";
  return date.toLocaleString();
}

function titleCase(word) {
  const text = String(word);
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

function plural(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

function compareText(a, b) {
  if (a < b) return -1;
  if (a > b) return 1;
  return 0;
}

// ========== DOM helpers ==========

// Builds an element. Children are nodes or strings; strings become text nodes, never HTML.
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else node.setAttribute(name, String(value));
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

// Writes only when the text changed, so polling doesn't disturb a selection in unchanged text.
function setText(node, value) {
  const text = String(value);
  if (node.textContent !== text) node.textContent = text;
}

// <time> elements carry their timestamp in data-ts; tick() keeps the relative text current.
function setTime(node, iso) {
  node.dataset.ts = iso || "";
  if (iso) node.setAttribute("datetime", iso);
  else node.removeAttribute("datetime");
  node.title = absoluteTime(iso);
  setText(node, relativeTime(iso));
}

function timeElement(iso) {
  const node = h("time");
  setTime(node, iso);
  return node;
}

function statusBadge() {
  return h(
    "span",
    { class: "status" },
    h("span", { class: "dot", "aria-hidden": "true" }),
    h("span", { class: "status-word" }),
  );
}

function setStatus(node, status) {
  const known = STATUSES.includes(status) ? status : "UNKNOWN";
  const className = `status status-${known.toLowerCase()}`;
  if (node.className !== className) node.className = className;
  setText(node.querySelector(".status-word"), status || "UNKNOWN");
}

// Rebuilds a table body only when its data changed since the last render.
function renderRowsIfChanged(key, tbody, items, buildRow) {
  const signature = JSON.stringify(items);
  if (detail.signatures.get(key) === signature) return;
  detail.signatures.set(key, signature);
  tbody.replaceChildren(...items.map(buildRow));
}

// ========== Router ==========

// #/ dashboard, #/?namespace=<ns> filtered dashboard, #/d/<uid> detail; anything else is the dashboard.
function parseRoute() {
  const hash = location.hash.replace(/^#/, "");
  const match = /^\/d\/([^/?]+)$/.exec(hash);
  if (match) {
    try {
      return { view: "detail", uid: decodeURIComponent(match[1]) };
    } catch (_) {
      // Malformed percent-encoding: fall back to the dashboard.
    }
  }
  const namespace = hash.startsWith("/?") ? new URLSearchParams(hash.slice(2)).get("namespace") : null;
  return { view: "dashboard", namespace: namespace || null };
}

function dashboardHash(namespace) {
  return namespace ? `#/?namespace=${encodeURIComponent(namespace)}` : "#/";
}

function detailHash(uid) {
  return `#/d/${encodeURIComponent(uid)}`;
}

function onRouteChange() {
  const previous = state.route;
  const route = parseRoute();
  state.route = route;
  closeModal();

  const isDetail = route.view === "detail";
  $("dashboard-view").hidden = isDetail;
  $("detail-view").hidden = !isDetail;
  $("ns-picker").hidden = isDetail;
  if (isDetail) enterDetail(route);
  else enterDashboard(route);

  if (previous.view !== route.view) window.scrollTo(0, 0);
  restartPolling();
}

// ========== Namespaces ==========

function addNamespaces(names) {
  for (const name of names) {
    if (!name || state.namespaces.has(name)) continue;
    state.namespaces.add(name);
    insertSorted($("ns-select"), new Option(name, name), 1); // after "All namespaces"
    insertSorted($("ns-options"), h("option", { value: name }), 0);
  }
}

// Options are only ever added, never rebuilt, so an open dropdown is left alone.
function insertSorted(parent, option, skip) {
  const next = Array.from(parent.children).slice(skip).find((node) => node.value > option.value);
  parent.insertBefore(option, next || null);
}

// The selector is built from unfiltered lists, so fetch one even when the first route is filtered.
async function loadNamespaces() {
  try {
    const data = await api.list(null);
    addNamespaces((data.items || []).map((item) => item.namespace));
  } catch (_) {
    // The current view's poll reports connection problems.
  }
}

// ========== Dashboard view ==========

function enterDashboard(route) {
  state.lastDashboardNamespace = route.namespace;
  if (route.namespace) addNamespaces([route.namespace]);
  $("ns-select").value = route.namespace || "";
  setText($("dash-title"), route.namespace ? `Deployments in ${route.namespace}` : "All deployments");
  // Rows already on screen for this filter stay until the next poll replaces them.
  if (dashboard.namespace !== route.namespace) resetDashboard(route.namespace);
}

function resetDashboard(namespace) {
  for (const entry of dashboard.rows.values()) entry.row.remove();
  dashboard.rows.clear();
  dashboard.namespace = namespace;
  dashboard.hasData = false;
  showDashboardMessage("Loading…");
}

function showDashboardMessage(text) {
  const message = $("dash-message");
  setText(message, text);
  message.hidden = false;
  $("dash-summary").hidden = true;
  $("dash-empty").hidden = true;
  $("dash-table").hidden = true;
}

function renderDashboard(items, namespace) {
  if (namespace === null) addNamespaces(items.map((item) => item.namespace));
  dashboard.hasData = true;

  const sorted = items.slice().sort((a, b) => compareText(a.namespace, b.namespace) || compareText(a.name, b.name));
  const empty = sorted.length === 0;
  $("dash-message").hidden = true;
  $("dash-empty").hidden = !empty;
  $("dash-table").hidden = empty;
  $("dash-summary").hidden = empty;
  setText($("dash-empty-text"), namespace ? `No deployments in ${namespace}` : "No deployments yet");
  setText($("dash-summary"), summaryText(sorted));
  syncDashboardRows(sorted);
}

// "4 deployments · 2 Healthy · 1 Progressing · 1 Failing"
function summaryText(items) {
  const counts = new Map();
  for (const item of items) counts.set(item.status, (counts.get(item.status) || 0) + 1);
  const order = [...STATUSES, ...[...counts.keys()].filter((status) => !STATUSES.includes(status))];
  const parts = [plural(items.length, "deployment", "deployments")];
  for (const status of order) {
    if (counts.get(status)) parts.push(`${counts.get(status)} ${titleCase(status)}`);
  }
  return parts.join(" · ");
}

function syncDashboardRows(items) {
  const tbody = $("dash-rows");
  const uids = new Set(items.map((item) => item.uid));
  for (const [uid, entry] of dashboard.rows) {
    if (!uids.has(uid)) {
      entry.row.remove();
      dashboard.rows.delete(uid);
    }
  }

  // Walk the sorted items alongside the existing rows; only new or out-of-place rows are inserted.
  let cursor = tbody.firstElementChild;
  for (const item of items) {
    let entry = dashboard.rows.get(item.uid);
    if (!entry) {
      entry = createDashboardRow(item.uid);
      dashboard.rows.set(item.uid, entry);
    }
    updateDashboardRow(entry.refs, item);
    if (entry.row === cursor) cursor = cursor.nextElementSibling;
    else tbody.insertBefore(entry.row, cursor);
  }
}

function createDashboardRow(uid) {
  const refs = {
    status: statusBadge(),
    name: h("a", { class: "row-link", href: detailHash(uid) }),
    namespace: h("span"),
    ready: h("span"),
    available: h("span"),
    age: h("time"),
    updated: h("time"),
  };
  const row = h(
    "tr",
    { "data-uid": uid },
    h("td", null, refs.status),
    h("td", null, refs.name),
    h("td", null, refs.namespace),
    h("td", { class: "num" }, refs.ready),
    h("td", { class: "num" }, refs.available),
    h("td", null, refs.age),
    h("td", null, refs.updated),
  );
  return { row, refs };
}

function updateDashboardRow(refs, item) {
  const replicas = item.replicas || {};
  setStatus(refs.status, item.status);
  setText(refs.name, item.name);
  setText(refs.namespace, item.namespace);
  setText(refs.ready, `${replicas.ready ?? 0}/${replicas.desired ?? 0}`);
  setText(refs.available, replicas.available ?? 0);
  setTime(refs.age, item.creationTimestamp);
  setTime(refs.updated, item.lastUpdated);
}

// The name cell is a real link (keyboard and middle-click); the rest of the row follows it on click.
function onDashboardRowClick(event) {
  if (event.target.closest("a")) return;
  const row = event.target.closest("tr[data-uid]");
  if (row) location.hash = detailHash(row.dataset.uid);
}

// ========== Detail view ==========

function enterDetail(route) {
  $("back-link").href = dashboardHash(state.lastDashboardNamespace);
  if (detail.uid !== route.uid || !detail.data) {
    detail.uid = route.uid;
    detail.data = null;
    detail.signatures.clear();
    showDetailMessage("Loading…");
  }
  // Inputs start fresh each time the view opens.
  detail.scaleDirty = false;
  $("scale-input").value = detail.data?.replicas?.desired ?? "";
  $("image-input").value = "";
}

function showDetailMessage(text) {
  $("detail-body").hidden = true;
  const message = $("detail-message");
  setText(message, text);
  message.hidden = false;
}

function renderDetail(deployment) {
  detail.data = deployment;
  addNamespaces([deployment.namespace]);
  $("detail-message").hidden = true;
  $("detail-body").hidden = false;

  setText($("d-namespace"), deployment.namespace);
  setText($("d-name"), deployment.name);
  setStatus($("d-status"), deployment.status);
  setTime($("d-created"), deployment.creationTimestamp);
  setTime($("d-updated"), deployment.lastUpdated);
  setText($("d-uid"), deployment.uid);
  $("d-uid").title = deployment.uid;

  const pods = deployment.pods || [];
  const replicas = deployment.replicas || {};
  renderImages(pods);
  renderCounts(replicas);
  syncScaleInput(replicas.desired);
  renderPods(pods);
  renderConditions(deployment.conditions || []);
}

function renderImages(pods) {
  const images = [...new Set(pods.flatMap((pod) => (pod.containers || []).map((c) => c.image)).filter(Boolean))].sort();
  $("d-images-line").hidden = images.length === 0;
  setText($("d-images-label"), images.length === 1 ? "Running image" : "Running images");
  setText($("d-images"), images.join(", "));
}

function renderCounts(replicas) {
  setText($("count-desired"), replicas.desired ?? 0);
  setText($("count-ready"), replicas.ready ?? 0);
  setText($("count-available"), replicas.available ?? 0);
  setText($("count-unavailable"), replicas.unavailable ?? 0);
}

// Never overwrite the replica input while the user is editing it.
function syncScaleInput(desired) {
  const input = $("scale-input");
  if (detail.scaleDirty || document.activeElement === input || desired === null || desired === undefined) return;
  if (input.value !== String(desired)) input.value = desired;
}

function renderPods(pods) {
  const sorted = pods.slice().sort((a, b) => compareText(a.name, b.name));
  $("pods-empty").hidden = sorted.length > 0;
  $("pods-table").hidden = sorted.length === 0;
  renderRowsIfChanged("pods", $("pods-rows"), sorted, podRow);
}

function podRow(pod) {
  const containers = pod.containers || [];
  const ready = containers.filter((container) => container.ready).length;
  const states = containers.map((container) => containerState(container, containers.length > 1));
  return h(
    "tr",
    null,
    h("td", { class: "mono" }, pod.name),
    h("td", null, pod.phase || "—"),
    h("td", { class: "num" }, `${ready}/${containers.length}`),
    h("td", { class: "num" }, pod.restartCount ?? 0),
    h("td", null, pod.node || "—"),
    h("td", { class: "cell-state" }, states.length ? states : "—"),
  );
}

// One line per container: Running, a waiting reason (with its message), or a terminated reason and exit code.
function containerState(container, showName) {
  const current = container.state || {};
  let reason = "—";
  let tone = null;
  let message = null;
  if (current.running) {
    reason = "Running";
  } else if (current.waiting) {
    reason = current.waiting.reason || "Waiting";
    tone = FAILING_REASONS.has(reason) ? "is-failing" : "is-muted";
    message = current.waiting.message || null;
  } else if (current.terminated) {
    const terminated = current.terminated;
    reason = `${terminated.reason || "Terminated"} (exit ${terminated.exitCode ?? "?"})`;
  }
  return h(
    "div",
    { class: "state-line" },
    showName ? h("span", { class: "mono" }, `${container.name} `) : null,
    h("span", { class: tone ? `state-reason ${tone}` : "state-reason" }, reason),
    message ? h("span", { class: "state-message", title: message }, message) : null,
  );
}

function renderConditions(conditions) {
  $("conditions-empty").hidden = conditions.length > 0;
  $("conditions-table").hidden = conditions.length === 0;
  renderRowsIfChanged("conditions", $("conditions-rows"), conditions, conditionRow);
}

function conditionRow(condition) {
  return h(
    "tr",
    null,
    h("td", null, condition.type || "—"),
    h("td", null, condition.status || "—"),
    h("td", null, condition.reason || "—"),
    h("td", { class: "cell-message" }, condition.message || "—"),
    h("td", null, timeElement(condition.lastUpdateTime)),
  );
}

// ========== Detail actions ==========

// Disables the button and swaps its label while the request is in flight.
async function runAction(button, busyLabel, action) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = busyLabel;
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// Renders the Deployment returned by PATCH or restart at once, then resumes the normal poll cycle.
function showActionResult(uid, deployment) {
  if (state.route.view !== "detail" || state.route.uid !== uid) return;
  cancelPoll();
  noteSuccess();
  renderDetail(deployment);
  schedulePoll();
}

async function onScaleSubmit(event) {
  event.preventDefault();
  const button = $("scale-button");
  if (button.disabled || !detail.data) return;
  const uid = detail.uid;
  const replicas = Number($("scale-input").value);
  try {
    const deployment = await runAction(button, "Applying…", () => api.patch(uid, { replicas }));
    toast(`Scaled ${deployment.name} to ${plural(replicas, "replica", "replicas")}`);
    if (detail.uid === uid) detail.scaleDirty = false;
    showActionResult(uid, deployment);
  } catch (err) {
    errorToast(err);
  }
}

async function onImageSubmit(event) {
  event.preventDefault();
  const button = $("image-button");
  const input = $("image-input");
  if (button.disabled || !detail.data) return;
  const uid = detail.uid;
  const image = input.value.trim();
  try {
    const deployment = await runAction(button, "Updating…", () => api.patch(uid, { image }));
    toast(`Image set to ${image} — rolling out`);
    if (detail.uid === uid && input.value.trim() === image) input.value = "";
    showActionResult(uid, deployment);
  } catch (err) {
    errorToast(err);
  }
}

async function onRestart() {
  const button = $("restart-button");
  if (button.disabled || !detail.data) return;
  const uid = detail.uid;
  try {
    const deployment = await runAction(button, "Restarting…", () => api.restart(uid));
    toast("Restart triggered — new pods are rolling out");
    showActionResult(uid, deployment);
  } catch (err) {
    errorToast(err);
  }
}

// ========== Modals ==========

function openModal(modal, firstField) {
  closeModal();
  modalReturnFocus = document.activeElement;
  activeModal = modal;
  modal.hidden = false;
  // Keep focus and the screen reader inside the dialog.
  $("site-header").inert = true;
  $("main").inert = true;
  firstField.focus();
}

function closeModal() {
  if (!activeModal) return;
  activeModal.hidden = true;
  activeModal = null;
  $("site-header").inert = false;
  $("main").inert = false;
  if (modalReturnFocus && modalReturnFocus.isConnected && typeof modalReturnFocus.focus === "function") {
    modalReturnFocus.focus();
  }
  modalReturnFocus = null;
}

// Closes on a click that both starts and ends on the overlay (not a text drag out of an input).
function bindOverlayDismiss(modal) {
  let pressedOverlay = false;
  modal.addEventListener("mousedown", (event) => {
    pressedOverlay = event.target === modal;
  });
  modal.addEventListener("click", (event) => {
    if (pressedOverlay && event.target === modal) closeModal();
  });
}

function showFormError(container, message, lines) {
  const children = [h("p", null, message)];
  if (lines && lines.length) children.push(h("ul", null, lines.map((line) => h("li", null, line))));
  container.replaceChildren(...children);
  container.hidden = false;
}

function hideFormError(container) {
  container.replaceChildren();
  container.hidden = true;
}

// ---------- New deployment ----------

function openNewModal() {
  clearCreateErrors();
  openModal($("new-modal"), $("new-name"));
}

function clearCreateErrors() {
  for (const inputId of CREATE_FIELDS.values()) {
    $(inputId).removeAttribute("aria-invalid");
    $(`${inputId}-error`).replaceChildren();
  }
  hideFormError($("new-error"));
}

// Empty optional fields are left out so the server applies its own defaults.
function buildCreateBody() {
  const body = {};
  for (const key of ["name", "namespace", "image"]) {
    const value = $(CREATE_FIELDS.get(key)).value.trim();
    if (value !== "") body[key] = value;
  }
  const replicas = $("new-replicas").value;
  if (replicas !== "") body.replicas = Number(replicas);
  return body;
}

async function onNewSubmit(event) {
  event.preventDefault();
  const button = $("new-submit");
  if (button.disabled) return;
  clearCreateErrors();
  const body = buildCreateBody();
  try {
    const created = await runAction(button, "Creating…", () => api.create(body));
    closeModal();
    $("new-form").reset();
    toast(`Created ${body.name}`);
    location.hash = detailHash(created.uid);
  } catch (err) {
    showCreateErrors(err);
  }
}

// 400 details go under the field they name; everything else goes above the buttons.
function showCreateErrors(err) {
  const details = err.details || [];
  const unmatched = [];
  let firstInvalid = null;
  for (const item of details) {
    const inputId = CREATE_FIELDS.get(item.field);
    if (!inputId) {
      unmatched.push(formatDetail(item));
      continue;
    }
    $(inputId).setAttribute("aria-invalid", "true");
    $(`${inputId}-error`).append(h("span", null, cleanMessage(item.message)));
    firstInvalid = firstInvalid || $(inputId);
  }
  if (details.length === 0 || unmatched.length > 0) showFormError($("new-error"), err.message, unmatched);
  if (firstInvalid) firstInvalid.focus();
}

// ---------- Delete ----------

function openDeleteModal() {
  if (!detail.data) return;
  const target = { uid: detail.data.uid, name: detail.data.name, namespace: detail.data.namespace };
  deletion.target = target;
  setText($("delete-title"), `Delete ${target.name}?`);
  setText($("delete-namespace"), target.namespace);
  setText($("delete-confirm-name"), target.name);
  $("delete-confirm").value = "";
  hideFormError($("delete-error"));
  updateDeleteButton();
  openModal($("delete-modal"), $("delete-confirm"));
}

function updateDeleteButton() {
  const target = deletion.target;
  $("delete-submit").disabled = deletion.busy || !target || $("delete-confirm").value !== target.name;
}

async function onDeleteSubmit(event) {
  event.preventDefault();
  const target = deletion.target;
  const button = $("delete-submit");
  if (deletion.busy || !target || $("delete-confirm").value !== target.name) return;

  deletion.busy = true;
  button.textContent = "Deleting…";
  updateDeleteButton();
  try {
    await api.remove(target.uid);
    closeModal();
    toast(`Deleted ${target.name}`);
    if (state.route.view === "detail" && state.route.uid === target.uid) {
      location.hash = dashboardHash(state.lastDashboardNamespace);
    }
  } catch (err) {
    showFormError($("delete-error"), err.message, detailLines(err));
  } finally {
    deletion.busy = false;
    button.textContent = "Delete";
    updateDeleteButton();
  }
}

// ========== Toasts ==========

function toast(message, kind = "success", lines = []) {
  const close = kind === "error"
    ? h("button", { type: "button", class: "toast-close", "aria-label": "Dismiss" }, "×")
    : null;
  const node = h(
    "div",
    { class: `toast toast-${kind}` },
    h(
      "div",
      { class: "toast-body" },
      h("p", null, message),
      lines.length ? h("ul", { class: "toast-details" }, lines.map((line) => h("li", null, line))) : null,
    ),
    close,
  );
  $("toasts").append(node);
  const timer = setTimeout(() => node.remove(), kind === "error" ? 8000 : 4000);
  if (close) {
    close.addEventListener("click", () => {
      clearTimeout(timer);
      node.remove();
    });
  }
}

function errorToast(err) {
  toast(err.message, "error", detailLines(err));
}

// ========== Polling ==========

// One timer for the current view. Each poll is scheduled only after the previous one finishes,
// and every cancel bumps the token so a late response for an old view is ignored.
let pollToken = 0;
let pollTimer = null;
let pollController = null;
let pollEnabled = false;

function cancelPoll() {
  pollToken += 1;
  clearTimeout(pollTimer);
  pollTimer = null;
  if (pollController) pollController.abort();
  pollController = null;
}

function restartPolling() {
  cancelPoll();
  pollEnabled = true;
  runPoll(pollToken);
}

function schedulePoll() {
  const token = pollToken;
  pollEnabled = true;
  pollTimer = setTimeout(() => runPoll(token), POLL_MS);
}

async function runPoll(token) {
  if (document.hidden) return; // resumed by onVisibilityChange
  const controller = new AbortController();
  pollController = controller;
  const route = state.route;
  let keepPolling = true;
  try {
    keepPolling = route.view === "detail"
      ? await pollDetail(route.uid, controller.signal, token)
      : await pollDashboard(route.namespace, controller.signal, token);
  } finally {
    if (token === pollToken) {
      pollController = null;
      if (keepPolling) schedulePoll();
      else pollEnabled = false;
    }
  }
}

// Each returns whether to keep polling.
async function pollDashboard(namespace, signal, token) {
  let data;
  try {
    data = await api.list(namespace, signal);
  } catch (err) {
    if (isAbort(err) || token !== pollToken) return false;
    if (err.status === 400) {
      // e.g. an invalid namespace typed into the URL: retrying won't help.
      setConnection(null);
      resetDashboard(namespace);
      showDashboardMessage(errorSummary(err));
      return false;
    }
    setConnection(err);
    if (!dashboard.hasData) showDashboardMessage(err.message);
    return true;
  }
  if (token !== pollToken) return false;
  noteSuccess();
  renderDashboard(data.items || [], namespace);
  return true;
}

async function pollDetail(uid, signal, token) {
  let deployment;
  try {
    deployment = await api.get(uid, signal);
  } catch (err) {
    if (isAbort(err) || token !== pollToken) return false;
    if (err.status === 404 || err.status === 400) {
      // Deleted elsewhere (404) or not a valid UID (400): stop polling this UID.
      setConnection(null);
      detail.data = null;
      showDetailMessage(err.status === 404 ? "This deployment no longer exists." : errorSummary(err));
      return false;
    }
    setConnection(err);
    if (!detail.data) showDetailMessage(err.message);
    return true;
  }
  if (token !== pollToken) return false;
  noteSuccess();
  renderDetail(deployment);
  return true;
}

function onVisibilityChange() {
  if (!pollEnabled) return;
  if (document.hidden) {
    cancelPoll();
  } else {
    restartPolling();
  }
}

// ========== Header indicator ==========

function noteSuccess() {
  state.lastSuccessAt = Date.now();
  setConnection(null);
}

function setConnection(err) {
  state.connectionError = err || null;
  renderIndicator();
}

function renderIndicator() {
  const indicator = $("updated");
  const err = state.connectionError;
  indicator.classList.toggle("is-lost", Boolean(err));
  if (err) {
    setText(indicator, "Connection lost — retrying");
    indicator.title = err.message;
  } else if (state.lastSuccessAt) {
    setText(indicator, `Updated ${relativeTime(state.lastSuccessAt)}`);
    indicator.title = absoluteTime(state.lastSuccessAt);
  } else {
    setText(indicator, "");
    indicator.title = "";
  }
}

// Every second: the indicator and every relative time on the page.
function tick() {
  renderIndicator();
  for (const node of document.querySelectorAll("time[data-ts]")) {
    setText(node, relativeTime(node.dataset.ts));
  }
}

// ========== Init ==========

function bindEvents() {
  $("new-button").addEventListener("click", openNewModal);
  $("empty-new-button").addEventListener("click", openNewModal);
  $("ns-select").addEventListener("change", (event) => {
    location.hash = dashboardHash(event.target.value || null);
  });
  $("dash-rows").addEventListener("click", onDashboardRowClick);

  $("scale-input").addEventListener("input", () => {
    detail.scaleDirty = true;
  });
  $("scale-form").addEventListener("submit", onScaleSubmit);
  $("image-form").addEventListener("submit", onImageSubmit);
  $("restart-button").addEventListener("click", onRestart);
  $("delete-button").addEventListener("click", openDeleteModal);

  $("new-form").addEventListener("submit", onNewSubmit);
  $("delete-confirm").addEventListener("input", updateDeleteButton);
  $("delete-form").addEventListener("submit", onDeleteSubmit);
  for (const modal of document.querySelectorAll(".modal")) bindOverlayDismiss(modal);
  for (const button of document.querySelectorAll("[data-close]")) button.addEventListener("click", closeModal);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && activeModal) {
      event.preventDefault();
      closeModal();
    }
  });

  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("hashchange", onRouteChange);
}

function init() {
  bindEvents();
  const route = parseRoute();
  // An unfiltered dashboard's first poll already lists every namespace.
  if (route.view !== "dashboard" || route.namespace) loadNamespaces();
  setInterval(tick, 1000);
  onRouteChange();
}

init();
