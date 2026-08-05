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

export function formatWallClock(elapsedSec) {
  let n = Math.max(0, Math.floor(Number(elapsedSec) || 0));
  const hh = Math.floor(n / 3600);
  n %= 3600;
  const mm = Math.floor(n / 60);
  const ss = n % 60;
  if (hh > 0) {
    return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
  }
  return `${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

function statusRaw(dqd) {
  let raw = String(dqd?.status_raw || "").toLowerCase();
  if (raw) return raw;
  const disp = String(dqd?.status || "").toLowerCase();
  if (disp.startsWith("playing") || disp.includes("进行中")) return "playing";
  if (disp.startsWith("played") || disp === "ft" || disp === "完场") return "played";
  if (disp.startsWith("fixture") || disp === "未开赛") return "fixture";
  return "";
}

/** Official match clock with stoppage, e.g. 45'+2' */
export function officialClock(dqd) {
  if (!dqd) return "—";
  if (dqd.official_clock) return dqd.official_clock;
  const raw = statusRaw(dqd);
  const minute = String(dqd.minute || "");
  const injury = Number(dqd.injury_time || 0);
  if (raw === "playing") {
    if (injury > 0 && minute) return `${minute}'+${injury}'`;
    return dqd.minute_str || (minute ? `${minute}'` : "Playing");
  }
  if (raw === "played") return "FT";
  if (raw === "fixture") return "未开赛";
  return dqd.status || "—";
}

/** Live wall-clock from kickoff timestamp. */
export function wallClockNow(dqd) {
  const raw = statusRaw(dqd);
  if (raw === "fixture") return "--:--";
  if (raw === "played") return "结束";
  if (raw !== "playing") return dqd?.wall_clock || "—";
  const ts = Number(dqd.match_timestamp || 0);
  if (!ts) return dqd?.wall_clock || "—";
  const elapsed = Math.floor(Date.now() / 1000) - ts;
  return formatWallClock(elapsed);
}

export function scoreText(dqd) {
  if (dqd?.home_score == null || dqd?.away_score == null) return "vs";
  return `${dqd.home_score} - ${dqd.away_score}`;
}

export function isLive(dqd) {
  return statusRaw(dqd) === "playing";
}

export function isFinished(dqd, row) {
  if (row?.finished || dqd?.is_finished) return true;
  return statusRaw(dqd) === "played";
}

export function leagueColor(row) {
  const dqd = row?.dongqiudi || {};
  const pm = row?.polymarket || {};
  const c = (dqd.league_color || "").trim();
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(c)) return c;
  let h = 0;
  const key = String(pm.league_id || dqd.league_id || dqd.league || "");
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 58% 46%)`;
}

export function groupMatches(matches, filterLeagueId = null) {
  const map = new Map();
  for (const row of matches) {
    const dqd = row.dongqiudi || {};
    const pm = row.polymarket || {};
    const id = pm.league_id || dqd.league_id || "unknown";
    if (filterLeagueId && id !== filterLeagueId) continue;
    if (!map.has(id)) {
      map.set(id, {
        id,
        name: pm.league || dqd.league || id,
        color: leagueColor(row),
        matches: [],
      });
    }
    map.get(id).matches.push(row);
  }
  return [...map.values()].sort(
    (a, b) => b.matches.length - a.matches.length || a.name.localeCompare(b.name),
  );
}

export function formatBeijingDateTime(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}
