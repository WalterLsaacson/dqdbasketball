/** Display helpers + fixed league color palette. */

export const BEIJING_TZ = "Asia/Shanghai";

/** Distinct colors per Polymarket soccer league code. */
export const LEAGUE_COLORS = {
  epl: "#3D195B",
  ucl: "#0B1F66",
  uel: "#F68E1E",
  mls: "#C8102E",
  lal: "#EE8707",
  bun: "#D20515",
  fl1: "#091C3E",
  sea: "#008FD7",
  afc: "#E30613",
  caf: "#008751",
  efa: "#003399",
  fifa: "#326295",
  fifaw: "#E6007E",
  fifwc: "#6CACE4",
  nor: "#BA0C2F",
  swe: "#006AA7",
  kor: "#C60C30",
  bra: "#009C3B",
  mex: "#006847",
  nwsl: "#E31837",
  rou1: "#002B7F",
  per1: "#D91023",
  wwcquefa: "#7B2D8E",
};

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

export function leagueColor(m) {
  const id = String(m.league_id || m.id || "").toLowerCase();
  if (LEAGUE_COLORS[id]) return LEAGUE_COLORS[id];
  let h = 0;
  const key = String(m.league_id || m.league || "x");
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 58% 46%)`;
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

export function formatKickoff(match) {
  if (match?.kickoff_beijing) {
    // "YYYY-MM-DD HH:MM" → show date + time when not today-ish
    const s = String(match.kickoff_beijing);
    const [datePart, timePart] = s.split(" ");
    if (datePart && timePart) {
      const [, mo, dy] = datePart.split("-");
      return `${mo}/${dy} ${timePart}`;
    }
    return s;
  }
  return match?.time || "--:--";
}

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
