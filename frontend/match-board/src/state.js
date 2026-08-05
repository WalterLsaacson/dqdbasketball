/** Shared UI state for the match-board module. */

export const state = {
  tab: "full",
  matches: [],
  leagues: [],
  filterLeagueId: null,
  watching: false,
  lastTicks: 0,
  seenEventKeys: new Set(),
  prevScores: new Map(),
  pollTimer: null,
};
