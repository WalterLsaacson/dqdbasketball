/**
 * @dongqiudi/bridge-board — frontend for match-bridge skill.
 *
 * System Main / pm_quote owns in-process match-bridge by default. This board
 * is a read-only viewer of data/bridge/* in that mode (like AF board).
 */
import { state } from "./state.js";
import { $, escapeHtml } from "./utils.js";
import { fetchEvents, fetchMatches, fetchStatus, runOnce, startBridge, stopBridge } from "./api.js";
import { consumeEvents, eventKey, render, renderMeta, tickWallClocks } from "./render.js";

function applySnap(snap) {
  state.matches = snap.matches || [];
  render(snap);
}

async function seedSeenEvents() {
  try {
    const data = await fetchEvents(80);
    for (const ev of data.events || []) {
      state.seenEventKeys.add(eventKey(ev));
    }
  } catch {
    /* ignore */
  }
}

function startClockTick() {
  stopClockTick();
  state.clockTimer = setInterval(tickWallClocks, 1000);
}

function stopClockTick() {
  if (state.clockTimer) {
    clearInterval(state.clockTimer);
    state.clockTimer = null;
  }
}

function shouldPoll() {
  return state.running || state.quoteOwned;
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      if (!shouldPoll()) return;
      const st = await fetchStatus();
      state.running = !!st.running;
      state.quoteOwned = !!st.inproc_owner || st.viewer_mode === "quote_owned";
      const snap = await fetchMatches();
      applySnap(snap);
      consumeEvents(st.last_result?.events || st.last_events || snap.events || []);
      renderMeta({
        ...(snap || {}),
        dqd_count: st.last_result?.dqd_count ?? snap.dqd_count,
        pm_count: st.last_result?.pm_count ?? snap.pm_count,
        finished_count: st.last_result?.finished_count ?? snap.finished_count,
      });
    } catch (e) {
      console.error(e);
    }
  }, 4000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function bind() {
  $("btnRefresh").addEventListener("click", async () => {
    try {
      $("board").innerHTML = `<div class="empty">Refreshing match-bridge…</div>`;
      const snap = await runOnce({ offline: false });
      applySnap(snap);
      const st = await fetchStatus();
      state.running = !!st.running;
      state.quoteOwned = !!st.inproc_owner || st.viewer_mode === "quote_owned";
      renderMeta({
        ...snap,
        dqd_count: snap.dqd_count,
        pm_count: snap.pm_count,
      });
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStart").addEventListener("click", async () => {
    try {
      await ensureBridgeRunning();
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Start failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStop").addEventListener("click", async () => {
    try {
      if (state.quoteOwned) {
        alert("Skill is owned by polymarket-quote; Stop is disabled on this board.");
        return;
      }
      await stopBridge();
      state.running = false;
      stopPolling();
      renderMeta(state.lastMeta);
    } catch (e) {
      alert(`Stop failed: ${e.message}`);
    }
  });
}

async function ensureBridgeRunning() {
  $("board").innerHTML = `<div class="empty">Starting match-bridge (DQD + Polymarket)…</div>`;
  const st = await startBridge();
  state.quoteOwned = st.viewer_mode === "quote_owned" || !!st.note?.includes("owned");
  state.running = !!st.running && !state.quoteOwned;
  if (st.last_result?.matches) applySnap(st.last_result);
  else applySnap(await fetchMatches());
  renderMeta({
    ...(st.last_result || state.lastMeta || {}),
    dqd_count: st.last_result?.dqd_count,
    pm_count: st.last_result?.pm_count,
  });
  startPolling();
  startClockTick();
  return st;
}

async function init() {
  bind();
  startClockTick();
  try {
    await seedSeenEvents();
    const st = await fetchStatus();
    state.running = !!st.running;
    state.quoteOwned = !!st.inproc_owner || st.viewer_mode === "quote_owned";
    // Always load file / memory snapshot (works for quote-owned + idle).
    applySnap(await fetchMatches());
    renderMeta({
      ...(state.lastMeta || {}),
      dqd_count: st.last_result?.dqd_count,
      pm_count: st.last_result?.pm_count,
      finished_count: st.last_result?.finished_count,
      matched_at: st.last_result?.matched_at,
    });
    if (shouldPoll()) {
      startPolling();
      return;
    }
    // Idle standalone: do not auto-start (align with AF board). User clicks Start.
    if (!state.matches.length) {
      $("board").innerHTML =
        `<div class="empty">Idle — click Start Bridge, or open via System Main (quote owns skill).</div>`;
    }
  } catch (e) {
    $("board").innerHTML = `<div class="empty">无法连接 bridge-board。<br/>${escapeHtml(e.message)}</div>`;
  }
}

init();
