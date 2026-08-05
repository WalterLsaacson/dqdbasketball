/** HTTP client for the polymarket-board server (skill bridge). */

export async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
    ...options,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

export function fetchMatches({
  league = "all",
  includeClosed = false,
  withinHours = 48,
  max = 100,
  refresh = false,
} = {}) {
  const qs = new URLSearchParams({
    league,
    include_closed: includeClosed ? "1" : "0",
    within_hours: String(withinHours),
    max: String(max),
    refresh: refresh ? "1" : "0",
  });
  return api(`/api/matches?${qs}`);
}

export function fetchStatus() {
  return api("/api/status");
}

export function startSkill({
  league = "all",
  includeClosed = false,
  withinHours = 48,
  interval = 600,
} = {}) {
  return api("/api/fetch/start", {
    method: "POST",
    body: JSON.stringify({
      league,
      include_closed: includeClosed,
      within_hours: withinHours,
      interval,
    }),
  });
}

export function stopSkill() {
  return api("/api/fetch/stop", { method: "POST", body: "{}" });
}
