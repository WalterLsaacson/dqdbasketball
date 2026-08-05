import { state } from "./state.js";
import {
  $,
  escapeHtml,
  groupMatches,
  formatKickoff,
  formatBeijingDateTime,
  leagueColor,
} from "./utils.js";

export function renderRail(onFilter) {
  const rail = $("leagueRail");
  const allCount = state.matches.length;
  const fullGroups = groupMatches(state.matches, null);
  const chips = [
    `<button class="league-chip ${state.filterLeagueId ? "" : "is-active"}" data-league="" type="button">
      <span class="league-chip__dot" style="--dot:#f0b429;background:#f0b429"></span>
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
    const hint =
      state.withinHours > 0
        ? `未来 ${state.withinHours} 小时暂无赛程。可关闭 Next 48h，或勾选 Include closed。`
        : "No matches. Try Include closed 或 Start Skill。";
    board.innerHTML = `<div class="empty">${escapeHtml(hint)}</div>`;
    return;
  }
  board.innerHTML = groups
    .map((g, idx) => {
      const rows = g.matches
        .map((m) => {
          const kickoff = formatKickoff(m);
          const closed = m.closed ? "is-closed" : "";
          const badgeColor = leagueColor(m);
          return `
            <a class="match-row ${closed}" href="${escapeHtml(m.url || "#")}" target="_blank" rel="noopener">
              <div class="match-time" title="${escapeHtml(m.kickoff_beijing || "")} 北京时间">${escapeHtml(kickoff)}</div>
              <div class="team team--home">
                <span class="team__name" title="${escapeHtml(m.home)}">${escapeHtml(m.home)}</span>
              </div>
              <div class="vs">vs</div>
              <div class="team team--away">
                <span class="team__name" title="${escapeHtml(m.away)}">${escapeHtml(m.away)}</span>
              </div>
              <div class="league-tag" style="--tag:${badgeColor}">
                <span class="league-tag__dot"></span>
                <span class="league-tag__name">${escapeHtml(m.league || m.league_id || "")}</span>
              </div>
            </a>
          `;
        })
        .join("");
      return `
        <section class="league-block" style="--league:${g.color}; animation-delay:${idx * 40}ms">
          <header class="league-head">
            <span class="league-head__bar"></span>
            <div class="league-head__name">${escapeHtml(g.name)}</div>
            <div class="league-head__badge" style="--tag:${g.color}">${escapeHtml(g.id)}</div>
            <div class="league-head__meta">${g.matches.length} games</div>
          </header>
          ${rows}
        </section>
      `;
    })
    .join("");
}

function formatWindowPill(snap) {
  const w = state.window || snap?.window;
  if (state.withinHours <= 0) return "window: all";
  if (!w?.start_utc || !w?.end_utc) return `window: next ${state.withinHours}h`;
  const start = formatBeijingDateTime(w.start_utc, { withSeconds: false, withLabel: false });
  const end = formatBeijingDateTime(w.end_utc, { withSeconds: false, withLabel: false });
  // show compact Beijing range
  const startShort = start.slice(5); // MM-DD HH:MM
  const endShort = end.slice(5);
  return `窗口 ${startShort} → ${endShort}`;
}

export function renderMeta(snap) {
  const leagueN = new Set(state.matches.map((m) => m.league_id)).size;
  $("pillCount").textContent = `${state.matches.length} matches`;
  $("pillLeagues").textContent = `${leagueN} leagues`;
  $("pillWindow").textContent = formatWindowPill(snap);
  $("pillWindow").classList.toggle("is-live", state.withinHours > 0);
  $("pillProxy").textContent = `proxy ${state.proxy || snap?.proxy || "—"}`;
  $("pillFetched").textContent = snap?.fetched_at
    ? `更新 ${formatBeijingDateTime(snap.fetched_at, { withSeconds: true, withLabel: true })}`
    : "—";
  if (state.stale) {
    $("pillStatus").textContent = "Stale cache";
    $("pillStatus").classList.remove("is-live");
  } else {
    $("pillStatus").textContent = state.running ? "Skill running" : "Idle";
    $("pillStatus").classList.toggle("is-live", state.running);
  }
  $("btnStart").disabled = state.running;
  $("btnStop").disabled = !state.running;
}

export function render(snap) {
  if (snap?.proxy) state.proxy = snap.proxy;
  if (snap?.window !== undefined) state.window = snap.window;
  renderRail((id) => {
    state.filterLeagueId = id;
    render(snap);
  });
  renderBoard();
  renderMeta(snap);
}
