/** Shared UI state for the polymarket-board module. */

export const state = {
  league: "all",
  includeClosed: false,
  /** Match skill default: next 48h. 0 = no window. */
  withinHours: 48,
  matches: [],
  leagues: [],
  window: null,
  stale: false,
  filterLeagueId: null,
  running: false,
  lastTicks: 0,
  pollTimer: null,
  proxy: null,
};
