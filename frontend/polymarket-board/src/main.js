/**
 * @polymarket/basketball-board — demo frontend for polymarket-basketball skill.
 * Aligns with skill defaults: within_hours=48.
 */
import { state } from "./state.js";
import { $, escapeHtml } from "./utils.js";
import { fetchMatches, fetchStatus, startSkill, stopSkill } from "./api.js";
import { render, renderMeta } from "./render.js";

function applySnap(snap) {
  state.matches = snap.matches || [];
  state.leagues = snap.leagues || [];
  state.proxy = snap.proxy || state.proxy;
  state.window = snap.window ?? null;
  state.stale = !!snap.stale;
  render(snap);
  if (snap.stale && snap.error) {
    console.warn("using stale snapshot:", snap.error);
  }
}

async function loadMatches({ refresh = false } = {}) {
  $("board").innerHTML = refresh
    ? `<div class="empty">Pulling Gamma via polymarket-basketball…</div>`
    : `<div class="empty">Loading skill buffer…</div>`;
  const snap = await fetchMatches({
    league: state.league,
    includeClosed: state.includeClosed,
    withinHours: state.withinHours,
    refresh,
  });
  applySnap(snap);
  return snap;
}

async function restartSkillIfRunning() {
  if (!state.running) return;
  await stopSkill();
  await startSkill({
    league: state.league,
    includeClosed: state.includeClosed,
    withinHours: state.withinHours,
  });
  state.running = true;
  renderMeta();
}

async function refreshFromStatus() {
  const st = await fetchStatus();
  state.running = !!st.running;
  if (st.league) state.league = st.league;
  if (typeof st.include_closed === "boolean") {
    state.includeClosed = st.include_closed;
    $("chkClosed").checked = state.includeClosed;
  }
  if (typeof st.within_hours === "number") {
    state.withinHours = st.within_hours;
    $("chkWindow").checked = state.withinHours > 0;
  }
  if (st.last_result?.matches) {
    applySnap({
      fetched_at: st.last_result.fetched_at,
      count: st.last_result.count,
      proxy: st.last_result.proxy || st.proxy,
      window: st.last_result.window || st.window,
      matches: st.last_result.matches,
      leagues: st.last_result.leagues || [],
    });
  } else {
    state.window = st.window || null;
    renderMeta(st.last_result);
  }
  if (st.last_error) {
    console.warn("skill error:", st.last_error);
  }
  state.lastTicks = st.ticks || 0;
  return st;
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      if (state.running) await refreshFromStatus();
    } catch (e) {
      console.error(e);
    }
  }, 2500);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function bind() {
  $("chkWindow").addEventListener("change", async (e) => {
    state.withinHours = e.target.checked ? 48 : 0;
    state.filterLeagueId = null;
    try {
      // Window filter is applied at fetch time — need a live pull.
      await loadMatches({ refresh: true });
      await restartSkillIfRunning();
    } catch (err) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(err.message)}</div>`;
    }
  });

  $("chkClosed").addEventListener("change", async (e) => {
    state.includeClosed = !!e.target.checked;
    state.filterLeagueId = null;
    try {
      await loadMatches({ refresh: true });
      await restartSkillIfRunning();
    } catch (err) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(err.message)}</div>`;
    }
  });

  $("btnRefresh").addEventListener("click", async () => {
    try {
      // Fast path: skill snapshot / in-memory buffer (no Gamma round-trip).
      await loadMatches({ refresh: false });
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStart").addEventListener("click", async () => {
    try {
      $("board").innerHTML = `<div class="empty">Starting polymarket-basketball (next ${state.withinHours || "∞"}h)…</div>`;
      await startSkill({
        league: state.league,
        includeClosed: state.includeClosed,
        withinHours: state.withinHours,
      });
      state.running = true;
      await refreshFromStatus();
      if (!state.matches.length) await loadMatches();
      startPolling();
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Start failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStop").addEventListener("click", async () => {
    try {
      await stopSkill();
      state.running = false;
      stopPolling();
      renderMeta();
    } catch (e) {
      alert(`Stop failed: ${e.message}`);
    }
  });
}

async function init() {
  bind();
  $("chkWindow").checked = state.withinHours > 0;
  try {
    const st = await fetchStatus();
    state.running = !!st.running;
    if (st.running) {
      await refreshFromStatus();
      startPolling();
    } else {
      await loadMatches();
    }
  } catch (e) {
    const msg = String(e.message || e);
    const proxyHint =
      /503|proxy|Tunnel|gamma-api/i.test(msg)
        ? "<br/><small>本地服务正常。这是 Shadowrocket → Gamma API 出站失败；请确认代理节点可用，或稍后 Refresh（skill 会尝试 Cloudflare IP 回退）。</small>"
        : "";
    $("board").innerHTML = `<div class="empty">拉取失败<br/>${escapeHtml(msg)}${proxyHint}</div>`;
  }
}

init();
