export const state = {
  matches: [],
  filterLeagueId: null,
  running: false,
  /** Quote owns in-process bridge; board only reads data/bridge files. */
  quoteOwned: false,
  lastMeta: null,
  pollTimer: null,
  clockTimer: null,
  seenEventKeys: new Set(),
};
