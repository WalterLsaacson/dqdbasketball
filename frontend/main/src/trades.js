const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function shortTime(iso) {
  if (!iso) return "—";
  const s = String(iso);
  const t = s.includes("T") ? s.split("T") : [s, ""];
  const date = t[0].length >= 10 ? t[0].slice(5) : t[0];
  const clock = (t[1] || "").slice(0, 8);
  return `${date} ${clock}`.trim();
}

function fmtNum(v, digits = 4) {
  if (typeof v !== "number" || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

function fmtUsd(v) {
  if (typeof v !== "number" || Number.isNaN(v)) return "—";
  return `$${v.toFixed(2)}`;
}

function statusClass(row) {
  const st = String(row.status || "");
  if (row.delayed) return "st-delayed";
  if (st === "posted" || st === "filled" || st === "flatten_posted") return "st-ok";
  if (st === "dry_run" || st === "flatten_dry_run") return "st-dry";
  if (st === "skipped") return "st-skip";
  if (st === "error") return "st-err";
  if (st.includes("flatten")) return "st-flat";
  return "";
}

function queryParams() {
  const p = new URLSearchParams();
  const q = $("q").value.trim();
  const trade = $("trade").value;
  const status = $("status").value;
  const live = $("live").value;
  const hours = $("hours").value;
  const limit = $("limit").value;
  if (q) p.set("q", q);
  if (trade) p.set("trade", trade);
  if (status) p.set("status", status);
  if (live !== "") p.set("live", live);
  if (hours) p.set("last_hours", hours);
  if (limit) p.set("limit", limit);
  return p;
}

let selectedKey = null;
let rowsByKey = new Map();

function renderRows(payload) {
  const rows = payload.trades || [];
  rowsByKey = new Map(rows.map((r) => [String(r.idempotency_key || ""), r]));
  $("pillCount").textContent = `显示 ${payload.returned ?? rows.length} / 匹配 ${payload.total_matched ?? "—"}`;
  const opens = payload.opens || {};
  $("pillOpens").textContent = `Open ${opens.open ?? 0} · $${Number(opens.open_usdc || 0).toFixed(2)}`;
  $("pillFresh").textContent = payload.analyzed_at ? `刷新 ${payload.analyzed_at}` : "—";

  if (!rows.length) {
    $("tbody").innerHTML = `<tr><td colspan="9" class="empty">没有匹配的成交记录</td></tr>`;
    return;
  }

  $("tbody").innerHTML = rows
    .map((r) => {
      const key = esc(r.idempotency_key || "");
      const sel = r.idempotency_key && r.idempotency_key === selectedKey ? "is-selected" : "";
      const match = `${esc(r.home)} <span class="score">${esc(r.score)}</span> ${esc(r.away)}`;
      const mkt = esc(r.market_key || `${r.family || "?"}/${r.outcome || "?"}`);
      const edge =
        typeof r.net_edge === "number"
          ? `<span class="mono">${fmtNum(r.net_edge)}</span>`
          : "—";
      const note = esc(r.note || r.skip_reason || "");
      return `
        <tr class="row ${statusClass(r)} ${sel}" data-key="${key}">
          <td class="mono">${esc(shortTime(r.quoted_at))}</td>
          <td>${r.live ? '<span class="tag live">LIVE</span>' : '<span class="tag dry">dry</span>'}</td>
          <td>${esc(r.status)}</td>
          <td>${esc(r.trade)}</td>
          <td class="match">${match}</td>
          <td class="mkt mono">${mkt}</td>
          <td>${edge}</td>
          <td class="mono">${fmtUsd(r.usdc)}</td>
          <td class="note">${note}</td>
        </tr>`;
    })
    .join("");
}

async function showDetail(key) {
  selectedKey = key;
  const panel = $("detail");
  if (!key) {
    panel.innerHTML = `<p class="detail-hint">点一行查看完整原始 JSON（不改文件）。</p>`;
    return;
  }
  const compact = rowsByKey.get(key);
  panel.innerHTML = `<p class="detail-hint">Loading raw…</p>`;
  try {
    const r = await fetch(
      `/api/trades/detail?idempotency_key=${encodeURIComponent(key)}`,
      { cache: "no-store" },
    );
    const data = await r.json();
    if (!data.ok) {
      panel.innerHTML = `<p class="detail-hint">找不到原始行：${esc(data.error)}</p>`;
      return;
    }
    const c = data.compact || compact || {};
    const head = `
      <div class="detail-head">
        <div>
          <div class="detail-title">${esc(c.home)} ${esc(c.score)} ${esc(c.away)}</div>
          <div class="detail-sub mono">${esc(c.market_key)} · ${esc(c.status)} · ${esc(c.trade)}</div>
        </div>
        <button type="button" class="btn" id="btnCopy">Copy JSON</button>
      </div>`;
    const raw = JSON.stringify(data.raw, null, 2);
    panel.innerHTML = `${head}<pre class="raw" id="rawBlock">${esc(raw)}</pre>`;
    $("btnCopy")?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(raw);
        $("btnCopy").textContent = "Copied";
        setTimeout(() => {
          if ($("btnCopy")) $("btnCopy").textContent = "Copy JSON";
        }, 1200);
      } catch {
        $("btnCopy").textContent = "Copy failed";
      }
    });
  } catch (e) {
    panel.innerHTML = `<p class="detail-hint">加载失败：${esc(e.message)}</p>`;
  }
  // re-mark selected row without full re-fetch
  for (const tr of document.querySelectorAll("tr.row")) {
    tr.classList.toggle("is-selected", tr.getAttribute("data-key") === key);
  }
}

async function tick() {
  try {
    const r = await fetch(`/api/trades?${queryParams().toString()}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`status ${r.status}`);
    renderRows(await r.json());
    if (selectedKey) {
      for (const tr of document.querySelectorAll("tr.row")) {
        tr.classList.toggle("is-selected", tr.getAttribute("data-key") === selectedKey);
      }
    }
  } catch (e) {
    $("tbody").innerHTML = `<tr><td colspan="9" class="empty">加载失败：${esc(e.message)}</td></tr>`;
  }
}

$("tbody").addEventListener("click", (ev) => {
  const tr = ev.target.closest("tr.row");
  if (!tr) return;
  const key = tr.getAttribute("data-key");
  if (key) showDetail(key);
});

for (const id of ["trade", "status", "live", "hours", "limit"]) {
  $(id).addEventListener("change", () => tick());
}
let qTimer = null;
$("q").addEventListener("input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(() => tick(), 220);
});
$("btnRefresh").addEventListener("click", () => tick());

tick();
setInterval(tick, 60_000);
