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

export function fetchMatches() {
  return api("/api/matches");
}

export function fetchStatus() {
  return api("/api/status");
}

export function fetchEvents(limit = 30) {
  return api(`/api/events?limit=${limit}`);
}

export function runOnce({ offline = false } = {}) {
  return api("/api/bridge/once", {
    method: "POST",
    body: JSON.stringify({ offline }),
  });
}

export function startBridge() {
  return api("/api/bridge/start", {
    method: "POST",
    body: JSON.stringify({ tab: "full" }),
  });
}

export function stopBridge() {
  return api("/api/bridge/stop", { method: "POST", body: "{}" });
}
