#!/usr/bin/env python3
"""
Fantasy FP Rankings
-------------------
Pulls this season's NHL regular-season stats from the NHL's public stats API,
scores every skater and goalie with YOUR Yahoo league's point values, and
builds a ranked report (HTML + CSV).

What you get (in the ./fp_output folder):
  - fp_rankings.html   -> opens in your browser: Skaters, Goalies, Hidden Value tabs
  - skaters.csv        -> every skater with FP totals and per-game numbers
  - goalies.csv        -> every goalie with FP totals and per-game numbers

How to run (Windows):
  1. Install Python 3.9+ from python.org (tick "Add Python to PATH").
  2. Save this file somewhere, e.g. Documents\\fantasy-hockey\\
  3. Open Command Prompt in that folder and run:
         python fantasy_fp_rankings.py
  Optional flags:
         --season 20262027   pick a season (default: current season)
         --min-gp 5          minimum games played to appear in the rankings
         --out fp_output     output folder
         --no-open           don't auto-open the report in your browser

Phone version: put this file in a GitHub repo with the included
.github/workflows/update-rankings.yml - GitHub runs it every morning and
publishes the report as a web page you can open on your phone.

No extra packages needed - standard library only.
"""

import argparse
import csv
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# ---------------------------------------------------------------------------
# YOUR LEAGUE SCORING  (edit these if your league settings change)
# ---------------------------------------------------------------------------
SKATER_SCORING = {
    "goals": 3.0,
    "assists": 2.0,
    "plusMinus": 0.25,
    "ppPoints": 1.0,
    "shGoals": 4.0,
    "shAssists": 2.0,
    "gameWinningGoals": 1.0,
    "shots": 0.15,
    "hits": 0.3,
    "blockedShots": 0.4,
}

GOALIE_SCORING = {
    "wins": 3.0,
    "goalsAgainst": -0.85,
    "saves": 0.20,
    "shutouts": 4.0,
}

# Which skater categories count as "scoring" vs "peripherals".
# Peripherals tend to be steady week to week; scoring swings with luck.
SCORING_KEYS = ["goals", "assists", "ppPoints", "shGoals", "shAssists", "gameWinningGoals", "plusMinus"]
PERIPHERAL_KEYS = ["shots", "hits", "blockedShots"]

LEAGUE_LABELS = {
    "goals": "G", "assists": "A", "plusMinus": "+/-", "ppPoints": "PPP",
    "shGoals": "SHG", "shAssists": "SHA", "gameWinningGoals": "GWG",
    "shots": "SOG", "hits": "HIT", "blockedShots": "BLK",
    "wins": "W", "goalsAgainst": "GA", "saves": "SV", "shutouts": "SHO",
}

# ---------------------------------------------------------------------------
# NHL STATS API
# ---------------------------------------------------------------------------
BASE_URL = "https://api.nhle.com/stats/rest/en"
PAGE_SIZE = 100
USER_AGENT = "personal-fantasy-fp-rankings/1.0"


