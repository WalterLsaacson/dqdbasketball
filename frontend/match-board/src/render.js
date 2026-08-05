import { state } from "./state.js";
import {
  $,
  escapeHtml,
  scoreText,
  isLive,
  groupMatches,
  formatKickoffBeijing,
  formatKickoffBeijingTitle,
  formatBeijingDateTime,
} from "./utils.js";

export function renderRail(onFilter) {
  const rail = $("leagueRail");
  const allCount = state.matches.length;
  const fullGroups = groupMatches(state.matches, null);
  const chips = [
    `<button class="league-chip ${state.filterLeagueId ? "" : "is-active"}" data-league="" type="button">
      <span class="league-chip__dot" style="--dot:#c8f542;background:#c8f542"></span>
      <span class="league-chip__name">All leagues</span>
      <span class="league-chip__count">${allCount}</span>
    </button>`,
  ];
  for (const g of fullGroups) {
    const active = state.filterLeagueId === g.id ? "is-active" : "";
    chips.push(`
      <button class="league-chip ${active}" data-league="${escapeHtml(g.id)}" type="button">
        <span class="league-chip__dot" style="--dot:${g.color};background:${g.color}"></span>
        <span class="league-chip__name">${escapeHtml(g.name)}</span>
        <span class="league-chip__count">${g.matches.length}</span>
      </button>
    `);
  }
  rail.innerHTML = chips.join("");
  rail.querySelectorAll(".league-chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-league") || "";
      onFilter(id || null);
    });
  });
}

export function renderBoard() {
  const board = $("board");
  const groups = groupMatches(state.matches, state.filterLeagueId);
  if (!groups.length) {
    board.innerHTML = `<div class="empty" id="empty">No matches in this tab.</div>`;
    return;
  }
  board.innerHTML = groups
    .map((g) => {
      const rows = g.matches
        .map((m) => {
          const live = isLive(m);
          const flashKey = `${m.id}:${m.home_score}-${m.away_score}`;
          const flash = state.prevScores.has(m.id) && state.prevScores.get(m.id) !== flashKey;
          const half =
            m.home_half !== "" || m.away_half !== ""
              ? `<div class="score__half">HT ${escapeHtml(m.home_half)}-${escapeHtml(m.away_half)}</div>`
              : "";
          const kickoff = formatKickoffBeijing(m);
          const kickoffTitle = formatKickoffBeijingTitle(m);
          return `
            <div class="match-row ${live ? "is-live" : ""} ${flash ? "is-flash" : ""}" data-id="${escapeHtml(m.id)}">
              <div class="match-time" title="${escapeHtml(kickoffTitle)}">${escapeHtml(kickoff)}</div>
              <div class="team team--home">
                <span class="team__name" title="${escapeHtml(m.home)}">${escapeHtml(m.home)}</span>
                ${m.home_logo ? `<img class="team__logo" src="${escapeHtml(m.home_logo)}" alt="" />` : ""}
              </div>
              <div class="score">
                <div class="score__main ${live ? "is-live" : ""}">${escapeHtml(scoreText(m))}</div>
                ${half}
              </div>
              <div class="team team--away">
                ${m.away_logo ? `<img class="team__logo" src="${escapeHtml(m.away_logo)}" alt="" />` : ""}
                <span class="team__name" title="${escapeHtml(m.away)}">${escapeHtml(m.away)}</span>
              </div>
              <div class="status ${live ? "is-live" : ""}">${escapeHtml(m.status || "")}</div>
            </div>
          `;
        })
        .join("");
      return `
        <section class="league-block" style="--league:${g.color}">
          <header class="league-head">
            <span class="league-head__bar"></span>
            <div class="league-head__name">${escapeHtml(g.name)}</div>
            <div class="league-head__meta">${g.matches.length} matches · id ${escapeHtml(g.id)}</div>
          </header>
          ${rows}
        </section>
      `;
    })
    .join("");

  for (const m of state.matches) {
    state.prevScores.set(m.id, `${m.id}:${m.home_score}-${m.away_score}`);
  }
}

export function renderMeta(snap) {
  const liveN = state.matches.filter(isLive).length;
  $("pillCount").textContent = `${state.matches.length} matches`;
  $("pillLive").textContent = `Live ${liveN}`;
  $("pillLive").classList.toggle("is-live", liveN > 0);
  $("pillFetched").textContent = snap?.fetched_at
    ? `更新 ${formatBeijingDateTime(snap.fetched_at, { withSeconds: true, withLabel: true })}`
    : "—";
  $("pillStatus").textContent = state.watching ? "Skill watching" : "Idle";
  $("pillStatus").classList.toggle("is-live", state.watching);
  $("btnStart").disabled = state.watching;
  $("btnStop").disabled = !state.watching;
}

export function render(snap) {
  renderRail((id) => {
    state.filterLeagueId = id;
    render(snap);
  });
  renderBoard();
  renderMeta(snap);
}

export function pushToast(ev) {
  const key = `${ev.match_id}:${ev.ts}:${ev.curr?.home}-${ev.curr?.away}`;
  if (state.seenEventKeys.has(key)) return;
  state.seenEventKeys.add(key);

  const stack = $("toasts");
  const el = document.createElement("div");
  el.className = "toast";
  const label = ev.is_goal ? "GOAL" : "SCORE";
  el.innerHTML = `
    <div class="toast__badge">${label}</div>
    <div class="toast__body">
      <div class="toast__title">${escapeHtml(ev.home)} ${ev.curr?.home ?? "-"} - ${ev.curr?.away ?? "-"} ${escapeHtml(ev.away)}</div>
      <div class="toast__meta">${escapeHtml(ev.league || "")} · ${escapeHtml(ev.side || "")} · was ${ev.prev?.home}-${ev.prev?.away}${ev.ts ? ` · ${escapeHtml(formatBeijingDateTime(ev.ts, { withSeconds: false, withLabel: true }))}` : ""}</div>
    </div>
  `;
  stack.prepend(el);
  setTimeout(() => {
    el.classList.add("is-out");
    setTimeout(() => el.remove(), 260);
  }, 5200);
}
