/**
 * @dongqiudi/match-board — official Match Board frontend entry.
 * Talks to the local server which wraps the dongqiudi-basketball skill.
 */
import { state } from "./state.js";
import { $, escapeHtml } from "./utils.js";
import { fetchMatches, fetchStatus, startWatch, stopWatch } from "./api.js";
import { render, renderMeta, pushToast } from "./render.js";

async function loadMatches() {
  $("board").innerHTML = `<div class="empty">Loading ${state.tab} tab via skill…</div>`;
  const snap = await fetchMatches(state.tab);
  state.matches = snap.matches || [];
  state.leagues = snap.leagues || [];
  render(snap);
  return snap;
}

async function refreshFromWatchStatus() {
  const st = await fetchStatus();
  state.watching = !!st.running;
  if (st.running && st.tab && st.tab !== state.tab) {
    state.tab = st.tab;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === state.tab));
  }
  if (st.last_result?.matches) {
    state.matches = st.last_result.matches;
    for (const ev of st.last_result.events || []) pushToast(ev);
    render({
      fetched_at: st.last_result.fetched_at,
      count: st.last_result.count,
    });
  } else {
    renderMeta(st.last_result);
  }
  if (st.ticks !== state.lastTicks) {
    state.lastTicks = st.ticks || 0;
  }
  return st;
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    try {
      if (state.watching) await refreshFromWatchStatus();
    } catch (e) {
      console.error(e);
    }
  }, 2000);
}

function stopPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function bind() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", async () => {
      state.tab = btn.dataset.tab;
      state.filterLeagueId = null;
      document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("is-active", b === btn));
      try {
        await loadMatches();
        if (state.watching) {
          await stopWatch();
          await startWatch(state.tab);
          state.watching = true;
          renderMeta();
        }
      } catch (e) {
        $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(e.message)}</div>`;
      }
    });
  });

  $("btnRefresh").addEventListener("click", async () => {
    try {
      await loadMatches();
    } catch (e) {
      $("board").innerHTML = `<div class="empty">Failed: ${escapeHtml(e.message)}</div>`;
    }
  });

  $("btnStart").addEventListener("click", async () => {
    try {
      await startWatch(state.tab);
      state.watching = true;
      await refreshFromWatchStatus();
      if (!state.matches.length) await loadMatches();
      startPolling();
    } catch (e) {
      alert(`Start failed: ${e.message}`);
    }
  });

  $("btnStop").addEventListener("click", async () => {
    try {
      await stopWatch();
      state.watching = false;
      stopPolling();
      renderMeta();
    } catch (e) {
      alert(`Stop failed: ${e.message}`);
    }
  });
}

async function init() {
  bind();
  try {
    const st = await fetchStatus();
    state.watching = !!st.running;
    if (st.running && st.tab) state.tab = st.tab;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("is-active", b.dataset.tab === state.tab));
    await loadMatches();
    if (state.watching) startPolling();
  } catch (e) {
    $("board").innerHTML = `<div class="empty">Failed to reach match-board server / skill.<br/>${escapeHtml(e.message)}</div>`;
  }
}

init();