def local_now():
    """Toronto time, so the 'updated' stamp is right even when GitHub runs this in UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Toronto"))
    except Exception:
        return datetime.datetime.now()


def current_season_id(today=None):
    """NHL seasons are written like 20262027. A new season starts in the fall."""
    today = today or datetime.date.today()
    start_year = today.year if today.month >= 9 else today.year - 1
    return f"{start_year}{start_year + 1}"


def get_json(url, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not load {url}\n  Reason: {last_error}")


def fetch_report(path, season_id, sort_prop):
    """Fetch every row of a stats report, page by page."""
    rows = []
    start = 0
    while True:
        params = {
            "isAggregate": "false",
            "isGame": "false",
            "start": start,
            "limit": PAGE_SIZE,
            "sort": json.dumps([
                {"property": sort_prop, "direction": "DESC"},
                {"property": "playerId", "direction": "ASC"},
            ]),
            "cayenneExp": f"seasonId={season_id} and gameTypeId=2",
        }
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        data = get_json(f"{BASE_URL}/{path}?{query}")
        batch = data.get("data", []) or []
        rows.extend(batch)
        total = data.get("total", len(rows))
        start += PAGE_SIZE
        if not batch or start >= total:
            break
        time.sleep(0.25)  # be polite to the API
    return rows


# ---------------------------------------------------------------------------
# MERGING + SCORING
# ---------------------------------------------------------------------------
def num(value):
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


POS_NAMES = {"C": "C", "L": "LW", "R": "RW", "D": "D", "G": "G"}

SUMMARY_FIELDS = ["gamesPlayed", "goals", "assists", "points", "plusMinus",
                  "ppPoints", "shGoals", "shPoints", "gameWinningGoals", "shots"]
REALTIME_FIELDS = ["hits", "blockedShots"]
GOALIE_FIELDS = ["gamesPlayed", "gamesStarted", "wins", "losses", "otLosses",
                 "goalsAgainst", "saves", "shotsAgainst", "shutouts"]


def add_teams(player, team_str):
    for t in str(team_str or "").split(","):
        t = t.strip()
        if t and t not in player["teamList"]:
            player["teamList"].append(t)


def merge_skaters(summary_rows, realtime_rows):
    players = {}

    def blank(row):
        return {
            "playerId": row.get("playerId"),
            "name": row.get("skaterFullName") or row.get("lastName") or "Unknown",
            "pos": POS_NAMES.get(row.get("positionCode", ""), row.get("positionCode", "")),
            "teamList": [],
            **{f: 0.0 for f in SUMMARY_FIELDS + REALTIME_FIELDS},
        }

    # Summing by playerId handles players who show up on more than one row (trades).
    for r in summary_rows:
        p = players.setdefault(r.get("playerId"), blank(r))
        for f in SUMMARY_FIELDS:
            p[f] += num(r.get(f))
        add_teams(p, r.get("teamAbbrevs"))

    for r in realtime_rows:
        pid = r.get("playerId")
        if pid not in players:
            continue  # realtime-only rows have no scoring stats; skip
        p = players[pid]
        for f in REALTIME_FIELDS:
            p[f] += num(r.get(f))

    for p in players.values():
        p["shAssists"] = max(0.0, p["shPoints"] - p["shGoals"])
        p["team"] = "/".join(p["teamList"])
        del p["teamList"]
    return list(players.values())


def score_skaters(players):
    for p in players:
        gp = p["gamesPlayed"]
        scoring = sum(p[k] * SKATER_SCORING[k] for k in SCORING_KEYS)
        peripheral = sum(p[k] * SKATER_SCORING[k] for k in PERIPHERAL_KEYS)
        p["fpScoring"] = scoring
        p["fpPeripheral"] = peripheral
        p["fp"] = scoring + peripheral
        p["fpg"] = p["fp"] / gp if gp else 0.0
        p["fpgScoring"] = scoring / gp if gp else 0.0
        p["fpgPeripheral"] = peripheral / gp if gp else 0.0
        p["ppg"] = p["points"] / gp if gp else 0.0
        pos_total = max(scoring, 0) + max(peripheral, 0)
        p["periPct"] = (max(peripheral, 0) / pos_total * 100) if pos_total > 0 else 0.0
    return players


def merge_goalies(rows):
    goalies = {}
    for r in rows:
        pid = r.get("playerId")
        g = goalies.setdefault(pid, {
            "playerId": pid,
            "name": r.get("goalieFullName") or r.get("lastName") or "Unknown",
            "pos": "G",
            "teamList": [],
            **{f: 0.0 for f in GOALIE_FIELDS},
        })
        for f in GOALIE_FIELDS:
            g[f] += num(r.get(f))
        add_teams(g, r.get("teamAbbrevs"))
    for g in goalies.values():
        g["team"] = "/".join(g["teamList"])
        del g["teamList"]
    return list(goalies.values())


def score_goalies(goalies):
    for g in goalies:
        gp, gs = g["gamesPlayed"], g["gamesStarted"]
        g["fpWins"] = g["wins"] * GOALIE_SCORING["wins"]
        g["fpSaves"] = g["saves"] * GOALIE_SCORING["saves"]
        g["fpGA"] = g["goalsAgainst"] * GOALIE_SCORING["goalsAgainst"]
        g["fpShutouts"] = g["shutouts"] * GOALIE_SCORING["shutouts"]
        g["fp"] = g["fpWins"] + g["fpSaves"] + g["fpGA"] + g["fpShutouts"]
        g["fpg"] = g["fp"] / gp if gp else 0.0
        g["fpgs"] = g["fp"] / gs if gs else 0.0
        g["svPct"] = g["saves"] / g["shotsAgainst"] if g["shotsAgainst"] else 0.0
        g["saFor"] = g["shotsAgainst"] / gp if gp else 0.0  # shots faced per game
        # FP from saves minus goals against = what he earns even without a win
        g["fpgNoWin"] = (g["fpSaves"] + g["fpGA"]) / gp if gp else 0.0
    return goalies


def rank(items, key, out_field, reverse=True):
    ordered = sorted(items, key=lambda x: x[key], reverse=reverse)
    for i, item in enumerate(ordered, start=1):
        item[out_field] = i


def add_ranks(skaters, goalies, min_gp):
    q_sk = [p for p in skaters if p["gamesPlayed"] >= min_gp]
    q_g = [g for g in goalies if g["gamesPlayed"] >= min_gp]

    rank(q_sk, "fpg", "rank")
    rank(q_sk, "ppg", "ppgRank")
    for p in q_sk:
        # Positive gap = better in your league than NHL points suggest
        p["gap"] = p["ppgRank"] - p["rank"]
    for pos in {p["pos"] for p in q_sk}:
        rank([p for p in q_sk if p["pos"] == pos], "fpg", "posRank")

    rank(q_g, "fpg", "rank")
    return q_sk, q_g


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------
SKATER_CSV_COLS = [
    "rank", "posRank", "name", "team", "pos", "gamesPlayed", "goals", "assists", "points",
    "ppPoints", "shGoals", "shAssists", "gameWinningGoals", "plusMinus", "shots", "hits",
    "blockedShots", "fp", "fpg", "fpScoring", "fpPeripheral", "fpgScoring", "fpgPeripheral",
    "periPct", "ppg", "ppgRank", "gap", "playerId",
]
GOALIE_CSV_COLS = [
    "rank", "name", "team", "gamesPlayed", "gamesStarted", "wins", "losses", "otLosses",
    "saves", "shotsAgainst", "goalsAgainst", "svPct", "shutouts", "fp", "fpg", "fpgs",
    "fpWins", "fpSaves", "fpGA", "fpShutouts", "fpgNoWin", "saFor", "playerId",
]


def write_csv(path, rows, cols, sort_key="fp"):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(rows, key=lambda x: x.get(sort_key, 0), reverse=True):
            out = []
            for c in cols:
                v = r.get(c, "")
                if isinstance(v, float):
                    v = round(v, 3)
                    if v == int(v) and c not in ("fpg", "fpgs", "svPct", "ppg", "periPct"):
                        v = int(v)
                out.append(v)
            w.writerow(out)


def slim(rows, keys):
    out = []
    for r in rows:
        d = {}
        for k in keys:
            v = r.get(k)
            d[k] = round(v, 4) if isinstance(v, float) else v
        out.append(d)
    return out


def season_label(season_id):
    s = str(season_id)
    return f"{s[:4]}–{s[6:]}" if len(s) == 8 else s


def write_html(path, skaters, goalies, season_id, min_gp, generated_at):
    sk_keys = ["rank", "posRank", "name", "team", "pos", "gamesPlayed", "goals", "assists", "points",
               "ppPoints", "shots", "hits", "blockedShots", "plusMinus", "fp", "fpg",
               "fpgScoring", "fpgPeripheral", "periPct", "ppg", "ppgRank", "gap"]
    g_keys = ["rank", "name", "team", "gamesPlayed", "gamesStarted", "wins", "saves", "goalsAgainst",
              "svPct", "shutouts", "fp", "fpg", "fpgs", "fpgNoWin", "saFor"]
    payload = {
        "season": season_label(season_id),
        "minGp": min_gp,
        "generated": generated_at,
        "skaterScoring": [[LEAGUE_LABELS[k], v] for k, v in SKATER_SCORING.items()],
        "goalieScoring": [[LEAGUE_LABELS[k], v] for k, v in GOALIE_SCORING.items()],
        "skaters": slim(skaters, sk_keys),
        "goalies": slim(goalies, g_keys),
    }
    data_json = json.dumps(payload).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__DATA__", data_json))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0f17">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="FP Rankings">
<title>FP Rankings</title>
<style>
  :root{
    --bg:#0b0f17; --panel:#121826; --panel2:#182033; --line:#243049;
    --text:#e8eef9; --muted:#8a97b0; --ice:#cfe3ff;
    --scoring:#4da3ff; --peri:#ffb547; --good:#3ddc97; --bad:#ff6b6b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.45 "Inter","Segoe UI",system-ui,-apple-system,sans-serif}
  header{padding:28px 32px 18px;border-bottom:1px solid var(--line);
    background:linear-gradient(180deg,#111a2b 0%,var(--bg) 100%)}
  .eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
  h1{margin:4px 0 10px;font-size:28px;font-weight:800;letter-spacing:-.02em}
  h1 span{color:var(--scoring)}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
    padding:3px 10px;font-size:12px;color:var(--ice);white-space:nowrap}
  .chip b{color:var(--text)}
  .chip.neg b{color:var(--bad)}
  .meta{color:var(--muted);font-size:12px;margin-top:10px}
  nav{display:flex;gap:4px;padding:14px 32px 0}
  nav button{background:none;border:none;color:var(--muted);font:inherit;font-weight:700;
    padding:10px 16px;border-radius:10px 10px 0 0;cursor:pointer;font-size:14px}
  nav button.active{background:var(--panel);color:var(--text)}
  main{background:var(--panel);margin:0 32px 40px;border-radius:0 12px 12px 12px;padding:18px}
  .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
  input[type=search]{background:var(--panel2);border:1px solid var(--line);color:var(--text);
    border-radius:8px;padding:8px 12px;font:inherit;min-width:220px}
  .seg{display:flex;background:var(--panel2);border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .seg button{background:none;border:none;color:var(--muted);font:inherit;font-weight:600;
    padding:7px 12px;cursor:pointer}
  .seg button.active{background:var(--scoring);color:#06101f}
  .legend{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-left:auto}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
  .scroll{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th{position:sticky;top:0;background:var(--panel);text-align:right;font-size:11px;
    text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding:8px 8px;
    border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none}
  th.l,td.l{text-align:left}
  th.sorted{color:var(--text)}
  th.sorted::after{content:" \25BE"}
  th.sorted.asc::after{content:" \25B4"}
  td{padding:7px 8px;border-bottom:1px solid #1a2236;text-align:right;white-space:nowrap}
  tr:hover td{background:#16203a}
  td.name{font-weight:700}
  td.fpg{font-weight:800;color:var(--ice)}
  .pos{display:inline-block;min-width:28px;text-align:center;font-size:11px;font-weight:700;
    border-radius:4px;padding:1px 5px;background:var(--panel2);color:var(--ice)}
  .pos.D{background:#3a2a10;color:var(--peri)}
  .bar{display:flex;height:10px;width:140px;background:#0e1422;border-radius:3px;overflow:hidden}
  .bar .s{background:var(--scoring)} .bar .p{background:var(--peri)}
  .up{color:var(--good);font-weight:700} .down{color:var(--bad);font-weight:700}
  .muted{color:var(--muted)}
  .hv{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  .hv h3{margin:4px 0 2px;font-size:15px}
  .hv p{margin:0 0 10px;color:var(--muted);font-size:12px}
  .note{color:var(--muted);font-size:12px;margin-top:12px}
  .sub{display:none;font-weight:400;font-size:11px;color:var(--muted)}
  @media (max-width:1000px){ .hv{grid-template-columns:1fr} main{margin:0 10px 30px}
    header,nav{padding-left:12px;padding-right:12px} }
  @media (max-width:700px){
    .hs{display:none}
    .sub{display:block}
    header{padding-top:calc(16px + env(safe-area-inset-top))}
    h1{font-size:24px}
    .chip{font-size:11px;padding:2px 8px}
    nav{padding-top:10px} nav button{padding:10px 12px;flex:1}
    main{margin:0 0 24px;border-radius:0;padding:12px 8px}
    .controls{gap:8px}
    input[type=search]{min-width:0;width:100%;font-size:16px}
    .seg{width:100%} .seg button{flex:1;padding:9px 0}
    .legend{margin-left:0;font-size:11px;gap:10px}
    td,th{padding:9px 5px}
    .bar{width:70px}
  }
</style>
</head>
<body>
<header>
  <div class="eyebrow">Yahoo points league &middot; personal rankings</div>
  <h1>Fantasy Points <span>Rankings</span></h1>
  <div class="chips" id="skChips"></div>
  <div class="chips" id="gChips" style="margin-top:6px"></div>
  <div class="meta" id="meta"></div>
</header>
<nav>
  <button data-tab="sk" class="active">Skaters</button>
  <button data-tab="g">Goalies</button>
  <button data-tab="hv">Hidden Value</button>
</nav>
<main>
  <section id="tab-sk">
    <div class="controls">
      <input type="search" id="skSearch" placeholder="Search player or team...">
      <div class="seg" id="posSeg">
        <button data-pos="ALL" class="active">All</button>
        <button data-pos="F">F</button>
        <button data-pos="C">C</button>
        <button data-pos="LW">LW</button>
        <button data-pos="RW">RW</button>
        <button data-pos="D">D</button>
      </div>
      <div class="legend"><span><i style="background:var(--scoring)"></i>Scoring FP/GP</span>
        <span><i style="background:var(--peri)"></i>Peripheral FP/GP (SOG, HIT, BLK)</span></div>
    </div>
    <div class="scroll"><table id="skTable"></table></div>
  </section>
  <section id="tab-g" hidden>
    <div class="controls">
      <input type="search" id="gSearch" placeholder="Search goalie or team...">
    </div>
    <div class="scroll"><table id="gTable"></table></div>
    <div class="note">"No-win FP/GP" = saves &times; 0.2 minus goals against &times; 0.85, per game. It is what a goalie earns even when he loses, and it mostly comes from facing lots of shots.</div>
  </section>
  <section id="tab-hv" hidden>
    <div class="hv">
      <div>
        <h3 class="up">&#9650; Better in your league</h3>
        <p>Ranked much higher by FP/GP than by NHL points per game. Usually hits, blocks and shots doing the work.</p>
        <div class="scroll"><table id="hvUp"></table></div>
      </div>
      <div>
        <h3 class="down">&#9660; Worse in your league</h3>
        <p>NHL points look better than their fantasy value here. Few peripherals, so they rely on scoring.</p>
        <div class="scroll"><table id="hvDown"></table></div>
      </div>
    </div>
  </section>
</main>

<script>
const D = __DATA__;

const f1 = v => (v == null ? "" : Number(v).toFixed(1));
const f2 = v => (v == null ? "" : Number(v).toFixed(2));
const f3 = v => (v == null ? "" : Number(v).toFixed(3).replace(/^0/, ""));
const i0 = v => (v == null ? "" : Math.round(v));
const pm = v => { v = Math.round(v); return v > 0 ? "+" + v : String(v); };
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

// ---- header ----
const chip = ([k, v]) => `<span class="chip ${v < 0 ? "neg" : ""}">${k} <b>${v}</b></span>`;
document.getElementById("skChips").innerHTML = D.skaterScoring.map(chip).join("");
document.getElementById("gChips").innerHTML = D.goalieScoring.map(chip).join("");
document.getElementById("meta").textContent =
  `${D.season} regular season · min ${D.minGp} GP · ${D.skaters.length} skaters, ${D.goalies.length} goalies · updated ${D.generated}`;

// ---- tabs ----
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x => x.classList.toggle("active", x === b));
  ["sk", "g", "hv"].forEach(t => document.getElementById("tab-" + t).hidden = t !== b.dataset.tab);
});

// ---- generic sortable table ----
function makeTable(el, cols, getRows, defaultSort, defaultAsc = false) {
  const state = { key: defaultSort, asc: defaultAsc };
  function render() {
    const col = cols.find(c => (c.sort || c.key) === state.key);
    const rows = getRows().slice().sort((a, b) => {
      let x = a[state.key], y = b[state.key];
      if (typeof x === "string") return state.asc ? x.localeCompare(y) : y.localeCompare(x);
      x = x ?? -Infinity; y = y ?? -Infinity;
      return state.asc ? x - y : y - x;
    });
    const head = "<tr>" + cols.map(c => {
      const k = c.sort || c.key;
      const cls = [c.hs ? "hs" : "", c.left ? "l" : "", k === state.key ? "sorted" : "", k === state.key && state.asc ? "asc" : ""].join(" ");
      return `<th class="${cls}" data-k="${k}" title="${esc(c.tip || "")}">${c.label}</th>`;
    }).join("") + "</tr>";
    const body = rows.map(r => "<tr>" + cols.map(c =>
      `<td class="${c.hs ? "hs" : ""} ${c.left ? "l" : ""} ${c.cls || ""}">${c.fmt ? c.fmt(r[c.key], r) : esc(r[c.key])}</td>`
    ).join("") + "</tr>").join("");
    el.innerHTML = `<thead>${head}</thead><tbody>${body || `<tr><td class="l muted" colspan="${cols.length}">No players match.</td></tr>`}</tbody>`;
    el.querySelectorAll("th").forEach(th => th.onclick = () => {
      const k = th.dataset.k;
      if (state.key === k) state.asc = !state.asc;
      else { state.key = k; state.asc = (k === "rank" || k === "posRank" || k === "name" || k === "team" || k === "pos"); }
      render();
    });
  }
  render();
  return render;
}

// ---- skaters ----
const maxFpg = Math.max(0.01, ...D.skaters.map(p => Math.max(0, p.fpgScoring) + Math.max(0, p.fpgPeripheral)));
const splitBar = (_, r) => {
  const s = Math.max(0, r.fpgScoring) / maxFpg * 100, p = Math.max(0, r.fpgPeripheral) / maxFpg * 100;
  return `<div class="bar" title="Scoring ${f2(r.fpgScoring)} / Peripheral ${f2(r.fpgPeripheral)}"><div class="s" style="width:${s}%"></div><div class="p" style="width:${p}%"></div></div>`;
};
const nameCell = (v, r) => `${esc(v)}<div class="sub">${esc(r.team)}</div>`;
const posTag = v => `<span class="pos ${v}">${v}</span>`;

let skPos = "ALL", skQuery = "";
const skCols = [
  { key: "rank", label: "#", tip: "Overall rank by FP per game" },
  { key: "name", label: "Player", left: true, cls: "name", fmt: nameCell },
  { hs: true, key: "team", label: "Team", left: true },
  { key: "pos", label: "Pos", left: true, fmt: posTag },
  { hs: true, key: "posRank", label: "Pos Rk", tip: "Rank at position by FP/GP" },
  { hs: true, key: "gamesPlayed", label: "GP", fmt: i0 },
  { hs: true, key: "goals", label: "G", fmt: i0 },
  { hs: true, key: "assists", label: "A", fmt: i0 },
  { hs: true, key: "points", label: "NHL Pts", fmt: i0 },
  { hs: true, key: "ppPoints", label: "PPP", fmt: i0 },
  { hs: true, key: "shots", label: "SOG", fmt: i0 },
  { hs: true, key: "hits", label: "HIT", fmt: i0 },
  { hs: true, key: "blockedShots", label: "BLK", fmt: i0 },
  { hs: true, key: "plusMinus", label: "+/-", fmt: pm },
  { hs: true, key: "fp", label: "FP", fmt: f1 },
  { key: "fpg", label: "FP/GP", fmt: f2, cls: "fpg" },
  { key: "split", sort: "fpgPeripheral", label: "Split", left: true, fmt: splitBar, tip: "Sort = peripheral FP/GP" },
  { hs: true, key: "periPct", label: "Peri %", fmt: v => i0(v) + "%", tip: "Share of FP from SOG/HIT/BLK - higher = steadier" },
];
const isF = p => p === "C" || p === "LW" || p === "RW";
const renderSk = makeTable(document.getElementById("skTable"), skCols, () => D.skaters.filter(p =>
  (skPos === "ALL" || p.pos === skPos || (skPos === "F" && isF(p.pos))) &&
  (!skQuery || p.name.toLowerCase().includes(skQuery) || p.team.toLowerCase().includes(skQuery))
), "fpg");
document.getElementById("skSearch").oninput = e => { skQuery = e.target.value.trim().toLowerCase(); renderSk(); };
document.querySelectorAll("#posSeg button").forEach(b => b.onclick = () => {
  skPos = b.dataset.pos;
  document.querySelectorAll("#posSeg button").forEach(x => x.classList.toggle("active", x === b));
  renderSk();
});

// ---- goalies ----
let gQuery = "";
const gCols = [
  { key: "rank", label: "#" },
  { key: "name", label: "Goalie", left: true, cls: "name", fmt: nameCell },
  { hs: true, key: "team", label: "Team", left: true },
  { hs: true, key: "gamesPlayed", label: "GP", fmt: i0 },
  { hs: true, key: "gamesStarted", label: "GS", fmt: i0 },
  { key: "wins", label: "W", fmt: i0 },
  { hs: true, key: "saves", label: "SV", fmt: i0 },
  { hs: true, key: "goalsAgainst", label: "GA", fmt: i0 },
  { key: "svPct", label: "SV%", fmt: f3 },
  { hs: true, key: "shutouts", label: "SHO", fmt: i0 },
  { hs: true, key: "saFor", label: "Shots/GP", fmt: f1, tip: "Shots faced per game - workload" },
  { hs: true, key: "fp", label: "FP", fmt: f1 },
  { key: "fpg", label: "FP/GP", fmt: f2, cls: "fpg" },
  { hs: true, key: "fpgs", label: "FP/Start", fmt: f2 },
  { key: "fpgNoWin", label: "No-win FP/GP", fmt: f2, tip: "Saves and GA only - his floor" },
];
const renderG = makeTable(document.getElementById("gTable"), gCols, () => D.goalies.filter(g =>
  !gQuery || g.name.toLowerCase().includes(gQuery) || g.team.toLowerCase().includes(gQuery)
), "fpg");
document.getElementById("gSearch").oninput = e => { gQuery = e.target.value.trim().toLowerCase(); renderG(); };

// ---- hidden value ----
const hvCols = [
  { key: "name", label: "Player", left: true, cls: "name", fmt: nameCell },
  { key: "pos", label: "Pos", left: true, fmt: posTag },
  { hs: true, key: "team", label: "Team", left: true },
  { key: "fpg", label: "FP/GP", fmt: (v, r) => `${f2(v)} <span class="muted">#${r.rank}</span>`, cls: "fpg" },
  { key: "ppg", label: "NHL P/GP", fmt: (v, r) => `${f2(v)} <span class="muted">#${r.ppgRank}</span>` },
  { key: "gap", label: "Gap", fmt: v => `<span class="${v > 0 ? "up" : "down"}">${v > 0 ? "+" : ""}${v}</span>`,
    tip: "Rank spots gained (or lost) using your scoring" },
  { hs: true, key: "periPct", label: "Peri %", fmt: v => i0(v) + "%" },
];
const byGap = D.skaters.slice().sort((a, b) => b.gap - a.gap);
makeTable(document.getElementById("hvUp"), hvCols, () => byGap.slice(0, 25), "gap");
makeTable(document.getElementById("hvDown"), hvCols, () => byGap.slice(-25), "gap", true);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def build(summary_rows, realtime_rows, goalie_rows, season_id, min_gp, out_dir, open_browser=True):
    skaters = score_skaters(merge_skaters(summary_rows, realtime_rows))
    goalies = score_goalies(merge_goalies(goalie_rows))
    q_sk, q_g = add_ranks(skaters, goalies, min_gp)

    os.makedirs(out_dir, exist_ok=True)
    write_csv(os.path.join(out_dir, "skaters.csv"), skaters, SKATER_CSV_COLS)
    write_csv(os.path.join(out_dir, "goalies.csv"), goalies, GOALIE_CSV_COLS)
    html_path = os.path.join(out_dir, "fp_rankings.html")
    generated = local_now().strftime("%b %d, %Y %I:%M %p")
    write_html(html_path, q_sk, q_g, season_id, min_gp, generated)

    # Quick console preview
    print(f"\nTop 15 skaters by FP/GP (min {min_gp} GP):")
    print(f"{'#':>3}  {'Player':<24}{'Pos':<4}{'Team':<9}{'GP':>4}{'FP':>8}{'FP/GP':>7}{'Peri%':>7}")
    for p in sorted(q_sk, key=lambda x: x["rank"])[:15]:
        print(f"{p['rank']:>3}  {p['name'][:23]:<24}{p['pos']:<4}{p['team'][:8]:<9}"
              f"{int(p['gamesPlayed']):>4}{p['fp']:>8.1f}{p['fpg']:>7.2f}{p['periPct']:>6.0f}%")
    print(f"\nTop 5 goalies by FP/GP:")
    for g in sorted(q_g, key=lambda x: x["rank"])[:5]:
        print(f"{g['rank']:>3}  {g['name'][:23]:<24}{g['team'][:8]:<9}"
              f"{int(g['gamesPlayed']):>4}{g['fp']:>8.1f}{g['fpg']:>7.2f}")

    print(f"\nSaved:\n  {os.path.abspath(html_path)}\n  {os.path.abspath(os.path.join(out_dir, 'skaters.csv'))}"
          f"\n  {os.path.abspath(os.path.join(out_dir, 'goalies.csv'))}")
    if open_browser:
        webbrowser.open("file://" + os.path.abspath(html_path))
    return q_sk, q_g


def main():
    ap = argparse.ArgumentParser(description="Rank NHL players by your Yahoo league's fantasy points.")
    ap.add_argument("--season", default=current_season_id(), help="Season id, e.g. 20262027")
    ap.add_argument("--min-gp", type=int, default=5, help="Minimum games played to be ranked (default 5)")
    ap.add_argument("--out", default="fp_output", help="Output folder (default fp_output)")
    ap.add_argument("--no-open", action="store_true", help="Don't open the report in a browser")
    args = ap.parse_args()

    print(f"Fetching {season_label(args.season)} regular-season stats from the NHL...")
    try:
        summary = fetch_report("skater/summary", args.season, "points")
        print(f"  skater summary: {len(summary)} rows")
        realtime = fetch_report("skater/realtime", args.season, "hits")
        print(f"  skater hits/blocks: {len(realtime)} rows")
        goalies = fetch_report("goalie/summary", args.season, "wins")
        print(f"  goalie summary: {len(goalies)} rows")
    except RuntimeError as e:
        print(f"\nERROR: {e}\nCheck your internet connection and try again.")
        sys.exit(1)

    if not summary:
        print("\nNo stats found for that season yet. Try --season with last season, e.g. --season 20252026")
        sys.exit(1)

    build(summary, realtime, goalies, args.season, args.min_gp, args.out, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
