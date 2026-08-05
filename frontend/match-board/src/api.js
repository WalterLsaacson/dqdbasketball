/** HTTP client for the match-board server (skill bridge). */

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

export function fetchMatches(tab) {
  return api(`/api/matches?tab=${encodeURIComponent(tab)}`);
}

export function fetchStatus() {
  return api("/api/status");
}

export function startWatch(tab, interval = 15, idleInterval = 60) {
  return api("/api/watch/start", {
    method: "POST",
    body: JSON.stringify({ tab, interval, idle_interval: idleInterval }),
  });
}

export function stopWatch() {
  return api("/api/watch/stop", { method: "POST", body: "{}" });
}
