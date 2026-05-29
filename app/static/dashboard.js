/* LLM Gateway dashboard — fetches /api/stats with the admin key and renders. */
const $ = (id) => document.getElementById(id);
let costChart, providerChart;

const fmtUsd = (n) => "$" + Number(n).toFixed(n < 1 ? 4 : 2);
const fmtNum = (n) => Number(n).toLocaleString();
// Escape model/provider strings — they originate from client-supplied request
// payloads and are rendered into the admin page, so they must never be raw HTML.
const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function card(label, value) {
  return `<div class="card"><div class="label">${label}</div><div class="value">${value}</div></div>`;
}

async function load() {
  const key = $("adminKey").value.trim();
  const days = $("days").value;
  if (!key) return;
  localStorage.setItem("gw_admin_key", key);

  let data;
  try {
    const res = await fetch(`/api/stats?days=${days}`, {
      headers: { Authorization: `Bearer ${key}` },
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    data = await res.json();
  } catch (e) {
    $("notice").innerHTML = `<span class="err">Failed to load: ${e.message}</span>`;
    $("notice").style.display = "block";
    $("content").style.display = "none";
    return;
  }

  $("notice").style.display = "none";
  $("content").style.display = "block";

  const t = data.totals;
  $("cards").innerHTML = [
    card("Total cost", fmtUsd(t.cost_usd)),
    card("Requests", fmtNum(t.requests)),
    card("Tokens", fmtNum(t.tokens)),
    card("Errors", `<span class="${t.errors ? "err" : "ok"}">${fmtNum(t.errors)}</span>`),
    card("Avg latency", `${fmtNum(Math.round(t.avg_latency_ms))} ms`),
    card("p95 latency", `${fmtNum(t.p95_latency_ms)} ms`),
  ].join("");

  renderCostChart(data.timeseries);
  renderProviderChart(data.by_provider);

  $("modelTable").querySelector("tbody").innerHTML = data.by_model
    .map(
      (m) =>
        `<tr><td>${esc(m.key)}</td><td>${fmtNum(m.requests)}</td><td>${fmtNum(m.tokens)}</td><td>${fmtUsd(m.cost_usd)}</td></tr>`
    )
    .join("");

  $("recentTable").querySelector("tbody").innerHTML = data.recent
    .map(
      (r) =>
        `<tr><td>${new Date(r.created_at).toLocaleString()}</td><td>${esc(r.provider)}</td><td>${esc(r.model)}</td><td>${fmtNum(r.tokens)}</td><td>${fmtUsd(r.cost_usd)}</td><td>${fmtNum(r.latency_ms)} ms</td><td class="${r.status === "ok" ? "ok" : "err"}">${esc(r.status)}</td></tr>`
    )
    .join("");
}

function renderCostChart(ts) {
  const ctx = $("costChart");
  costChart?.destroy();
  costChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: ts.map((p) => p.date),
      datasets: [
        {
          label: "Cost (USD)",
          data: ts.map((p) => p.cost_usd),
          borderColor: "#6c8cff",
          backgroundColor: "rgba(108,140,255,.15)",
          fill: true,
          tension: 0.3,
        },
      ],
    },
    options: { plugins: { legend: { display: false } }, scales: { x: { ticks: { color: "#8b93a7" } }, y: { ticks: { color: "#8b93a7" } } } },
  });
}

function renderProviderChart(rows) {
  const ctx = $("providerChart");
  providerChart?.destroy();
  providerChart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels: rows.map((r) => r.key),
      datasets: [
        {
          data: rows.map((r) => r.cost_usd),
          backgroundColor: ["#6c8cff", "#4ade80", "#f59e0b", "#ef4444", "#a78bfa"],
        },
      ],
    },
    options: { plugins: { legend: { labels: { color: "#e7e9ee" } } } },
  });
}

$("refresh").addEventListener("click", load);
$("days").addEventListener("change", () => $("adminKey").value && load());
const saved = localStorage.getItem("gw_admin_key");
if (saved) {
  $("adminKey").value = saved;
  load();
}
