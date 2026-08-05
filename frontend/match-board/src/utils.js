/** Display timezone: Beijing (Asia/Shanghai, UTC+8). */
export const BEIJING_TZ = "Asia/Shanghai";

export function $(id) {
  return document.getElementById(id);
}

export function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/**
 * Parse API `start_play` ("YYYY-MM-DD HH:mm:ss") as UTC — same as official site / skill.
 */
export function parseStartPlayUtc(startPlay) {
  if (!startPlay) return null;
  const s = String(startPlay).trim();
  if (!s) return null;
  if (/[zZ]|[+-]\d{2}:?\d{2}$/.test(s)) {
    const d = new Date(s.includes("T") ? s : s.replace(" ", "T"));
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const d = new Date(s.replace(" ", "T") + "Z");
  return Number.isNaN(d.getTime()) ? null : d;
}

function partsInBeijing(date) {
  const fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: BEIJING_TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  const bag = Object.fromEntries(fmt.formatToParts(date).map((p) => [p.type, p.value]));
  return {
    year: bag.year,
    month: bag.month,
    day: bag.day,
    hour: bag.hour === "24" ? "00" : bag.hour,
    minute: bag.minute,
    second: bag.second,
  };
}

/** Kickoff time HH:mm in Beijing. */
export function formatKickoffBeijing(match) {
  const d = parseStartPlayUtc(match?.start_play);
  if (d) {
    const p = partsInBeijing(d);
    return `${p.hour}:${p.minute}`;
  }
  return match?.time || "--:--";
}

/** Full kickoff label, e.g. 07/19 20:00 · 北京时间 */
export function formatKickoffBeijingTitle(match) {
  const d = parseStartPlayUtc(match?.start_play);
  if (!d) return "北京时间";
  const p = partsInBeijing(d);
  return `${p.month}/${p.day} ${p.hour}:${p.minute} 北京时间`;
}

/** Format ISO / date string as Beijing wall clock. */
export function formatBeijingDateTime(value, { withSeconds = true, withLabel = true } = {}) {
  if (!value) return "—";
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const p = partsInBeijing(d);
  const base = withSeconds
    ? `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`
    : `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}`;
  return withLabel ? `${base} 北京时间` : base;
}

export function todayBeijing() {
  const p = partsInBeijing(new Date());
  return `${p.year}-${p.month}-${p.day}`;
}

export function scoreText(m) {
  if (m.home_score == null || m.away_score == null) return "vs";
  return `${m.home_score} - ${m.away_score}`;
}

export function isLive(m) {
  const raw = (m.status_raw || "").toLowerCase();
  return raw === "playing" || String(m.status || "").startsWith("Playing");
}

export function leagueColor(m) {
  const c = (m.league_color || "").trim();
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(c)) return c;
  let h = 0;
  const key = String(m.league_id || m.league || "");
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 62% 48%)`;
}

export function groupMatches(matches, filterLeagueId = null) {
  const map = new Map();
  for (const m of matches) {
    if (filterLeagueId && m.league_id !== filterLeagueId) continue;
    const key = m.league_id || m.league;
    if (!map.has(key)) {
      map.set(key, {
        id: m.league_id,
        name: m.league,
        color: leagueColor(m),
        matches: [],
      });
    }
    map.get(key).matches.push(m);
  }
  return [...map.values()].sort(
    (a, b) => b.matches.length - a.matches.length || a.name.localeCompare(b.name),
  );
}
