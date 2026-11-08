"use strict";

const csrf = document.querySelector('meta[name="csrf-token"]').content;
const state = {emails: [], filter: "all", settingsLoaded: false, settingsDirty: false};
const $ = (id) => document.getElementById(id);
const text = (tag, value, className = "") => {
  const node = document.createElement(tag);
  node.textContent = value ?? "";
  if (className) node.className = className;
  return node;
};

async function api(path, data) {
  const options = {credentials: "same-origin", headers: {"Accept": "application/json"}};
  if (data !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = csrf;
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  if (response.status === 401) { window.location.assign("/login"); throw new Error("Please sign in again."); }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request could not be completed.");
  return result;
}

function notice(message, error = false) {
  const node = $("message");
  node.textContent = message;
  node.className = error ? "notice error" : "notice";
}

function date(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return parsed.toLocaleString("en-US", {timeZone: state.timezone || "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
}

function badge(value) {
  const allowed = ["urgent", "expected", "normal", "quarantine", "review", "preview", "submitted", "failed", "uncertain", "pending", "fulfilled", "expired", "cancelled", "connected", "configured", "disabled", "error"];
  const tone = allowed.includes(value) ? value : "normal";
  return text("span", String(value || "unknown").replaceAll("_", " "), `badge ${tone}`);
}

function openEmail(email) {
  $("detail-subject").textContent = email.subject || "(No subject)";
  $("detail-sender").textContent = `${email.sender} · ${email.account} · ${date(email.received_at)}`;
  const reasons = Array.isArray(email.reasons) ? email.reasons.join(" · ") : "";
  $("detail-reasons").textContent = `Urgency score ${email.urgency_score || 0}/100${reasons ? ` · ${reasons}` : ""}`;
  $("detail-text").textContent = email.text || "No plain-text body stored. Review the original message in your mailbox for attachments.";
  $("email-dialog").showModal();
}

function renderEmails() {
  const search = $("search").value.toLowerCase();
  const rows = state.emails.filter((email) => {
    const matches = state.filter === "all" || (state.filter === "review" ? ["quarantine", "review"].includes(email.status) : email.status === state.filter || (state.filter === "urgent" && email.is_urgent));
    return matches && `${email.sender} ${email.subject}`.toLowerCase().includes(search);
  });
  $("email-feed").replaceChildren();
  $("email-count").textContent = rows.length;
  $("mail-empty").classList.toggle("hidden", rows.length > 0);
  if (!rows.length && state.emails.length) {
    $("mail-empty").querySelector("h3").textContent = "No messages match this view";
    $("mail-empty").querySelector("p").textContent = "Try another filter or search phrase.";
  }
  rows.forEach((email) => {
    const row = document.createElement("tr");
    const sender = document.createElement("td");
    sender.append(text("strong", email.sender, "sender"), text("small", email.account, "account-name"));
    const subject = document.createElement("td");
    const button = text("button", email.subject || "(No subject)", "subject-button");
    button.addEventListener("click", () => openEmail(email));
    subject.append(button);
    const status = document.createElement("td"); status.append(badge(email.status));
    const action = document.createElement("td");
    if (["quarantine", "review"].includes(email.status)) {
      const restore = text("button", "Restore", "text-button");
      restore.addEventListener("click", () => perform(restore, async () => {
        await api(`/api/emails/${encodeURIComponent(email.id)}/restore`, {});
        notice("Message restored to the local mail desk. The original mailbox was unchanged.");
        await refresh();
      }));
      action.append(restore);
    }
    row.append(sender, subject, text("td", date(email.received_at), "date-cell"), status, action);
    $("email-feed").append(row);
  });
}

function renderExpectations(items) {
  const list = $("expectation-list"); list.replaceChildren();
  const pending = items.filter((item) => (item.status || "pending") === "pending");
  $("expectation-count").textContent = pending.length;
  if (!items.length) list.append(text("p", "No open watches. Add the next reply you’re waiting for.", "empty-note"));
  items.slice(0, 12).forEach((item) => {
    const card = text("article", "", "list-item");
    const top = text("div", "", "item-heading");
    top.append(text("strong", item.subject || item.subject_keyword), badge(item.status || "pending"));
    card.append(top, text("p", item.sender || item.sender_pattern, "muted"), text("small", `Until ${date(item.expires_at || item.expires)} · ${item.notify || item.notify_via || "sms"}`, "micro"));
    if ((item.status || "pending") === "pending") {
      const cancel = text("button", "Cancel watch", "text-button small");
      cancel.addEventListener("click", () => perform(cancel, async () => {
        await api(`/api/expectations/${encodeURIComponent(item.id)}/cancel`, {}); await refresh();
      }));
      card.append(cancel);
    }
    list.append(card);
  });
}

function renderNotifications(items) {
  const list = $("notification-list"); list.replaceChildren();
  if (!items.length) list.append(text("p", "No alerts prepared yet. Urgent and expected messages will appear here.", "empty-note"));
  items.slice(0, 10).forEach((item) => {
    const card = text("article", "", "list-item");
    const top = text("div", "", "item-heading");
    top.append(text("strong", item.channel === "voice" ? "Voice call" : "SMS"), badge(item.status));
    card.append(top, text("p", item.body, "alert-body"), text("small", date(item.created_at), "micro"));
    if (item.detail) card.append(text("p", item.detail, "micro"));
    list.append(card);
  });
}

function renderAccounts(items) {
  const list = $("account-list"); list.replaceChildren();
  if (!items.length) { list.append(text("p", "No mailbox connections have been checked. Configure an account and start the worker to verify it.", "empty-note")); return; }
  items.forEach((item) => {
    const card = text("article", "", "account-card");
    const top = text("div", "", "item-heading");
    top.append(text("strong", item.account || item.name || item.id), badge(item.status));
    card.append(top);
    if (item.detail) card.append(text("p", item.detail, "micro"));
    card.append(text("small", `Last check: ${date(item.updated_at || item.checked_at)}`, "micro"));
    list.append(card);
  });
}

function populateSettings(settings) {
  state.timezone = settings.timezone || "America/New_York";
  const preview = settings.dry_run !== false;
  $("mode").textContent = preview ? "● Preview mode" : "● Live delivery enabled";
  $("alert-mode").textContent = preview ? "No calls or texts sent" : "Provider submission";
  $("status-line").textContent = preview ? "Preview mode is active. Alert drafts stay here; no calls or texts are sent." : "Live delivery is enabled. Configured alert channels and quiet hours apply.";
  $("settings-zone").textContent = state.timezone;
  if (state.settingsDirty) return;
  const form = $("settings-form");
  for (const input of form.elements) {
    if (!input.name || !(input.name in settings)) continue;
    if (input.type === "checkbox") input.checked = Boolean(settings[input.name]);
    else {
      if (input.tagName === "SELECT" && !Array.from(input.options).some((option) => option.value === String(settings[input.name]))) {
        input.append(new Option(String(settings[input.name]), String(settings[input.name])));
      }
      input.value = settings[input.name];
    }
  }
  state.settingsLoaded = true;
}

async function refresh() {
  const [stats, emails, expectations, notifications, accounts, settings] = await Promise.all([
    api("/api/stats"), api("/api/emails"), api("/api/expectations"), api("/api/notifications"), api("/api/accounts"), api("/api/settings"),
  ]);
  state.emails = emails;
  populateSettings(settings);
  $("stat-total").textContent = stats.total_processed ?? stats.processed_today ?? 0;
  $("stat-urgent").textContent = stats.urgent_flagged ?? stats.urgent_today ?? 0;
  $("stat-pending").textContent = stats.expected_pending ?? expectations.filter((item) => item.status === "pending").length;
  $("stat-review").textContent = stats.spam_blocked ?? stats.spam_today ?? 0;
  renderEmails(); renderExpectations(expectations); renderNotifications(notifications); renderAccounts(accounts);
  $("last-updated").textContent = `Updated ${new Date().toLocaleTimeString("en-US", {hour: "numeric", minute: "2-digit"})}`;
}

async function perform(button, action) {
  button.disabled = true;
  try { await action(); } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
}

$("today").textContent = new Date().toLocaleDateString("en-US", {timeZone: "America/New_York", weekday: "long", month: "long", day: "numeric", year: "numeric"}).toUpperCase();
$("refresh").addEventListener("click", () => perform($("refresh"), refresh));
$("search").addEventListener("input", renderEmails);
document.querySelectorAll("[data-filter]").forEach((tab) => tab.addEventListener("click", () => {
  state.filter = tab.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach((item) => { item.classList.toggle("selected", item === tab); item.setAttribute("aria-pressed", String(item === tab)); });
  renderEmails();
}));
$("close-dialog").addEventListener("click", () => $("email-dialog").close());
$("cleanup").addEventListener("click", () => perform($("cleanup"), async () => {
  await api("/api/cleanup", {});
  notice("Cleanup review prepared. Use the Review tab to inspect or restore messages.");
  await refresh();
}));
if ($("load-demo")) $("load-demo").addEventListener("click", () => perform($("load-demo"), async () => {
  await api("/api/demo", {}); notice("Synthetic sample mail loaded. These messages are demonstrations."); await refresh();
}));
$("expectation-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  perform(form.querySelector('button[type="submit"]'), async () => {
    const values = Object.fromEntries(new FormData(form)); values.hours = Number(values.hours);
    await api("/api/expectations", values); form.reset(); notice("Reply watch saved. Steph will check new arrivals against it."); await refresh();
  });
});
$("settings-form").addEventListener("input", () => { state.settingsDirty = true; $("settings-status").textContent = "Unsaved changes"; });
$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  perform(form.querySelector('button[type="submit"]'), async () => {
    if (!state.settingsLoaded) throw new Error("Wait for controls to load before saving.");
    const values = {};
    for (const input of form.elements) {
      if (!input.name) continue;
      values[input.name] = input.type === "checkbox" ? input.checked : ["retention_days", "urgency_threshold", "max_alerts_per_hour"].includes(input.name) ? Number(input.value) : input.value;
    }
    await api("/api/settings", values); state.settingsDirty = false; $("settings-status").textContent = "Controls saved";
    notice("Operating controls saved. They take effect on the next worker cycle."); await refresh();
  });
});
refresh().catch((error) => notice(error.message, true));
setInterval(() => { if (!document.hidden) refresh().catch((error) => notice(error.message, true)); }, 15000);
