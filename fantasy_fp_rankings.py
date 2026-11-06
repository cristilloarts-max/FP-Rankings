#!/usr/bin/env python3
"""
Fantasy FP Rankings
-------------------
Pulls NHL stats from the NHL's public APIs, scores every player with YOUR Yahoo
league's point values, projects the rest of the season, and builds a phone-friendly
web app with five pages:

  Pulse     - this week's schedule, stream targets, goalie starts, hot/cold, luck flags
  Rankings  - skaters, goalies and "Hidden Value" ranked by your scoring
  Compare   - up to 4 players side by side
  Trade     - mock trades with a verdict (value over replacement, schedule, stability)
  Guide     - what every number means

Output (in ./fp_output):
  fp_rankings.html, skaters.csv, goalies.csv

Run on a PC:   python fantasy_fp_rankings.py
Phone version: keep this file in your GitHub repo with
               .github/workflows/update-rankings.yml - it rebuilds every morning.

Optional flags:
  --season 20262027   pick a season (default: current season)
  --min-gp 5          minimum games played to appear in the rankings
  --out fp_output     output folder
  --no-open           don't auto-open the report in your browser

No extra packages needed - standard library only.
"""

import argparse
import csv
import datetime
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# ===========================================================================
# YOUR LEAGUE SETTINGS  (edit these if your league changes)
# ===========================================================================
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

LEAGUE_TEAMS = 12                                         # teams in your league
ROSTER_SLOTS = {"C": 2, "LW": 2, "RW": 2, "D": 4, "Util": 1, "G": 2}  # starting slots per team
BENCH_SLOTS = 4
IR_SLOTS = 3
SKATERS_ROSTERED_PER_TEAM = 14   # starters + bench skaters (used to guess who's a free agent)

# Yahoo positions and your roster live in this file next to the script (optional).
LEAGUE_FILE = "my_league.json"

# Fantasy playoff window. Leave as None to use the last 3 weeks of the NHL regular season,
# or set real dates like "2027-03-15" / "2027-04-04".
PLAYOFF_START = None
PLAYOFF_END = None
PLAYOFF_WEEKS = 3

# Projection tuning
RECENT_DAYS = 14          # "hot/cold" window
RECENT_WEIGHT = 0.20      # how much recent form moves the projection
PRIOR_GAMES = 15          # skaters: last season counts like this many games of this season
GOALIE_PRIOR_STARTS = 8   # goalies: same idea, in starts

SCORING_KEYS = ["goals", "assists", "ppPoints", "shGoals", "shAssists", "gameWinningGoals", "plusMinus"]
PERIPHERAL_KEYS = ["shots", "hits", "blockedShots"]

LEAGUE_LABELS = {
    "goals": "G", "assists": "A", "plusMinus": "+/-", "ppPoints": "PPP",
    "shGoals": "SHG", "shAssists": "SHA", "gameWinningGoals": "GWG",
    "shots": "SOG", "hits": "HIT", "blockedShots": "BLK",
    "wins": "W", "goalsAgainst": "GA", "saves": "SV", "shutouts": "SHO",
}

# ===========================================================================
# NHL APIs
# ===========================================================================
STATS_URL = "https://api.nhle.com/stats/rest/en"
WEB_URL = "https://api-web.nhle.com/v1"
PAGE_SIZE = 100
USER_AGENT = "personal-fantasy-fp-rankings/2.0"
FINISHED_STATES = {"OFF", "FINAL"}


def log(*args):
    print(*args, flush=True)


def local_now():
    """Toronto time, so dates are right even when GitHub runs this in UTC."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Toronto"))
    except Exception:
        return datetime.datetime.now()


def current_season_id(today=None):
    today = today or local_now().date()
    start_year = today.year if today.month >= 9 else today.year - 1
    return f"{start_year}{start_year + 1}"


def prev_season_id(season_id):
    start = int(str(season_id)[:4])
    return f"{start - 1}{start}"


def season_label(season_id):
    s = str(season_id)
    return f"{s[:4]}–{s[6:]}" if len(s) == 8 else s


def get_json(url, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not load {url}\n  Reason: {last_error}")


def fetch_report(path, cayenne, sort_prop, is_game=False, is_aggregate=False):
    """Fetch every row of an NHL stats report, page by page."""
    rows, start = [], 0
    while True:
        params = {
            "isAggregate": "true" if is_aggregate else "false",
            "isGame": "true" if is_game else "false",
            "start": start,
            "limit": PAGE_SIZE,
            "sort": json.dumps([
                {"property": sort_prop, "direction": "DESC"},
                {"property": "playerId", "direction": "ASC"},
            ]),
            "cayenneExp": cayenne,
        }
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        data = get_json(f"{STATS_URL}/{path}?{query}")
        batch = data.get("data", []) or []
        rows.extend(batch)
        total = data.get("total", len(rows))
        start += PAGE_SIZE
        if not batch or start >= total:
            break
        time.sleep(0.2)
    return rows


def season_exp(season_id):
    return f"seasonId={season_id} and gameTypeId=2"


def fetch_range(path, season_id, start, end, sort_prop):
    """Stats between two dates. Works whether the API returns totals or one row per game."""
    exp = (f'seasonId={season_id} and gameTypeId=2 and gameDate>="{start.isoformat()}" '
           f'and gameDate<="{end.isoformat()} 23:59:59"')
    try:
        return fetch_report(path, exp, sort_prop, is_game=True, is_aggregate=True)
    except RuntimeError:
        return fetch_report(path, exp, sort_prop, is_game=True, is_aggregate=False)


def optional(label, fn, *args, **kwargs):
    """Extra data is nice to have - if it fails, carry on without it."""
    try:
        rows = fn(*args, **kwargs)
        log(f"  {label}: {len(rows)} rows")
        return rows
    except RuntimeError as e:
        log(f"  {label}: skipped ({str(e).splitlines()[-1].strip()})")
        return []


def fetch_schedules(teams, season_id):
    out = {}
    for team in teams:
        try:
            data = get_json(f"{WEB_URL}/club-schedule-season/{team}/{season_id}", retries=2)
            games = []
            for g in data.get("games", []) or []:
                if g.get("gameType") != 2 or not g.get("gameDate"):
                    continue
                games.append({"date": g["gameDate"][:10], "state": g.get("gameState", "")})
            out[team] = games
        except RuntimeError:
            log(f"  schedule for {team}: skipped")
        time.sleep(0.15)
    log(f"  team schedules: {len(out)} of {len(teams)} teams")
    return out


def fetch_all(season_id, today):
    raw = {}
    log(f"Fetching {season_label(season_id)} stats from the NHL...")
    raw["summary"] = fetch_report("skater/summary", season_exp(season_id), "points")
    log(f"  skater summary: {len(raw['summary'])} rows")
    raw["realtime"] = fetch_report("skater/realtime", season_exp(season_id), "hits")
    log(f"  skater hits/blocks: {len(raw['realtime'])} rows")
    raw["goalies"] = fetch_report("goalie/summary", season_exp(season_id), "wins")
    log(f"  goalie summary: {len(raw['goalies'])} rows")

    prev = prev_season_id(season_id)
    log(f"Fetching last season ({season_label(prev)}) for projections...")
    raw["lastSummary"] = optional("last season skaters", fetch_report, "skater/summary", season_exp(prev), "points")
    raw["lastRealtime"] = optional("last season hits/blocks", fetch_report, "skater/realtime", season_exp(prev), "hits")
    raw["lastGoalies"] = optional("last season goalies", fetch_report, "goalie/summary", season_exp(prev), "wins")

    start = today - datetime.timedelta(days=RECENT_DAYS - 1)
    log(f"Fetching last {RECENT_DAYS} days ({start} to {today})...")
    raw["recentSummary"] = optional("recent skaters", fetch_range, "skater/summary", season_id, start, today, "points")
    raw["recentRealtime"] = optional("recent hits/blocks", fetch_range, "skater/realtime", season_id, start, today, "hits")
    raw["recentGoalies"] = optional("recent goalies", fetch_range, "goalie/summary", season_id, start, today, "wins")

    teams = set()
    for r in raw["summary"] + raw["goalies"]:
        teams.update(t.strip() for t in str(r.get("teamAbbrevs") or "").split(",") if t.strip())
    log("Fetching team schedules...")
    raw["schedules"] = fetch_schedules(sorted(teams), season_id)
    return raw


# ===========================================================================
# MERGING
# ===========================================================================
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
    """Sum rows by player (handles trades and per-game rows)."""
    players = {}
    for r in summary_rows:
        pid = r.get("playerId")
        if pid is None:
            continue
        p = players.setdefault(pid, {
            "playerId": pid,
            "name": r.get("skaterFullName") or r.get("lastName") or "Unknown",
            "pos": POS_NAMES.get(r.get("positionCode", ""), r.get("positionCode", "")),
            "teamList": [],
            **{f: 0.0 for f in SUMMARY_FIELDS + REALTIME_FIELDS},
        })
        for f in SUMMARY_FIELDS:
            p[f] += num(r.get(f))
        add_teams(p, r.get("teamAbbrevs") or r.get("teamAbbrev"))
    for r in realtime_rows:
        p = players.get(r.get("playerId"))
        if p:
            for f in REALTIME_FIELDS:
                p[f] += num(r.get(f))
    for p in players.values():
        p["shAssists"] = max(0.0, p["shPoints"] - p["shGoals"])
    return players


def merge_goalies(rows):
    goalies = {}
    for r in rows:
        pid = r.get("playerId")
        if pid is None:
            continue
        g = goalies.setdefault(pid, {
            "playerId": pid,
            "name": r.get("goalieFullName") or r.get("lastName") or "Unknown",
            "teamList": [],
            **{f: 0.0 for f in GOALIE_FIELDS},
        })
        for f in GOALIE_FIELDS:
            g[f] += num(r.get(f))
        add_teams(g, r.get("teamAbbrevs") or r.get("teamAbbrev"))
    return goalies


def skater_fp(p):
    scoring = sum(p.get(k, 0.0) * SKATER_SCORING[k] for k in SCORING_KEYS)
    peripheral = sum(p.get(k, 0.0) * SKATER_SCORING[k] for k in PERIPHERAL_KEYS)
    return scoring, peripheral


def goalie_fp(g):
    return (g.get("wins", 0) * GOALIE_SCORING["wins"] + g.get("goalsAgainst", 0) * GOALIE_SCORING["goalsAgainst"]
            + g.get("saves", 0) * GOALIE_SCORING["saves"] + g.get("shutouts", 0) * GOALIE_SCORING["shutouts"])


def div(a, b):
    return a / b if b else 0.0


# ===========================================================================
# SCHEDULE
# ===========================================================================
def analyze_schedule(schedules, today):
    D = datetime.timedelta
    mon = today - D(days=today.weekday())
    sun = mon + D(days=6)
    nmon, nsun = mon + D(days=7), sun + D(days=7)

    all_dates = [datetime.date.fromisoformat(g["date"]) for gs in schedules.values() for g in gs]
    if PLAYOFF_START and PLAYOFF_END:
        ps, pe = datetime.date.fromisoformat(PLAYOFF_START), datetime.date.fromisoformat(PLAYOFF_END)
    elif all_dates:
        pe = max(all_dates)
        ps = pe - D(days=pe.weekday()) - D(days=7 * (PLAYOFF_WEEKS - 1))
    else:
        ps = pe = None

    teams = {}
    for team, games in schedules.items():
        info = {"played": 0, "ros": 0, "wk": 0, "wkTotal": 0, "nxt": 0, "po": 0}
        for g in games:
            d = datetime.date.fromisoformat(g["date"])
            done = g["state"] in FINISHED_STATES
            if done:
                info["played"] += 1
            elif d >= today:
                info["ros"] += 1
                if d <= sun:
                    info["wk"] += 1
                if ps and ps <= d <= pe:
                    info["po"] += 1
            if mon <= d <= sun:
                info["wkTotal"] += 1
            if nmon <= d <= nsun:
                info["nxt"] += 1
        teams[team] = info

    week = {
        "today": today.isoformat(), "mon": mon.isoformat(), "sun": sun.isoformat(),
        "nmon": nmon.isoformat(), "nsun": nsun.isoformat(),
        "ps": ps.isoformat() if ps else None, "pe": pe.isoformat() if pe else None,
        "focus": "next" if today.weekday() == 6 else "this",
    }
    return teams, week


def team_info(teams, team, fallback_played):
    if team in teams:
        return teams[team]
    played = int(fallback_played)
    return {"played": played, "ros": max(0, 82 - played), "wk": None, "wkTotal": None, "nxt": None, "po": None}


# ===========================================================================
# PLAYERS + PROJECTIONS
# ===========================================================================
def current_team(recent, season):
    """Latest team: recent games first (catches trades), then the season row."""
    for src in (recent, season):
        if src and src.get("teamList"):
            return src["teamList"][-1]
    return ""


def rank(items, key, out_field, reverse=True):
    for i, item in enumerate(sorted(items, key=lambda x: x[key], reverse=reverse), start=1):
        item[out_field] = i


def build_skaters(raw, teams, min_gp):
    cur = merge_skaters(raw["summary"], raw["realtime"])
    last = merge_skaters(raw.get("lastSummary", []), raw.get("lastRealtime", []))
    rec = merge_skaters(raw.get("recentSummary", []), raw.get("recentRealtime", []))

    # League shooting % by forwards / defence, used when there's no history
    sh_league = {}
    for grp in ("F", "D"):
        pool = [p for p in cur.values() if (p["pos"] == "D") == (grp == "D")]
        sh_league[grp] = div(sum(p["goals"] for p in pool), sum(p["shots"] for p in pool)) or (0.05 if grp == "D" else 0.11)

    players = []
    for pid, p in cur.items():
        gp = p["gamesPlayed"]
        if gp <= 0:
            continue
        s, pe = skater_fp(p)
        team_now = current_team(rec.get(pid), p)
        out = {
            "id": pid, "name": p["name"], "team": team_now, "teams": "/".join(p["teamList"]), "pos": p["pos"],
            "gp": gp, "g": p["goals"], "a": p["assists"], "pts": p["points"], "ppp": p["ppPoints"],
            "shg": p["shGoals"], "sha": p["shAssists"], "gwg": p["gameWinningGoals"], "pm": p["plusMinus"],
            "sog": p["shots"], "hit": p["hits"], "blk": p["blockedShots"],
            "fp": s + pe, "fpS": s, "fpP": pe,
            "fpg": div(s + pe, gp), "fpgS": div(s, gp), "fpgP": div(pe, gp),
            "ppg": div(p["points"], gp),
            "peri": div(max(pe, 0), max(s, 0) + max(pe, 0)) * 100,
        }
        # Shooting luck
        grp = "D" if p["pos"] == "D" else "F"
        lp = last.get(pid)
        sh_base = div(lp["goals"], lp["shots"]) if lp and lp["shots"] >= 60 else sh_league[grp]
        out["sh"] = div(p["goals"], p["shots"]) * 100 if p["shots"] else None
        out["shBase"] = sh_base * 100
        out["shDiff"] = (out["sh"] - out["shBase"]) if p["shots"] >= 30 else None
        # Last season + recent form
        if lp and lp["gamesPlayed"] > 0:
            ls, lpe = skater_fp(lp)
            out["lastGp"], out["lastFpg"] = lp["gamesPlayed"], div(ls + lpe, lp["gamesPlayed"])
        else:
            out["lastGp"], out["lastFpg"] = 0, None
        rp = rec.get(pid)
        if rp and rp["gamesPlayed"] > 0:
            rs, rpe = skater_fp(rp)
            out["recGp"], out["recFpg"] = rp["gamesPlayed"], div(rs + rpe, rp["gamesPlayed"])
        else:
            out["recGp"], out["recFpg"] = 0, None
        out["q"] = gp >= min_gp
        players.append(out)

    # Baseline for players with little or no history: 85% of the median regular at their position
    pos_median = {}
    for pos in ("C", "LW", "RW", "D"):
        vals = [p["fpg"] for p in players if p["pos"] == pos and p["q"]]
        pos_median[pos] = statistics.median(vals) if vals else 2.0

    for p in players:
        baseline = 0.85 * pos_median.get(p["pos"], 2.0)
        if p["lastGp"] >= 20:
            prior = p["lastFpg"]
        elif p["lastGp"] > 0:
            prior = (p["lastGp"] * p["lastFpg"] + (20 - p["lastGp"]) * baseline) / 20
        else:
            prior = baseline
        base = (p["gp"] * p["fpg"] + PRIOR_GAMES * prior) / (p["gp"] + PRIOR_GAMES)
        proj = base
        if p["recGp"] >= 3 and p["recFpg"] is not None:
            proj = (1 - RECENT_WEIGHT) * base + RECENT_WEIGHT * p["recFpg"]
        p["prior"], p["proj"] = prior, proj

        t = team_info(teams, p["team"], p["gp"])
        p["ros"], p["wk"], p["nxt"], p["po"] = t["ros"], t["wk"], t["nxt"], t["po"]
        p["rosFp"] = proj * (t["ros"] or 0)
        p["wkFp"] = proj * (t["wk"] or 0)
        p["nxtFp"] = proj * (t["nxt"] or 0)
        p["poFp"] = proj * (t["po"] or 0)

    q = [p for p in players if p["q"]]
    rank(q, "fpg", "rank")
    rank(q, "proj", "projRank")
    rank(q, "ppg", "ppgRank")
    for p in q:
        p["gap"] = p["ppgRank"] - p["rank"]
    for pos in ("C", "LW", "RW", "D"):
        rank([p for p in q if p["pos"] == pos], "proj", "posRank")

    # Replacement level: fill every team's starting spots (including Util) with the best
    # projections, then the best player left at each position is what a free agent looks like.
    by_pos = {pos: sorted([p for p in q if p["pos"] == pos], key=lambda x: -x["proj"]) for pos in ("C", "LW", "RW", "D")}
    starters = set()
    for pos, pool in by_pos.items():
        starters.update(p["id"] for p in pool[:LEAGUE_TEAMS * ROSTER_SLOTS.get(pos, 2)])
    rest = sorted([p for p in q if p["id"] not in starters], key=lambda x: -x["proj"])
    starters.update(p["id"] for p in rest[:LEAGUE_TEAMS * ROSTER_SLOTS.get("Util", 0)])
    repl = {}
    for pos, pool in by_pos.items():
        left = [p for p in pool if p["id"] not in starters]
        repl[pos] = left[0]["proj"] if left else (pool[-1]["proj"] if pool else 0.0)
    for p in players:
        p["vor"] = p["proj"] - repl.get(p["pos"], 0.0)
        p["rosVor"] = p["vor"] * (p["ros"] or 0)
    return players, repl


def build_goalies(raw, teams, min_gp):
    cur = merge_goalies(raw["goalies"])
    last = merge_goalies(raw.get("lastGoalies", []))
    rec = merge_goalies(raw.get("recentGoalies", []))

    tot_fp = sum(goalie_fp(g) for g in cur.values())
    tot_gs = sum(g["gamesStarted"] for g in cur.values())
    if tot_gs >= 20:
        league_fps = tot_fp / tot_gs
    else:
        lgs = sum(g["gamesStarted"] for g in last.values())
        league_fps = div(sum(goalie_fp(g) for g in last.values()), lgs) if lgs else 4.0

    goalies = []
    for pid, g in cur.items():
        gp, gs = g["gamesPlayed"], g["gamesStarted"]
        if gp <= 0:
            continue
        fp = goalie_fp(g)
        team_now = current_team(rec.get(pid), g)
        noWin = g["saves"] * GOALIE_SCORING["saves"] + g["goalsAgainst"] * GOALIE_SCORING["goalsAgainst"]
        out = {
            "id": pid, "name": g["name"], "team": team_now, "teams": "/".join(g["teamList"]), "pos": "G",
            "gp": gp, "gs": gs, "w": g["wins"], "l": g["losses"], "otl": g["otLosses"],
            "sv": g["saves"], "ga": g["goalsAgainst"], "sa": g["shotsAgainst"], "so": g["shutouts"],
            "svPct": div(g["saves"], g["shotsAgainst"]), "saFor": div(g["shotsAgainst"], gp),
            "fp": fp, "fpg": div(fp, gp), "fpgs": div(fp, gs) if gs else None, "noWin": div(noWin, gp),
        }
        lg = last.get(pid)
        out["lastGs"] = lg["gamesStarted"] if lg else 0
        out["lastFpgs"] = div(goalie_fp(lg), lg["gamesStarted"]) if lg and lg["gamesStarted"] else None
        rg = rec.get(pid)
        out["recGs"] = rg["gamesStarted"] if rg else 0
        out["recFpgs"] = div(goalie_fp(rg), rg["gamesStarted"]) if rg and rg["gamesStarted"] else None

        baseline = 0.9 * league_fps
        if out["lastGs"] >= 10:
            prior = out["lastFpgs"]
        elif out["lastGs"] > 0:
            prior = (out["lastGs"] * out["lastFpgs"] + (10 - out["lastGs"]) * baseline) / 10
        else:
            prior = baseline
        base = (gs * (out["fpgs"] or 0) + GOALIE_PRIOR_STARTS * prior) / (gs + GOALIE_PRIOR_STARTS)
        proj = base
        if out["recGs"] >= 2 and out["recFpgs"] is not None:
            proj = (1 - RECENT_WEIGHT) * base + RECENT_WEIGHT * out["recFpgs"]

        t = team_info(teams, team_now, gp)
        last_share = min(0.9, out["lastGs"] / 82) if out["lastGs"] else 0.3
        share = (gs + 3 * last_share) / (t["played"] + 3) if t["played"] else last_share
        share = max(0.0, min(0.95, share))

        out.update({
            "prior": prior, "proj": proj, "share": share, "projTG": proj * share,
            "ros": t["ros"], "wk": t["wk"], "nxt": t["nxt"], "po": t["po"],
            "wkStarts": share * (t["wk"] or 0), "nxtStarts": share * (t["nxt"] or 0),
        })
        out["rosFp"] = proj * share * (t["ros"] or 0)
        out["wkFp"] = proj * out["wkStarts"]
        out["nxtFp"] = proj * out["nxtStarts"]
        out["poFp"] = proj * share * (t["po"] or 0)
        out["q"] = gs >= max(2, (min_gp + 1) // 2)
        goalies.append(out)

    q = [g for g in goalies if g["q"]]
    rank(q, "fpg", "rank")
    rank(q, "projTG", "projRank")
    pool = sorted((g["projTG"] for g in q), reverse=True)
    idx = LEAGUE_TEAMS * ROSTER_SLOTS.get("G", 2)
    repl = pool[min(idx, len(pool) - 1)] if pool else 0.0
    for g in goalies:
        g["vor"] = g["projTG"] - repl
        g["rosVor"] = g["vor"] * (g["ros"] or 0)
    return goalies, repl


def build_pulse(skaters, goalies, week):
    nxt = week["focus"] == "next"
    wk_key = "nxtFp" if nxt else "wkFp"
    games_key = "nxt" if nxt else "wk"
    cut = LEAGUE_TEAMS * SKATERS_ROSTERED_PER_TEAM

    form = [p for p in skaters if p["q"] and p["recGp"] >= 3 and p["gp"] >= 6 and p["recFpg"] is not None]
    hot = sorted(form, key=lambda p: p["recFpg"] - p["fpg"], reverse=True)[:10]
    cold = sorted(form, key=lambda p: p["recFpg"] - p["fpg"])[:10]
    lucky = [p for p in skaters if p["q"] and p["shDiff"] is not None]
    sell = sorted([p for p in lucky if p["shDiff"] >= 5], key=lambda p: -p["shDiff"])[:8]
    buy = sorted([p for p in lucky if p["shDiff"] <= -4], key=lambda p: p["shDiff"])[:8]
    stream = sorted([p for p in skaters if p["q"] and p.get("projRank", 0) > cut and (p[games_key] or 0) > 0],
                    key=lambda p: -p[wk_key])[:12]
    gweek = sorted([g for g in goalies if g["share"] >= 0.2 and (g[games_key] or 0) > 0],
                   key=lambda g: -g[wk_key])[:10]
    ids = lambda xs: [x["id"] for x in xs]
    return {"hot": ids(hot), "cold": ids(cold), "sell": ids(sell), "buy": ids(buy),
            "stream": ids(stream), "goalies": ids(gweek), "streamCut": cut}


# ===========================================================================
# OUTPUT
# ===========================================================================
SKATER_CSV_COLS = ["projRank", "rank", "posRank", "name", "team", "pos", "gp", "g", "a", "pts", "ppp", "shg", "sha",
                   "gwg", "pm", "sog", "hit", "blk", "fp", "fpg", "fpgS", "fpgP", "peri", "recGp", "recFpg",
                   "lastGp", "lastFpg", "proj", "ros", "rosFp", "vor", "rosVor", "wk", "wkFp", "nxt", "nxtFp",
                   "po", "poFp", "sh", "shBase", "shDiff", "ppg", "ppgRank", "gap", "id"]
GOALIE_CSV_COLS = ["projRank", "rank", "name", "team", "gp", "gs", "w", "l", "otl", "sv", "sa", "ga", "svPct", "so",
                   "fp", "fpg", "fpgs", "noWin", "saFor", "recGs", "recFpgs", "lastGs", "lastFpgs", "proj", "share",
                   "projTG", "ros", "rosFp", "vor", "rosVor", "wk", "wkStarts", "wkFp", "nxt", "nxtStarts",
                   "nxtFp", "po", "poFp", "id"]


def write_csv(path, rows, cols, sort_key):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(rows, key=lambda x: x.get(sort_key) or 0, reverse=True):
            w.writerow([round(r[c], 3) if isinstance(r.get(c), float) else r.get(c, "") for c in cols])


def slim(rows):
    return [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for r in rows]


def write_html(path, payload):
    data_json = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as f:
        f.write(HTML_TEMPLATE.replace("__DATA__", data_json))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#F2F6F9" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0C1626" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="FP Hockey">
<title>FP Hockey</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@100,400;100,500;100,600;100,700&display=swap">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;700;800&display=swap">
<style>
  :root{
    --ice:#F2F6F9; --surface:#FFFFFF; --ink:#0F1E33; --muted:#5D6D83; --faint:#93A1B4; --line:#DCE4EC;
    --blue:#1F5FD1; --blue-soft:#E3ECFB; --red:#D7263D; --red-soft:#FBE6E9; --gold:#E0A100;
    --pC:#1E6FD0; --pW:#11866A; --pD:#9A7000; --pG:#5B8A0E;
    --heat0:#E9EEF3; --heat1:#D6E2F4; --heat2:#A9C3EE; --heat3:#5F8FE0; --heat4:#1F5FD1;
    --shadow:0 18px 40px rgba(15,30,51,.18);
    --display:"Big Shoulders Display","Big Shoulders","Barlow Condensed","Arial Narrow",sans-serif;
    --body:"Archivo",system-ui,-apple-system,"Segoe UI",sans-serif;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --ice:#0C1626; --surface:#13213A; --ink:#E9F0F8; --muted:#97A6BC; --faint:#64748B; --line:#22324D;
      --blue:#5B8EF0; --blue-soft:#1A2D52; --red:#F0525F; --red-soft:#3A1C27; --gold:#F2BE3A;
      --pC:#5AA7F0; --pW:#4CC7A3; --pD:#E0B43A; --pG:#A8CF3F;
      --heat0:#16243D; --heat1:#1B3159; --heat2:#25467F; --heat3:#3767BE; --heat4:#5B8EF0;
      --shadow:0 18px 40px rgba(0,0,0,.5);
    }
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--ice);color:var(--ink);font:15px/1.5 var(--body);
    padding-bottom:calc(76px + env(safe-area-inset-bottom))}
  button,input,select{font:inherit;color:inherit}
  button{cursor:pointer}
  :focus-visible{outline:2px solid var(--blue);outline-offset:2px;border-radius:6px}
  .wrap{max-width:760px;margin:0 auto;padding:0 18px}

  /* top bar */
  .top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
    padding:calc(14px + env(safe-area-inset-top)) 18px 6px;max-width:760px;margin:0 auto}
  .brand{font:800 22px/1 var(--display);letter-spacing:.01em}
  .brand i{font-style:normal;color:var(--red)}
  .upd{font-size:12px;color:var(--muted)}

  /* bottom nav (top on wide screens) */
  nav.tabs{position:fixed;left:0;right:0;bottom:0;z-index:30;display:flex;justify-content:center;
    background:color-mix(in srgb,var(--surface) 94%,transparent);backdrop-filter:blur(12px);
    border-top:1px solid var(--line);padding:4px 4px calc(4px + env(safe-area-inset-bottom))}
  nav.tabs button{flex:1;max-width:120px;background:none;border:0;padding:6px 0 4px;display:flex;flex-direction:column;
    align-items:center;gap:2px;font-size:11px;font-weight:600;color:var(--faint)}
  nav.tabs svg{width:24px;height:24px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
  nav.tabs button[aria-current="page"]{color:var(--blue)}

  /* type */
  h1{font:800 44px/1 var(--display);margin:14px 0 6px;letter-spacing:.005em}
  h2{font:700 24px/1.1 var(--display);margin:0;letter-spacing:.01em}
  .intro{color:var(--muted);margin:0 0 18px;max-width:60ch}
  .hint{color:var(--muted);font-size:13px;margin:4px 0 10px;max-width:60ch}

  /* sections divided by rink lines */
  .block{padding:18px 0 14px;border-top:2px solid var(--line)}
  .block.redline{border-top-color:var(--red)}
  .block.blueline{border-top-color:var(--blue)}
  .bh{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}

  /* briefing */
  .brief{list-style:none;margin:0 0 22px;padding:0;display:grid;gap:10px}
  .brief li{background:var(--surface);border-radius:14px;padding:12px 14px;display:flex;gap:12px;align-items:flex-start}
  .brief .ic{flex:none;width:28px;height:28px;border-radius:50%;display:grid;place-items:center;font:800 15px/1 var(--display)}
  .brief .ic.b{background:var(--blue-soft);color:var(--blue)} .brief .ic.r{background:var(--red-soft);color:var(--red)}
  .brief .ic.g{background:color-mix(in srgb,var(--gold) 22%,transparent);color:var(--gold)}
  .brief button.lnk{background:none;border:0;padding:0;font-weight:700;color:var(--ink);text-decoration:underline;
    text-decoration-color:var(--faint);text-underline-offset:3px}

  /* segmented */
  .seg{display:inline-flex;background:var(--heat0);border-radius:10px;padding:3px}
  .seg button{border:0;background:none;padding:6px 12px;border-radius:8px;font-size:13px;font-weight:600;color:var(--muted)}
  .seg button[aria-pressed="true"]{background:var(--surface);color:var(--ink);box-shadow:0 1px 3px rgba(15,30,51,.15)}
  .seg.full{display:flex} .seg.full button{flex:1}

  /* schedule heat grid */
  .teamgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(68px,1fr));gap:6px;margin-top:6px}
  .tile{border-radius:10px;padding:7px 8px 6px;display:flex;justify-content:space-between;align-items:baseline;background:var(--heat0)}
  .tile b{font:800 26px/1 var(--display)}
  .tile span{font-size:11px;font-weight:700;letter-spacing:.02em}
  .tile.h1{background:var(--heat1)} .tile.h2{background:var(--heat2)}
  .tile.h3{background:var(--heat3);color:#fff} .tile.h4{background:var(--heat4);color:#fff}
  .tile.h0{color:var(--faint)}
  .scale{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--muted);margin-top:10px}
  .scale i{display:inline-block;width:14px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}

  /* player rows */
  .list{background:var(--surface);border-radius:16px;overflow:hidden}
  .row{display:flex;align-items:center;gap:12px;width:100%;text-align:left;background:none;border:0;
    padding:11px 14px;border-top:1px solid var(--line)}
  .row:first-child{border-top:0}
  .row:active{background:var(--heat0)}
  .rk{flex:none;width:30px;font:700 20px/1 var(--display);color:var(--faint);text-align:right}
  .who{flex:1;min-width:0}
  .who b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .meta{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted);margin-top:1px}
  .tm{font-size:10.5px;font-weight:700;letter-spacing:.03em;padding:1px 5px;border-radius:4px;background:var(--heat0);color:var(--ink)}
  .val{text-align:right;flex:none}
  .val b{display:block;font:800 24px/1 var(--display)}
  .val small{display:block;font-size:11.5px;color:var(--muted);margin-top:2px}
  .val .up{color:var(--red)} .val .dn{color:var(--blue)}
  .more{display:block;width:100%;margin-top:8px;background:none;border:0;padding:10px;font-weight:600;color:var(--blue);font-size:14px}
  .empty{padding:16px 14px;color:var(--muted);font-size:14px}

  .split{display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--heat0);width:72px;margin:5px 0 0 auto}
  .split .s{background:var(--blue)} .split .p{background:var(--gold)}

  /* controls */
  .controls{display:flex;flex-direction:column;gap:10px;margin:4px 0 12px}
  .search{position:relative}
  .search svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);width:18px;height:18px;stroke:var(--faint);fill:none;stroke-width:2}
  .search input{width:100%;border:1px solid var(--line);background:var(--surface);border-radius:12px;padding:11px 12px 11px 38px;font-size:16px;outline:none}
  .search input:focus{border-color:var(--blue)}
  .chips{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding-bottom:2px}
  .chips::-webkit-scrollbar{display:none}
  .chip{flex:none;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:6px 13px;font-size:13px;font-weight:600;color:var(--muted)}
  .chip[aria-pressed="true"]{background:var(--ink);border-color:var(--ink);color:var(--ice)}
  .sortrow{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:13px;color:var(--muted)}
  select{border:1px solid var(--line);background:var(--surface);border-radius:10px;padding:7px 10px;font-size:14px;font-weight:600}

  /* suggestions */
  .sugg{position:absolute;z-index:25;left:0;right:0;top:calc(100% + 6px);background:var(--surface);border-radius:14px;
    box-shadow:var(--shadow);overflow:hidden}
  .sugg .row{padding:10px 14px}

  /* compare */
  .chead,.mv{display:grid;gap:8px}
  .chead{margin:6px 0 4px}
  .pcol{background:var(--surface);border-radius:14px;padding:10px 10px 8px;position:relative;min-width:0}
  .pcol b{display:block;font:700 19px/1.05 var(--display);overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .pcol .meta{margin-top:5px}
  .pcol .x{position:absolute;top:6px;right:6px;border:0;background:none;color:var(--faint);font-size:16px;line-height:1;padding:4px}
  .mgroup{margin-top:18px}
  .mgroup h3{font:700 18px/1 var(--display);margin:0 0 6px;color:var(--muted)}
  .mrow{background:var(--surface);padding:10px 12px 12px;border-top:1px solid var(--line)}
  .mgroup .mrow:first-of-type{border-radius:14px 14px 0 0;border-top:0}
  .mgroup .mrow:last-child{border-radius:0 0 14px 14px}
  .mgroup .mrow:only-of-type{border-radius:14px}
  .ml{font-size:12.5px;color:var(--muted);margin-bottom:5px}
  .mc{min-width:0}
  .mc b{font:700 22px/1 var(--display)}
  .mc.best b{color:var(--blue)}
  .mc .bar{height:4px;border-radius:2px;background:var(--heat0);margin-top:6px;overflow:hidden}
  .mc .bar i{display:block;height:100%;background:var(--faint)}
  .mc.best .bar i{background:var(--blue)}
  .mc small{display:block;font-size:11px;color:var(--muted)}

  /* trade */
  .sides{display:grid;gap:12px}
  .side h2{font-size:20px;margin-bottom:8px}
  .side .addwrap{position:relative;margin-top:8px}
  .rm{flex:none;border:0;background:var(--heat0);width:28px;height:28px;border-radius:50%;color:var(--muted);font-size:13px}
  .verdict{margin-top:20px;background:var(--surface);border-radius:20px;padding:18px 16px 16px}
  .rink{width:100%;height:auto;display:block;margin:4px 0 2px}
  .rink .puck{transition:transform .6s cubic-bezier(.2,.9,.25,1)}
  .ends{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);padding:0 4px}
  .vh{font:800 34px/1.02 var(--display);margin:14px 0 4px}
  .vh.win{color:var(--blue)} .vh.lose{color:var(--red)}
  .vs{color:var(--muted);margin:0 0 10px}
  .facts{margin:12px 0 0;padding:0;list-style:none;display:grid;gap:8px}
  .facts li{padding-left:16px;position:relative;font-size:14px}
  .facts li::before{content:"";position:absolute;left:2px;top:.6em;width:6px;height:6px;border-radius:50%;background:var(--faint)}
  details.more-d{margin-top:14px;border-top:1px solid var(--line);padding-top:10px}
  details.more-d summary{font-weight:600;color:var(--blue);cursor:pointer;list-style:none}
  details.more-d summary::-webkit-details-marker{display:none}
  .dt{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px;font-variant-numeric:tabular-nums}
  .dt td,.dt th{padding:7px 4px;border-top:1px solid var(--line);text-align:right}
  .dt th{font-weight:600;color:var(--muted);font-size:12px;border-top:0}
  .dt td:first-child,.dt th:first-child{text-align:left;color:var(--muted)}
  .actions{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
  .btn{border:1px solid var(--line);background:var(--surface);border-radius:12px;padding:10px 14px;font-weight:600;font-size:14px}
  .btn.primary{background:var(--blue);border-color:var(--blue);color:#fff}
  .btn.ghost{background:none}

  /* player sheet */
  .scrim{position:fixed;inset:0;background:rgba(8,16,30,.45);z-index:40;opacity:0;pointer-events:none;transition:opacity .2s}
  .scrim.open{opacity:1;pointer-events:auto}
  .sheet{position:fixed;left:0;right:0;bottom:0;z-index:41;background:var(--ice);border-radius:22px 22px 0 0;
    max-height:88vh;overflow-y:auto;transform:translateY(102%);transition:transform .28s cubic-bezier(.2,.9,.25,1);
    padding:8px 18px calc(18px + env(safe-area-inset-bottom));box-shadow:var(--shadow)}
  .sheet.open{transform:none}
  .grab{width:40px;height:5px;border-radius:3px;background:var(--line);margin:4px auto 10px}
  .sheet .close{position:absolute;right:12px;top:12px;border:0;background:var(--heat0);width:32px;height:32px;border-radius:50%;color:var(--muted)}
  .sheet h2{font-size:34px;line-height:1;margin:6px 40px 6px 0}
  .trio{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:14px 0}
  .trio div{background:var(--surface);border-radius:14px;padding:10px 12px}
  .trio b{display:block;font:800 30px/1 var(--display)}
  .trio span{font-size:12px;color:var(--muted)}
  .trio .lead b{color:var(--blue)}
  .kv{background:var(--surface);border-radius:14px;overflow:hidden;margin-top:10px}
  .kv > div{display:flex;justify-content:space-between;gap:10px;padding:10px 14px;border-top:1px solid var(--line);font-size:14px}
  .kv > div:first-child{border-top:0}
  .kv span{color:var(--muted)}
  .kv b{font-weight:600;text-align:right}
  .bigsplit{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--heat0);margin:8px 0 4px}
  .bigsplit .s{background:var(--blue)} .bigsplit .p{background:var(--gold)}
  .note{background:var(--surface);border-left:3px solid var(--red);border-radius:6px 14px 14px 6px;padding:10px 12px;margin-top:10px;font-size:14px}
  .note.cool{border-left-color:var(--blue)}
  .sheet .actions .btn{flex:1}

  /* guide */
  .guide details{background:var(--surface);border-radius:14px;margin-bottom:8px}
  .guide summary{padding:14px 16px;font:700 20px/1.1 var(--display);cursor:pointer;list-style:none;display:flex;justify-content:space-between}
  .guide summary::-webkit-details-marker{display:none}
  .guide summary::after{content:"+";color:var(--faint);font-weight:400}
  .guide details[open] summary::after{content:"\2013"}
  .guide .gb{padding:0 16px 14px}
  .guide .gb p,.guide .gb li{max-width:62ch}
  .guide dt{font-weight:700;margin-top:12px}
  .guide dd{margin:2px 0 0;color:var(--muted)}
  .pts{display:flex;flex-wrap:wrap;gap:6px}
  .pts span{background:var(--heat0);border-radius:8px;padding:4px 9px;font-size:13px}
  .pts b{font-weight:700}
  .pts .neg b{color:var(--red)}
  .formula{background:var(--heat0);border-radius:10px;padding:10px 12px;font-size:14px}

  .toast{position:fixed;left:50%;bottom:calc(86px + env(safe-area-inset-bottom));z-index:50;transform:translate(-50%,12px);opacity:0;
    background:var(--ink);color:var(--ice);padding:10px 16px;border-radius:12px;font-weight:600;font-size:14px;
    transition:all .2s;pointer-events:none;white-space:nowrap}
  .toast.show{opacity:1;transform:translate(-50%,0)}
  /* header help */
  .tr{display:flex;align-items:center;gap:10px}
  .help{border:1px solid var(--line);background:var(--surface);width:30px;height:30px;border-radius:50%;font-weight:700;color:var(--muted);font-size:14px}
  /* my team */
  .tsum{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:16px 0 4px}
  .tsum div{background:var(--surface);border-radius:14px;padding:10px 12px}
  .tsum b{display:block;font:800 30px/1 var(--display)}
  .tsum div:first-child b{color:var(--blue)}
  .tsum span{font-size:12px;color:var(--muted);line-height:1.3;display:block;margin-top:2px}
  .sub2{font-size:20px;margin:22px 0 8px;display:flex;align-items:baseline;gap:8px}
  .sub2 .cnt{font:500 13px/1 var(--body);color:var(--muted)}
  .slot{flex:none;width:38px;text-align:center;font-size:11px;font-weight:700;letter-spacing:.02em;padding:4px 0;border-radius:6px;background:var(--heat0);color:var(--muted)}
  .empty-slot .slot{background:var(--red-soft);color:var(--red)}
  .shape{background:var(--surface);border-radius:16px;padding:12px 14px}
  .sh-row{display:grid;grid-template-columns:38px 1fr 40px 62px;gap:10px;align-items:center;padding:7px 0}
  .sh-l{font-size:12px;font-weight:700;color:var(--muted)}
  .sh-bar{position:relative;height:10px;border-radius:5px;background:var(--heat0)}
  .sh-bar i{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:var(--faint)}
  .sh-bar i.strong{background:var(--blue)} .sh-bar i.weak{background:var(--red)}
  .sh-bar em{position:absolute;top:-4px;width:2px;height:18px;background:var(--ink);transform:translateX(-1px);opacity:.7}
  .sh-v{font:700 18px/1 var(--display);text-align:right}
  .sh-tag{font-size:11.5px;font-weight:600;color:var(--muted);text-align:right}
  .sh-tag.strong{color:var(--blue)} .sh-tag.weak,.sh-tag.empty{color:var(--red)}
  .pc{list-style:none;margin:0;padding:0;display:grid;gap:8px}
  .pc li{background:var(--surface);border-radius:12px;padding:11px 14px 11px 16px;border-left:3px solid var(--blue);font-size:14px}
  .pc.con li{border-left-color:var(--red)}
  .pc .lnk,.why .lnk{background:none;border:0;padding:0;font-weight:700;text-decoration:underline;text-decoration-color:var(--faint);text-underline-offset:3px}
  .move{background:var(--surface);border-radius:18px;padding:14px;margin-top:12px}
  .mtop{display:flex;align-items:baseline;gap:8px}
  .mtop b{font:800 30px/1 var(--display);color:var(--blue)}
  .mtop span{font-size:13px;color:var(--muted)}
  .swap{display:grid;grid-template-columns:1fr 22px 1fr;gap:8px;align-items:center;margin-top:10px}
  .swap svg{width:22px;height:22px;stroke:var(--faint);fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .mside{border:0;border-radius:12px;padding:9px 10px;text-align:left;min-width:0;background:var(--heat0)}
  .mside.in{background:var(--blue-soft)}
  .mside small{display:block;font-size:11px;color:var(--muted)}
  .mside b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .mside .tm{background:var(--surface)}
  .why{font-size:14px;margin:10px 0 0;color:var(--ink)}
  .mact{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
  .mact .btn{padding:8px 12px;font-size:13px}
  /* Yahoo-style position colours */
  .ps{font-weight:700;font-size:12px;letter-spacing:.01em}
  .ps i{font-style:normal}
  .ps .C{color:var(--pC)} .ps .LW,.ps .RW{color:var(--pW)} .ps .D{color:var(--pD)} .ps .G{color:var(--pG)}
  .slot{background:none !important;border:2px solid var(--line);border-radius:999px;color:var(--muted)}
  .slot.s-C{border-color:var(--pC);color:var(--pC)} .slot.s-LW,.slot.s-RW{border-color:var(--pW);color:var(--pW)}
  .slot.s-D{border-color:var(--pD);color:var(--pD)} .slot.s-G{border-color:var(--pG);color:var(--pG)}
  .slot.s-Util{border-color:var(--ink);color:var(--ink)}
  .empty-slot .slot{border-style:dashed}
  .chip.pc-C[aria-pressed="true"]{background:var(--pC);border-color:var(--pC);color:#fff}
  .chip.pc-LW[aria-pressed="true"],.chip.pc-RW[aria-pressed="true"]{background:var(--pW);border-color:var(--pW);color:#fff}
  .chip.pc-D[aria-pressed="true"]{background:var(--pD);border-color:var(--pD);color:#fff}
  [hidden]{display:none !important}

  @media (min-width:900px){
    body{padding-bottom:40px}
    nav.tabs{position:sticky;top:0;bottom:auto;border-top:0;border-bottom:1px solid var(--line);padding:0}
    nav.tabs button{flex-direction:row;gap:8px;font-size:14px;padding:12px 14px;max-width:none;flex:none}
    nav.tabs svg{width:20px;height:20px}
    .sides{grid-template-columns:1fr 1fr}
    .sheet{left:50%;right:auto;bottom:auto;top:50%;width:440px;border-radius:22px;transform:translate(-50%,-46%) scale(.98);
      opacity:0;pointer-events:none;transition:all .2s}
    .sheet.open{transform:translate(-50%,-50%);opacity:1;pointer-events:auto}
    .grab{display:none}
    .toast{bottom:28px}
  }
  @media (prefers-reduced-motion: reduce){ *{transition:none !important} }
</style>
</head>
<body>
<header class="top">
  <div class="brand">FP Hockey</div>
  <div class="tr"><span class="upd" id="upd"></span><button class="help" data-go="guide" aria-label="Open the guide">?</button></div>
</header>
<nav class="tabs" aria-label="Pages">
  <button data-v="week"><svg viewBox="0 0 24 24"><rect x="3.5" y="5" width="17" height="15" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>This week</button>
  <button data-v="team"><svg viewBox="0 0 24 24"><path d="M8 4l-4.5 3 2 4L7 10v10h10V10l1.5 1 2-4L16 4c-.8 1.4-2.2 2.2-4 2.2S8.8 5.4 8 4z"/></svg>My team</button>
  <button data-v="rank"><svg viewBox="0 0 24 24"><path d="M5 20V11M12 20V5M19 20v-6"/></svg>Rankings</button>
  <button data-v="compare"><svg viewBox="0 0 24 24"><rect x="3.5" y="4" width="7" height="16" rx="2"/><rect x="13.5" y="4" width="7" height="16" rx="2"/></svg>Compare</button>
  <button data-v="trade"><svg viewBox="0 0 24 24"><path d="M4 8h14l-3.5-3.5M20 16H6l3.5 3.5"/></svg>Trade</button>
</nav>

<main class="wrap">
  <section id="v-week"></section>
  <section id="v-team" hidden></section>
  <section id="v-rank" hidden></section>
  <section id="v-compare" hidden></section>
  <section id="v-trade" hidden></section>
  <section id="v-guide" hidden></section>
</main>



<div class="scrim" id="scrim"></div>
<div class="sheet" id="sheet" role="dialog" aria-modal="true" aria-labelledby="sheetName"></div>
<div class="toast" id="toast" role="status"></div>

<script>
const D = __DATA__;
const C = D.config;
const SK = D.skaters, GO = D.goalies;
const P = new Map([...SK, ...GO].map(p => [p.id, p]));
const isG = p => p.pos === "G";
const projTG = p => isG(p) ? p.projTG : p.proj;

// ---------- formatting ----------
const nz = v => v === null || v === undefined || Number.isNaN(v);
const DASH = "–";
const f0 = v => nz(v) ? DASH : Math.round(v).toLocaleString();
const f1 = v => nz(v) ? DASH : Number(v).toFixed(1);
const f2 = v => nz(v) ? DASH : Number(v).toFixed(2);
const f3 = v => nz(v) ? DASH : Number(v).toFixed(3).replace(/^0/, "");
const pct = v => nz(v) ? DASH : Math.round(v) + "%";
const sgn = (v, f = f1) => nz(v) ? DASH : (v > 0 ? "+" : v < 0 ? "−" : "") + f(Math.abs(v));
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const dt = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const fd = s => s ? dt(s).toLocaleDateString("en-CA", { month: "short", day: "numeric" }) : DASH;
const fday = s => s ? dt(s).toLocaleDateString("en-CA", { day: "numeric" }) : DASH;
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const words = ["zero","one","two","three","four","five","six","seven"];
const store = {
  get(k, d) { try { const v = localStorage.getItem("fp:" + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("fp:" + k, JSON.stringify(v)); } catch (e) {} },
};
let toastT;
function toast(msg) {
  const t = document.getElementById("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 1700);
}
document.getElementById("upd").textContent = "Updated " + D.generated.replace(/, \d{4}/, ",").replace(/^(\w+) 0/, "$1 ").replace(/ 0(\d:)/, " $1");

let elig = store.get("elig", {});
const eligOf = p => isG(p) ? ["G"] : (elig[p.id] && elig[p.id].length ? elig[p.id] : (p.elig && p.elig.length ? p.elig : [p.pos]));
const posTags = p => `<span class="ps">${eligOf(p).map(x => `<i class="${x}">${x}</i>`).join(",")}</span>`;
const meta = p => `<span class="meta"><span class="tm">${esc(p.team)}</span>${posTags(p)}</span>`;
const maxSplit = Math.max(0.01, ...SK.filter(p => p.q).map(p => Math.max(0, p.fpgS) + Math.max(0, p.fpgP)));
const split = p => isG(p) ? "" : `<div class="split" aria-hidden="true"><div class="s" style="width:${Math.max(0, p.fpgS) / maxSplit * 100}%"></div><div class="p" style="width:${Math.max(0, p.fpgP) / maxSplit * 100}%"></div></div>`;
function row(p, value, sub, rank, extra = "") {
  return `<button class="row" data-pid="${p.id}">${rank !== undefined ? `<span class="rk">${rank}</span>` : ""}
    <span class="who"><b>${esc(p.name)}</b>${meta(p)}</span>
    <span class="val">${value}${sub ? `<small>${sub}</small>` : ""}${extra}</span></button>`;
}
const listHTML = (items, fn, empty) => items.length ? `<div class="list">${items.map(fn).join("")}</div>` : `<div class="list"><div class="empty">${empty}</div></div>`;
function seg(name, options, current, full) {
  return `<div class="seg${full ? " full" : ""}" role="group">${options.map(([v, l]) =>
    `<button data-seg="${name}" data-val="${v}" aria-pressed="${v === current}">${l}</button>`).join("")}</div>`;
}

// ---------- navigation ----------
const VIEWS = ["week", "team", "rank", "compare", "trade", "guide"];
function go(v) {
  VIEWS.forEach(x => document.getElementById("v-" + x).hidden = x !== v);
  document.querySelectorAll("nav.tabs button").forEach(b => b.toggleAttribute("aria-current", b.dataset.v === v));
  document.querySelectorAll("nav.tabs button").forEach(b => { if (b.dataset.v === v) b.setAttribute("aria-current", "page"); });
  store.set("view", v); window.scrollTo(0, 0);
}
document.querySelectorAll("nav.tabs button").forEach(b => b.onclick = () => go(b.dataset.v));
document.addEventListener("click", e => { const g = e.target.closest("[data-go]"); if (g) go(g.dataset.go); });

// ---------- player sheet ----------
const sheet = document.getElementById("sheet"), scrim = document.getElementById("scrim");
let lastFocus = null;
function openSheet(id) {
  const p = P.get(id); if (!p) return;
  lastFocus = document.activeElement;
  sheet.innerHTML = sheetHTML(p);
  sheet.classList.add("open"); scrim.classList.add("open");
  sheet.scrollTop = 0;
  setTimeout(() => sheet.querySelector(".close").focus(), 50);
}
function closeSheet() {
  sheet.classList.remove("open"); scrim.classList.remove("open");
  if (lastFocus) lastFocus.focus();
}
scrim.onclick = closeSheet;
document.addEventListener("keydown", e => { if (e.key === "Escape") closeSheet(); });
sheet.addEventListener("click", e => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const id = +b.dataset.id;
  if (b.dataset.act === "close") return closeSheet();
  if (b.dataset.act === "compare") { addCompare(id); closeSheet(); }
  if (b.dataset.act === "give" || b.dataset.act === "get") { addTrade(b.dataset.act, id); closeSheet(); }
  if (b.dataset.act === "team-add") { addToTeam(id); closeSheet(); }
  if (b.dataset.act === "team-rm") { removeFromTeam(id); closeSheet(); }
  if (b.dataset.act === "ir") { toggleIR(id); closeSheet(); }
  if (b.dataset.act === "elig") { toggleElig(id, b.dataset.pos); sheet.innerHTML = sheetHTML(P.get(id)); }
});
document.addEventListener("click", e => {
  const r = e.target.closest("[data-pid]");
  if (r && !r.closest(".sugg")) openSheet(+r.dataset.pid);
});

function sheetHTML(p) {
  const g = isG(p);
  const trio = g
    ? [["Projected per start", f2(p.proj), true], ["Season per game", f2(p.fpg)], [`Last ${C.recentDays} days`, p.recFpgs === null ? DASH : f2(p.recFpgs)]]
    : [["Projected per game", f2(p.proj), true], ["Season per game", f2(p.fpg)], [`Last ${C.recentDays} days`, p.recFpg === null ? DASH : f2(p.recFpg)]];
  let body = `<div class="trio">${trio.map(([l, v, lead]) => `<div class="${lead ? "lead" : ""}"><b>${v}</b><span>${l}</span></div>`).join("")}</div>`;
  if (!g) {
    const tot = Math.max(0, p.fpgS) + Math.max(0, p.fpgP) || 1;
    body += `<div class="kv"><div style="display:block">
      <span>Where his points come from</span>
      <div class="bigsplit"><div class="s" style="width:${Math.max(0, p.fpgS) / tot * 100}%"></div><div class="p" style="width:${Math.max(0, p.fpgP) / tot * 100}%"></div></div>
      <span style="font-size:13px">${pct(100 - p.peri)} goals and assists, ${pct(p.peri)} shots, hits and blocks. ${p.peri >= 40 ? "That's a steady floor." : p.peri < 20 ? "Streaky: it rides on scoring." : ""}</span></div></div>`;
  } else {
    body += `<div class="kv"><div><span>Starts his team's games</span><b>${pct(p.share * 100)}</b></div>
      <div><span>Record</span><b>${f0(p.w)} W, ${f0(p.l + p.otl)} L</b></div>
      <div><span>Save %</span><b>${f3(p.svPct)}</b></div>
      <div><span>Shots faced per game</span><b>${f1(p.saFor)}</b></div>
      <div><span>Points per game even when he loses</span><b>${f2(p.noWin)}</b></div></div>`;
  }
  const wkLine = g ? `${f1(p.wkStarts)} starts, ${f1(p.wkFp)} FP` : `${plural(p.wk ?? 0, "game")}, ${f1(p.wkFp)} FP`;
  const nxLine = g ? `${f1(p.nxtStarts)} starts, ${f1(p.nxtFp)} FP` : `${plural(p.nxt ?? 0, "game")}, ${f1(p.nxtFp)} FP`;
  body += `<div class="kv">
    <div><span>Rest of this week</span><b>${p.wk === null ? DASH : wkLine}</b></div>
    <div><span>Next week</span><b>${p.nxt === null ? DASH : nxLine}</b></div>
    <div><span>Rest of season</span><b>${f0(p.rosFp)} FP</b></div>
    <div><span>Value over a free agent</span><b>${sgn(p.rosVor, f0)} FP</b></div>
    ${D.week.ps ? `<div><span>Your playoff weeks</span><b>${f0(p.poFp)} FP</b></div>` : ""}
    <div><span>Season stats</span><b>${g ? `${f0(p.gp)} GP, ${f0(p.sv)} saves, ${f0(p.so)} SO` : `${f0(p.gp)} GP, ${f0(p.g)} G, ${f0(p.a)} A, ${f0(p.hit)} HIT, ${f0(p.blk)} BLK`}</b></div>
  </div>`;
  if (!g && !nz(p.shDiff) && p.shDiff >= 5)
    body += `<div class="note">Scoring on ${f1(p.sh)}% of his shots, well above his normal ${f1(p.shBase)}%. Goals will probably slow down, so this is a good time to sell.</div>`;
  if (!g && !nz(p.shDiff) && p.shDiff <= -4)
    body += `<div class="note cool">Scoring on only ${f1(p.sh)}% of his shots, below his normal ${f1(p.shBase)}%. If the shots keep coming, goals should follow.</div>`;
  const rk = g ? (p.projRank ? `#${p.projRank} goalie` : "") : (p.posRank ? `#${p.posRank} ${p.pos}` : "");
  return `<div class="grab"></div><button class="close" data-act="close" aria-label="Close">✕</button>
    <h2 id="sheetName">${esc(p.name)}</h2>
    <div class="meta" style="font-size:13px"><span class="tm">${esc(p.team)}</span>${posTags(p)}${rk ? `<span>${rk}</span>` : ""}</div>
    ${body}
    ${!g ? `<div class="kv"><div style="display:block"><span>Yahoo positions</span>
      <div class="chips" style="margin-top:8px">${["C", "LW", "RW", "D"].map(x => `<button class="chip pc-${x}" data-act="elig" data-id="${p.id}" data-pos="${x}" aria-pressed="${eligOf(p).includes(x)}">${x}</button>`).join("")}</div>
      <span style="font-size:12px">${elig[p.id] ? "Changed on this device." : p.elig ? "From your league file." : "From his NHL position."} Tap to match Yahoo if it's different.</span></div></div>` : ""}
    <div class="actions">
      <button class="btn primary" data-act="compare" data-id="${p.id}">Compare</button>
      <button class="btn" data-act="give" data-id="${p.id}">Trade away</button>
      <button class="btn" data-act="get" data-id="${p.id}">Trade for</button>
    </div>
    <div class="actions" style="margin-top:8px">${onTeam(p.id)
      ? `<button class="btn" data-act="ir" data-id="${p.id}">${irSet.has(p.id) ? "Back from IR" : "Move to IR"}</button><button class="btn ghost" data-act="team-rm" data-id="${p.id}">Remove from My team</button>`
      : `<button class="btn" data-act="team-add" data-id="${p.id}" style="flex:1">Add to My team</button>`}</div>`;
}

// ---------- THIS WEEK ----------
const weekState = { sched: D.week.focus === "next" ? "nxt" : "wk", form: "hot", luck: "sell", open: {} };
function heatGrid(key) {
  const teams = Object.entries(D.teams).filter(([, i]) => !nz(i[key]));
  if (!teams.length) return `<div class="list"><div class="empty">The schedule didn't load on today's update.</div></div>`;
  teams.sort((a, b) => b[1][key] - a[1][key] || a[0].localeCompare(b[0]));
  return `<div class="teamgrid">${teams.map(([t, i]) => `<div class="tile h${Math.min(4, i[key])}"><span>${t}</span><b>${i[key]}</b></div>`).join("")}</div>
    <div class="scale"><span><i style="background:var(--heat4)"></i>4+ games</span><span><i style="background:var(--heat2)"></i>2</span><span><i style="background:var(--heat0)"></i>0</span></div>`;
}
function capList(id, items, fn, empty) {
  const n = weekState.open[id] ? items.length : 5;
  return listHTML(items.slice(0, n), fn, empty) +
    (items.length > 5 ? `<button class="more" data-more="${id}">${weekState.open[id] ? "Show fewer" : `Show all ${items.length}`}</button>` : "");
}
function renderWeek() {
  const W = D.week, nxt = W.focus === "next";
  const L = ids => ids.map(id => P.get(id)).filter(Boolean);
  const stream = L(D.pulse.stream), goalies = L(D.pulse.goalies), hot = L(D.pulse.hot), cold = L(D.pulse.cold);
  const sell = L(D.pulse.sell), buy = L(D.pulse.buy);
  const wkFp = p => nxt ? p.nxtFp : p.wkFp, wkG = p => nxt ? p.nxt : p.wk;
  const daysLeft = Math.round((dt(W.sun) - dt(W.today)) / 864e5) + 1;

  // briefing
  const key = nxt ? "nxt" : "wk";
  const most = Object.entries(D.teams).filter(([, i]) => !nz(i[key]));
  const top = most.length ? Math.max(...most.map(([, i]) => i[key])) : 0;
  const busy = most.filter(([, i]) => i[key] === top).map(([t]) => t).sort();
  const lnk = p => `<button class="lnk" data-pid="${p.id}">${esc(p.name)}</button>`;
  const brief = [];
  if (busy.length) brief.push(["b", top, `${busy.length > 4 ? busy.slice(0, 4).join(", ") + ` and ${busy.length - 4} more` : busy.join(", ")} ${busy.length === 1 ? "has" : "have"} the most games ${nxt ? "next week" : "left this week"} (${top}). Lean on players from ${busy.length === 1 ? "that team" : "those teams"}.`]);
  if (stream[0]) brief.push(["g", "+", `Best pickup: ${lnk(stream[0])}, about ${f1(wkFp(stream[0]))} points over ${plural(wkG(stream[0]), "game")}. Check he's a free agent in Yahoo.`]);
  if (hot[0]) brief.push(["r", "↑", `Hottest player: ${lnk(hot[0])}, scoring ${f2(hot[0].recFpg)} a game over the last ${C.recentDays} days.`]);

  const html = `
    <h1>${nxt ? "Next week" : "This week"}</h1>
    <p class="intro">${nxt ? `${fd(W.nmon)} to ${fday(W.nsun)}. Set your adds tonight.` : `${fd(W.mon)} to ${fday(W.sun)}. ${daysLeft > 1 ? `${words[daysLeft] ? words[daysLeft][0].toUpperCase() + words[daysLeft].slice(1) : daysLeft} days left` : "Last day"}, so lineup moves still count.`}</p>
    ${brief.length ? `<ul class="brief">${brief.map(([c, ic, t]) => `<li><span class="ic ${c}">${ic}</span><span>${t}</span></li>`).join("")}</ul>` : ""}

    <section class="block blueline">
      <div class="bh"><h2>Games by team</h2>${seg("sched", [["wk", "Rest of week"], ["nxt", "Next week"]], weekState.sched)}</div>
      <p class="hint">More games means more points. Teams with 4 are the best places to find streamers.</p>
      ${heatGrid(weekState.sched)}
    </section>

    <section class="block">
      <h2>Pickups</h2>
      <p class="hint">Players probably available in a ${C.leagueTeams}-team league, by points expected ${nxt ? "next week" : "for the rest of the week"}.</p>
      ${capList("stream", stream, p => row(p, `<b>${f1(wkFp(p))}</b>`, plural(wkG(p), "game")), "No pickups to suggest yet.")}
    </section>

    <section class="block">
      <h2>Goalie starts</h2>
      <p class="hint">Expected starts ${nxt ? "next week" : "for the rest of the week"}, based on how often each goalie plays.</p>
      ${capList("goalies", goalies, g => row(g, `<b>${f1(wkFp(g))}</b>`, `about ${f1(nxt ? g.nxtStarts : g.wkStarts)} starts`), "No goalie projections yet.")}
    </section>

    <section class="block redline">
      <div class="bh"><h2>Form</h2>${seg("form", [["hot", "Heating up"], ["cold", "Cooling off"]], weekState.form)}</div>
      <p class="hint">Points per game over the last ${C.recentDays} days compared with their season.</p>
      ${weekState.form === "hot"
        ? capList("hot", hot, p => row(p, `<b class="up">${sgn(p.recFpg - p.fpg, f2)}</b>`, `${f2(p.recFpg)} lately`), "Needs a few more games of data.")
        : capList("cold", cold, p => row(p, `<b class="dn">${sgn(p.recFpg - p.fpg, f2)}</b>`, `${f2(p.recFpg)} lately`), "Needs a few more games of data.")}
    </section>

    <section class="block">
      <div class="bh"><h2>Shooting luck</h2>${seg("luck", [["sell", "Sell high"], ["buy", "Buy low"]], weekState.luck)}</div>
      <p class="hint">${weekState.luck === "sell"
        ? "Scoring on far more of their shots than usual. That rarely lasts, so their value may never be higher."
        : "Scoring on far fewer of their shots than usual. If the shots keep coming, goals usually follow."}</p>
      ${weekState.luck === "sell"
        ? capList("sell", sell, p => row(p, `<b class="up">${f1(p.sh)}%</b>`, `usually ${f1(p.shBase)}%`), "No one is running unusually hot.")
        : capList("buy", buy, p => row(p, `<b class="dn">${f1(p.sh)}%</b>`, `usually ${f1(p.shBase)}%`), "No one is running unusually cold.")}
    </section>`;
  document.getElementById("v-week").innerHTML = html;
}
document.getElementById("v-week").addEventListener("click", e => {
  const s = e.target.closest("[data-seg]");
  if (s) { weekState[s.dataset.seg] = s.dataset.val; return renderWeek(); }
  const m = e.target.closest("[data-more]");
  if (m) { weekState.open[m.dataset.more] = !weekState.open[m.dataset.more]; renderWeek(); }
});

// ---------- RANKINGS ----------
const SORTS = {
  sk: [["proj", "Projected per game"], ["rosFp", "Rest of season"], ["rosVor", "Value over a free agent"], ["fpg", "Season per game"],
       ["recFpg", `Last ${C.recentDays} days`], ["fpgP", "Shots, hits and blocks"]],
  g: [["projTG", "Projected per team game"], ["proj", "Projected per start"], ["rosFp", "Rest of season"], ["share", "Share of starts"], ["fpg", "Season per game"]],
};
const rs = { view: "sk", pos: "ALL", q: "", sort: { sk: "proj", g: "projTG" }, hv: "up", shown: 30 };
function rankVal(p, k) {
  const v = p[k];
  if (k === "share") return [`<b>${pct(v * 100)}</b>`, "of starts"];
  if (k === "rosFp") return [`<b>${f0(v)}</b>`, "points left"];
  if (k === "rosVor") return [`<b>${sgn(v, f0)}</b>`, "vs free agent"];
  if (k === "fpgP") return [`<b>${f2(v)}</b>`, "per game"];
  if (k === "proj" && isG(p)) return [`<b>${f2(v)}</b>`, "per start"];
  return [`<b>${f2(v)}</b>`, "per game"];
}
function rankListHTML() {
  const isSk = rs.view === "sk";
  const sortKey = rs.sort[rs.view];
  const q = rs.q.trim().toLowerCase();
  const pool = (isSk ? SK : GO).filter(p => p.q
    && (!isSk || rs.pos === "ALL" || eligOf(p).includes(rs.pos) || (rs.pos === "F" && eligOf(p).some(x => x !== "D")))
    && (!q || p.name.toLowerCase().includes(q) || p.team.toLowerCase() === q))
    .sort((a, b) => (nz(b[sortKey]) ? -1e9 : b[sortKey]) - (nz(a[sortKey]) ? -1e9 : a[sortKey]));
  return listHTML(pool.slice(0, rs.shown), (p, i) => { const [v, s] = rankVal(p, sortKey); return row(p, v, s, i + 1, isSk ? split(p) : ""); }, "No players match that search.")
    + (pool.length > rs.shown ? `<button class="more" data-rmore>Show more (${pool.length - rs.shown} left)</button>` : "");
}
function renderRank() {
  const el = document.getElementById("v-rank");
  const head = `<h1>Rankings</h1>
    <p class="intro">Everyone ranked by your league's scoring. Tap a player for the full picture.</p>
    ${seg("rview", [["sk", "Skaters"], ["g", "Goalies"], ["hv", "Hidden value"]], rs.view, true)}`;
  let body = "";
  if (rs.view === "hv") {
    const pool = SK.filter(p => p.q).sort((a, b) => b.gap - a.gap);
    const items = rs.hv === "up" ? pool.slice(0, 30) : pool.slice(-30).reverse();
    body = `<div style="margin:16px 0 4px">${seg("hv", [["up", "Better than their points"], ["down", "Worse than their points"]], rs.hv)}</div>
      <p class="hint">${rs.hv === "up"
        ? "Your scoring ranks these players much higher than plain NHL points do, usually because of hits, blocks and shots. League-mates who draft by points will undervalue them."
        : "NHL points make these players look better than they are in your league. Few hits, blocks or shots, so they rely on scoring."}</p>
      ${listHTML(items, p => row(p, `<b class="${p.gap > 0 ? "dn" : "up"}">${p.gap > 0 ? "+" : "−"}${Math.abs(p.gap)}</b>`, `#${p.rank} here, #${p.ppgRank} by points`), "Not enough games yet.")}`;
  } else {
    const isSk = rs.view === "sk";
    const sortKey = rs.sort[rs.view];
    body = `<div class="controls" style="margin-top:14px">
        <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/></svg>
          <input type="search" id="rq" placeholder="Search a player or team (EDM)" value="${esc(rs.q)}" autocomplete="off" autocorrect="off" spellcheck="false"></div>
        ${isSk ? `<div class="chips" role="group">${["ALL", "F", "C", "LW", "RW", "D"].map(x =>
          `<button class="chip" data-pos="${x}" aria-pressed="${rs.pos === x}">${x === "ALL" ? "All" : x === "F" ? "Forwards" : x}</button>`).join("")}</div>` : ""}
        <div class="sortrow"><label for="rsort">Sort by</label><select id="rsort">${SORTS[rs.view].map(([k, l]) =>
          `<option value="${k}" ${k === sortKey ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      </div>
      <div id="rlist">${rankListHTML()}</div>
      ${isSk ? `<p class="hint" style="margin-top:12px">The small bar shows where points come from: <b style="color:var(--blue)">blue</b> is goals and assists, <b style="color:var(--gold)">gold</b> is shots, hits and blocks.</p>` : ""}`;
  }
  el.innerHTML = head + body;
  const rq = document.getElementById("rq");
  if (rq) rq.oninput = e => { rs.q = e.target.value; rs.shown = 30; document.getElementById("rlist").innerHTML = rankListHTML(); };
  const sel = document.getElementById("rsort");
  if (sel) sel.onchange = e => { rs.sort[rs.view] = e.target.value; rs.shown = 30; renderRank(); };
}
document.getElementById("v-rank").addEventListener("click", e => {
  const s = e.target.closest("[data-seg]");
  if (s) { if (s.dataset.seg === "rview") { rs.view = s.dataset.val; rs.shown = 30; rs.q = ""; } else rs.hv = s.dataset.val; return renderRank(); }
  const c = e.target.closest("[data-pos]"); if (c) { rs.pos = c.dataset.pos; rs.shown = 30; return renderRank(); }
  if (e.target.closest("[data-rmore]")) { rs.shown += 30; document.getElementById("rlist").innerHTML = rankListHTML(); }
});

// ---------- player search ----------
const ALL = [...P.values()].sort((a, b) => (b.rosFp || 0) - (a.rosFp || 0));
function picker(root, placeholder, onPick) {
  root.innerHTML = `<div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/></svg>
    <input type="search" placeholder="${placeholder}" autocomplete="off" autocorrect="off" spellcheck="false"></div><div class="sugg" hidden></div>`;
  root.style.position = "relative";
  const inp = root.querySelector("input"), box = root.querySelector(".sugg");
  inp.oninput = () => {
    const q = inp.value.trim().toLowerCase();
    if (q.length < 2) { box.hidden = true; return; }
    const m = ALL.filter(p => p.name.toLowerCase().includes(q) || p.team.toLowerCase() === q).slice(0, 6);
    box.innerHTML = m.length ? m.map(p => `<button class="row" data-pick="${p.id}"><span class="who"><b>${esc(p.name)}</b>${meta(p)}</span>
      <span class="val"><b>${f2(projTG(p))}</b><small>per game</small></span></button>`).join("") : `<div class="empty">No player found.</div>`;
    box.hidden = false;
  };
  box.onclick = e => { const b = e.target.closest("[data-pick]"); if (!b) return; onPick(+b.dataset.pick); inp.value = ""; box.hidden = true; inp.blur(); };
  document.addEventListener("click", e => { if (!root.contains(e.target)) box.hidden = true; });
}

// ---------- COMPARE ----------
let cmp = store.get("cmp", []).filter(id => P.has(id));
function addCompare(id) {
  const p = P.get(id); if (!p) return;
  if (!cmp.includes(id)) { if (cmp.length >= 4) cmp.shift(); cmp.push(id); }
  store.set("cmp", cmp); renderCompare(); toast(`Added ${p.name} to Compare`);
}
const GROUPS = [
  { title: "Value", rows: [
    ["Projected points per game", p => projTG(p), f2, "hi"],
    ["Points left this season", p => p.rosFp, f0, "hi"],
    ["Value over a free agent", p => p.rosVor, v => sgn(v, f0), "hi"],
    ["Your playoff weeks", p => D.week.ps ? p.poFp : null, f0, "hi"],
  ]},
  { title: "Schedule", rows: [
    ["Games left this week", p => p.wk, f0, "hi"],
    ["Points expected this week", p => p.wkFp, f1, "hi"],
    ["Games next week", p => p.nxt, f0, "hi"],
    ["Points expected next week", p => p.nxtFp, f1, "hi"],
  ]},
  { title: "Form and style", rows: [
    ["Season points per game", p => p.fpg, f2, "hi"],
    [`Last ${C.recentDays} days per game`, p => isG(p) ? null : p.recFpg, f2, "hi"],
    ["From shots, hits and blocks", p => isG(p) ? null : p.peri, pct, null],
    ["Shooting % (usual)", p => isG(p) ? null : p.sh, (v, p) => nz(v) ? DASH : `${f1(v)}%<small>usually ${f1(p.shBase)}%</small>`, null],
    ["Rank at position", p => isG(p) ? p.projRank : p.posRank, v => nz(v) ? DASH : "#" + v, "lo"],
  ]},
  { title: "Goalies", goalie: true, rows: [
    ["Share of starts", p => isG(p) ? p.share * 100 : null, pct, "hi"],
    ["Projected points per start", p => isG(p) ? p.proj : null, f2, "hi"],
    ["Save %", p => isG(p) ? p.svPct : null, f3, "hi"],
  ]},
];
function renderCompare() {
  const el = document.getElementById("v-compare");
  const ps = cmp.map(id => P.get(id)).filter(Boolean);
  const n = ps.length;
  let body = "";
  if (!n) {
    body = `<div class="list" style="margin-top:14px"><div class="empty">Search for a player above, or tap any player in the app and choose <b>Compare</b>. You can line up to four side by side.</div></div>`;
  } else {
    const cols = `grid-template-columns:repeat(${n},minmax(0,1fr))`;
    body = `<div class="chead" style="${cols}">${ps.map(p => `<div class="pcol"><button class="x" data-rm="${p.id}" aria-label="Remove ${esc(p.name)}">✕</button>
      <b data-pid="${p.id}" style="cursor:pointer;padding-right:14px">${esc(p.name)}</b>${meta(p)}</div>`).join("")}</div>`;
    GROUPS.forEach(gr => {
      if (gr.goalie && !ps.some(isG)) return;
      const rows = gr.rows.map(([label, get, fmt, better]) => {
        const vals = ps.map(get);
        if (vals.every(nz)) return "";
        const nums = vals.filter(v => !nz(v));
        const max = Math.max(...nums.map(Math.abs), 1e-9);
        let best = -1;
        if (better && n > 1 && nums.length > 1 && !nums.every(v => v === nums[0])) {
          const target = better === "hi" ? Math.max(...nums) : Math.min(...nums);
          best = vals.findIndex(v => v === target);
        }
        return `<div class="mrow"><div class="ml">${label}</div><div class="mv" style="${cols}">${vals.map((v, i) =>
          `<div class="mc ${i === best ? "best" : ""}"><b>${fmt(v, ps[i])}</b>${better && !nz(v) && n > 1 ? `<div class="bar"><i style="width:${better === "lo" ? (Math.min(...nums) / v) * 100 : Math.max(0, v) / max * 100}%"></i></div>` : ""}</div>`).join("")}</div></div>`;
      }).join("");
      if (rows) body += `<div class="mgroup"><h3>${gr.title}</h3>${rows}</div>`;
    });
    body += `<div class="actions"><button class="btn ghost" id="cClear">Clear all</button></div>`;
  }
  el.innerHTML = `<h1>Compare</h1><p class="intro">The best number in each row is highlighted in blue.</p><div id="cPick"></div>${body}`;
  picker(document.getElementById("cPick"), n >= 4 ? "Add a player (replaces the first)" : "Add a player", addCompare);
  el.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { cmp = cmp.filter(x => x !== +b.dataset.rm); store.set("cmp", cmp); renderCompare(); });
  const cc = document.getElementById("cClear"); if (cc) cc.onclick = () => { cmp = []; store.set("cmp", cmp); renderCompare(); };
}

// ---------- TRADE ----------
let trade = { give: store.get("give", []).filter(id => P.has(id)), get: store.get("get", []).filter(id => P.has(id)) };
const saveTrade = () => { store.set("give", trade.give); store.set("get", trade.get); };
function addTrade(side, id) {
  const other = side === "give" ? "get" : "give";
  trade[other] = trade[other].filter(x => x !== id);
  if (!trade[side].includes(id)) trade[side].push(id);
  saveTrade(); renderTrade(); toast(`Added ${P.get(id).name} to ${side === "give" ? "You give" : "You get"}`);
}
function totals(ids) {
  const ps = ids.map(id => P.get(id)).filter(Boolean);
  const sum = k => ps.reduce((s, p) => s + (p[k] || 0), 0);
  const skFp = ps.filter(p => !isG(p)).reduce((s, p) => s + p.rosFp, 0);
  return { ps, n: ps.length, val: ps.reduce((s, p) => s + Math.max(0, p.rosVor || 0), 0), rosFp: sum("rosFp"),
    wkFp: sum("wkFp"), nxtFp: sum("nxtFp"), poFp: sum("poFp"),
    peri: skFp > 0 ? ps.filter(p => !isG(p)).reduce((s, p) => s + p.peri * p.rosFp, 0) / skFp : null };
}
function rinkSVG(x) {
  // x from -1 (their end) to 1 (your end)
  const px = 300 + x * 236;
  return `<svg class="rink" viewBox="0 0 600 150" role="img" aria-label="Trade meter">
    <rect x="3" y="3" width="594" height="144" rx="64" fill="var(--surface)" stroke="var(--line)" stroke-width="4"/>
    <line x1="44" y1="9" x2="44" y2="141" stroke="var(--red)" stroke-width="2" opacity=".7"/>
    <line x1="556" y1="9" x2="556" y2="141" stroke="var(--red)" stroke-width="2" opacity=".7"/>
    <path d="M44 61 a14 14 0 0 1 0 28" fill="var(--blue-soft)" stroke="var(--red)" stroke-width="1.5" opacity=".9"/>
    <path d="M556 61 a14 14 0 0 0 0 28" fill="var(--blue-soft)" stroke="var(--red)" stroke-width="1.5" opacity=".9"/>
    <rect x="200" y="5" width="7" height="140" fill="var(--blue)" opacity=".85"/>
    <rect x="393" y="5" width="7" height="140" fill="var(--blue)" opacity=".85"/>
    <rect x="297" y="5" width="6" height="140" fill="var(--red)" opacity=".85"/>
    <circle cx="300" cy="75" r="30" fill="none" stroke="var(--blue)" stroke-width="2" opacity=".6"/>
    <circle cx="120" cy="75" r="26" fill="none" stroke="var(--red)" stroke-width="1.5" opacity=".45"/>
    <circle cx="480" cy="75" r="26" fill="none" stroke="var(--red)" stroke-width="1.5" opacity=".45"/>
    <g class="puck" style="transform:translateX(${px - 300}px)">
      <ellipse cx="300" cy="79" rx="17" ry="7" fill="#000" opacity=".18"/>
      <ellipse cx="300" cy="75" rx="16" ry="9" fill="#111820"/>
      <ellipse cx="300" cy="72" rx="16" ry="7" fill="#2a3442"/>
    </g>
  </svg>`;
}
function sideHTML(side, title) {
  const ps = trade[side].map(id => P.get(id)).filter(Boolean);
  return `<div class="side"><h2>${title}</h2>
    ${ps.length ? `<div class="list">${ps.map(p => `<div class="row" style="cursor:default"><span class="who" data-pid="${p.id}" style="cursor:pointer"><b>${esc(p.name)}</b>${meta(p)}</span>
      <span class="val"><b>${f0(p.rosFp)}</b><small>points left</small></span>
      <button class="rm" data-trm="${side}:${p.id}" aria-label="Remove ${esc(p.name)}">✕</button></div>`).join("")}</div>` : ""}
    <div class="addwrap" id="pick-${side}"></div></div>`;
}
let lastPuck = 0;
function renderTrade() {
  const el = document.getElementById("v-trade");
  const G = totals(trade.give), R = totals(trade.get);
  let verdict;
  if (!G.n || !R.n) {
    verdict = `<div class="verdict">${rinkSVG(0)}<div class="ends"><span>Their end</span><span>Your end</span></div>
      <div class="vh">Build a trade</div><p class="vs">Add at least one player on each side. The puck slides toward whoever comes out ahead.</p></div>`;
  } else {
    const net = R.val - G.val;
    const scale = Math.max(40, 0.6 * (G.val + R.val));
    const fair = Math.max(8, 0.08 * Math.max(G.val, R.val));
    const x = Math.max(-1, Math.min(1, net / scale));
    const cls = Math.abs(net) <= fair ? "" : net > 0 ? "win" : "lose";
    const head = Math.abs(net) <= fair ? "Even trade" : net > 0 ? `You win by ${f0(net)} points` : `You lose by ${f0(-net)} points`;
    const facts = [];
    facts.push(`Rest of the season: ${sgn(R.rosFp - G.rosFp, f0)} total points for you.`);
    const wk = R.wkFp - G.wkFp, nw = R.nxtFp - G.nxtFp, po = R.poFp - G.poFp;
    if (Math.abs(wk) >= 3) facts.push(`This week it ${wk > 0 ? "adds" : "costs"} about ${f1(Math.abs(wk))} points.`);
    if (Math.abs(nw) >= 3) facts.push(`Next week it ${nw > 0 ? "adds" : "costs"} about ${f1(Math.abs(nw))} points.`);
    if (D.week.ps && Math.abs(po) >= 3) facts.push(`In your playoff weeks (${fd(D.week.ps)} to ${fd(D.week.pe)}) it ${po > 0 ? "adds" : "costs"} about ${f0(Math.abs(po))} points.`);
    if (G.peri !== null && R.peri !== null && Math.abs(R.peri - G.peri) >= 8)
      facts.push(R.peri > G.peri ? `You get steadier production: ${pct(R.peri)} from shots, hits and blocks, versus ${pct(G.peri)}.`
                                 : `You get streakier production: ${pct(R.peri)} from shots, hits and blocks, versus ${pct(G.peri)}.`);
    const lucky = R.ps.filter(p => !isG(p) && !nz(p.shDiff) && p.shDiff >= 5);
    if (lucky.length) facts.push(`${lucky.map(p => esc(p.name)).join(" and ")} ${lucky.length > 1 ? "are" : "is"} shooting well above normal. Expect fewer goals.`);
    const due = R.ps.filter(p => !isG(p) && !nz(p.shDiff) && p.shDiff <= -4);
    if (due.length) facts.push(`${due.map(p => esc(p.name)).join(" and ")} ${due.length > 1 ? "are" : "is"} shooting below normal and could heat up.`);
    const below = [...G.ps, ...R.ps].filter(p => (p.rosVor || 0) <= 0);
    if (below.length) facts.push(`${below.map(p => esc(p.name)).join(" and ")} ${below.length > 1 ? "count" : "counts"} as zero: a free agent would score about the same.`);
    if (G.n !== R.n) facts.push(R.n > G.n ? `You'll need to drop ${plural(R.n - G.n, "player")} to make room. That's already factored in.` : `You free up ${plural(G.n - R.n, "roster spot")}. That's already factored in.`);
    const tr = (l, a, b, f) => `<tr><td>${l}</td><td>${f(a)}</td><td>${f(b)}</td><td>${sgn(b - a, f)}</td></tr>`;
    const summary = [`Trade check, ${D.generated}`, `Give: ${G.ps.map(p => p.name).join(", ")}`, `Get: ${R.ps.map(p => p.name).join(", ")}`,
      `${head}. Value over a free agent: ${Math.round(G.val)} given, ${Math.round(R.val)} received.`].join("\n");
    verdict = `<div class="verdict">${rinkSVG(x)}<div class="ends"><span>Their end</span><span>Your end</span></div>
      <div class="vh ${cls}">${head}</div>
      <p class="vs">Measured in points above what a free agent would give you, for the rest of the season.</p>
      <ul class="facts">${facts.map(f => `<li>${f}</li>`).join("")}</ul>
      <details class="more-d"><summary>See the numbers</summary>
        <table class="dt"><thead><tr><th></th><th>Give</th><th>Get</th><th>Change</th></tr></thead><tbody>
        ${tr("Value over a free agent", G.val, R.val, f0)}
        ${tr("Points left this season", G.rosFp, R.rosFp, f0)}
        ${tr("Points this week", G.wkFp, R.wkFp, f1)}
        ${tr("Points next week", G.nxtFp, R.nxtFp, f1)}
        ${D.week.ps ? tr("Your playoff weeks", G.poFp, R.poFp, f0) : ""}
        </tbody></table></details>
      <div class="actions"><button class="btn primary" id="tCopy">Copy for league chat</button><button class="btn" id="tSwap">Swap sides</button><button class="btn ghost" id="tClear">Clear</button></div>
      <textarea id="tSum" hidden>${esc(summary)}</textarea></div>`;
    lastPuck = x;
  }
  el.innerHTML = `<h1>Trade</h1><p class="intro">Line up a trade and see who comes out ahead, counting position scarcity, schedule and roster spots.</p>
    <div class="sides">${sideHTML("give", "You give")}${sideHTML("get", "You get")}</div>${verdict}`;
  picker(document.getElementById("pick-give"), "Add a player you'd give", id => addTrade("give", id));
  picker(document.getElementById("pick-get"), "Add a player you'd get", id => addTrade("get", id));
  el.querySelectorAll("[data-trm]").forEach(b => b.onclick = () => {
    const [s, id] = b.dataset.trm.split(":"); trade[s] = trade[s].filter(x => x !== +id); saveTrade(); renderTrade();
  });
  const c = document.getElementById("tCopy");
  if (c) c.onclick = async () => { try { await navigator.clipboard.writeText(document.getElementById("tSum").value); toast("Copied for league chat"); } catch (e) { toast("Copying isn't allowed in this browser"); } };
  const sw = document.getElementById("tSwap"); if (sw) sw.onclick = () => { trade = { give: trade.get, get: trade.give }; saveTrade(); renderTrade(); };
  const cl = document.getElementById("tClear"); if (cl) cl.onclick = () => { trade = { give: [], get: [] }; saveTrade(); renderTrade(); };
}

// ---------- MY TEAM ----------
let team, irSet;
const fileTeam = D.myTeam || { roster: [], ir: [], version: "" };
if (fileTeam.version && store.get("teamFrom", "") !== fileTeam.version) {
  team = fileTeam.roster.filter(id => P.has(id));
  irSet = new Set(fileTeam.ir.filter(id => P.has(id)));
  store.set("teamFrom", fileTeam.version); store.set("team", team); store.set("ir", [...irSet]);
} else {
  team = store.get("team", []).filter(id => P.has(id));
  irSet = new Set(store.get("ir", []).filter(id => P.has(id)));
}
let taken = new Set(store.get("taken", []));
const saveTeam = () => { store.set("team", team); store.set("ir", [...irSet]); store.set("elig", elig); store.set("taken", [...taken]); };
const onTeam = id => team.includes(id);
const teamState = { tab: "lineup", moves: "season" };
const ROSTER_MAX = Object.values(C.slots).reduce((a, b) => a + b, 0) + (C.bench || 4);
const GPW = 3.3; // average games per team per week
const avg = xs => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;

function addToTeam(id) {
  const p = P.get(id); if (!p) return;
  if (!onTeam(id)) team.push(id);
  taken.delete(id);
  saveTeam(); renderTeam(); toast(`Added ${p.name} to My team`);
}
function removeFromTeam(id) {
  const p = P.get(id);
  team = team.filter(x => x !== id); irSet.delete(id);
  saveTeam(); renderTeam(); if (p) toast(`Removed ${p.name} from My team`);
}
function toggleIR(id) {
  const p = P.get(id);
  if (irSet.has(id)) { irSet.delete(id); toast(`${p.name} is back in your lineup`); }
  else { irSet.add(id); toast(`Moved ${p.name} to IR`); }
  saveTeam(); renderTeam();
}
function toggleElig(id, pos) {
  const p = P.get(id); if (!p || isG(p)) return;
  let e = eligOf(p).slice();
  e = e.includes(pos) ? e.filter(x => x !== pos) : [...e, pos];
  if (!e.length) e = [p.pos];
  elig[id] = e; saveTeam();
  renderWeek(); renderTeam(); renderRank(); renderCompare(); renderTrade();
}

const SLOTS = [];
Object.entries(C.slots).forEach(([k, v]) => { for (let i = 0; i < v; i++) SLOTS.push(k); });
function lineup() {
  const active = team.filter(id => !irSet.has(id)).map(id => P.get(id)).filter(Boolean).sort((a, b) => projTG(b) - projTG(a));
  const slots = SLOTS.map(s => ({ s, p: null }));
  const fits = (p, s) => s === "Util" ? !isG(p) : eligOf(p).includes(s);
  const bench = [];
  for (const p of active) {
    let sl = slots.find(x => !x.p && x.s !== "Util" && fits(p, x.s));
    if (!sl) {
      for (const x of slots) {
        if (x.p && x.s !== "Util" && fits(p, x.s)) {
          const alt = slots.find(y => !y.p && y.s !== "Util" && fits(x.p, y.s));
          if (alt) { alt.p = x.p; x.p = null; sl = x; break; }
        }
      }
    }
    if (!sl) sl = slots.find(x => !x.p && x.s === "Util" && fits(p, "Util"));
    if (sl) sl.p = p; else bench.push(p);
  }
  return { slots, bench, ir: team.filter(id => irSet.has(id)).map(id => P.get(id)).filter(Boolean) };
}

// What a typical starter looks like in your league, per slot type
const LG = (() => {
  const T = C.leagueTeams, out = {}, used = new Set();
  const q = SK.filter(p => p.q);
  ["C", "LW", "RW", "D"].forEach(pos => {
    const all = q.filter(p => p.pos === pos).sort((a, b) => b.proj - a.proj);
    const top = all.slice(0, T * (C.slots[pos] || 0));
    top.forEach(p => used.add(p.id));
    out[pos] = avg(top.map(p => p.proj)); out[pos + "Max"] = all[0] ? all[0].proj : 1;
  });
  const util = q.filter(p => !used.has(p.id)).sort((a, b) => b.proj - a.proj).slice(0, T * (C.slots.Util || 0));
  out.Util = avg(util.map(p => p.proj)); out.UtilMax = Math.max(out.CMax, out.LWMax, out.RWMax, out.DMax);
  const g = GO.filter(x => x.q).sort((a, b) => b.projTG - a.projTG);
  out.G = avg(g.slice(0, T * (C.slots.G || 0)).map(x => x.projTG)); out.GMax = g[0] ? g[0].projTG : 1;
  return out;
})();

const nxtFocus = () => D.week.focus === "next";
const wkKey = () => nxtFocus() ? "nxtFp" : "wkFp";
const gKey = () => nxtFocus() ? "nxt" : "wk";
const isAvail = p => {
  if (onTeam(p.id) || taken.has(p.id) || !p.q) return false;
  if (isG(p)) return (p.projRank || 999) > C.leagueTeams * ((C.slots.G || 2) + 1) && p.share >= 0.45;
  return (p.projRank || 999) > C.leagueTeams * C.rosteredPerTeam;
};

function shapeHTML(L) {
  const groups = Object.keys(C.slots);
  return `<div class="shape">${groups.map(gp => {
    const ps = L.slots.filter(x => x.s === gp);
    const filled = ps.filter(x => x.p);
    const mine = filled.length ? avg(ps.map(x => x.p ? projTG(x.p) : 0)) : 0;
    const lg = LG[gp] || 0;
    const max = Math.max(LG[gp + "Max"] || 1, mine, lg * 1.4);
    const r = lg ? mine / lg : 1;
    const tag = !filled.length ? ["empty", "Empty"] : r >= 1.08 ? ["strong", "Strong"] : r <= 0.92 ? ["weak", "Weak"] : ["", "Average"];
    return `<div class="sh-row"><span class="sh-l">${gp}</span>
      <div class="sh-bar" title="Yours ${f2(mine)} vs typical ${f2(lg)}"><i class="${tag[0]}" style="width:${Math.min(100, mine / max * 100)}%"></i><em style="left:${Math.min(100, lg / max * 100)}%"></em></div>
      <span class="sh-v">${f2(mine)}</span><span class="sh-tag ${tag[0]}">${tag[1]}</span></div>`;
  }).join("")}
  <p class="hint" style="margin-top:10px">Bars show your starters' projected points per game. The tick marks a typical starter in a ${C.leagueTeams}-team league.</p></div>`;
}

function prosCons(L) {
  const pros = [], cons = [];
  const starters = L.slots.filter(x => x.p).map(x => x.p);
  const sk = starters.filter(p => !isG(p));
  const nameL = p => `<button class="lnk" data-pid="${p.id}">${esc(p.name)}</button>`;
  const posName = { C: "centres", LW: "left wings", RW: "right wings", D: "defence", Util: "Util spot", G: "goalies" };
  Object.keys(C.slots).forEach(gp => {
    const ps = L.slots.filter(x => x.s === gp);
    const empty = ps.filter(x => !x.p).length;
    if (empty) {
      cons.push(empty === ps.length
        ? `Your ${gp} spot${ps.length > 1 ? "s are" : " is"} empty. Add a ${gp === "Util" ? "skater" : gp} so you stop losing points every game.`
        : `${empty} of your ${ps.length} ${gp} spots ${empty > 1 ? "are" : "is"} empty. Add a ${gp} so you stop losing points every game.`);
      return;
    }
    const mine = avg(ps.map(x => projTG(x.p))), lg = LG[gp];
    if (!lg) return;
    const r = mine / lg;
    if (r >= 1.08) pros.push(`Your ${posName[gp]} ${gp === "Util" ? "is" : "are"} a strength: ${f2(mine)} projected per game, against ${f2(lg)} for a typical starter.`);
    if (r <= 0.92) cons.push(`Your ${posName[gp]} ${gp === "Util" ? "is" : "are"} below a typical starter: ${f2(mine)} per game against ${f2(lg)}.`);
  });
  const names = ps => ps.length === 1 ? nameL(ps[0]) : ps.slice(0, -1).map(nameL).join(", ") + " and " + nameL(ps[ps.length - 1]);
  const weakS = starters.filter(p => (p.vor || 0) < 0);
  if (weakS.length) cons.push(`${names(weakS)} ${weakS.length > 1 ? "are" : "is"} starting, but a free agent at the same position would score about as much. Check Moves for upgrades.`);
  starters.filter(isG).forEach(g => {
    if (g.share >= 0.75) pros.push(`${nameL(g)} starts ${pct(g.share * 100)} of his team's games. That steady workload is worth a lot in your scoring.`);
    else if (g.share < 0.55) cons.push(`${nameL(g)} only starts ${pct(g.share * 100)} of his team's games. Goalie starts are your biggest weekly lever.`);
  });
  if (sk.length >= 4) {
    const tot = sk.reduce((s, p) => s + p.proj, 0) || 1;
    const peri = sk.reduce((s, p) => s + p.peri * p.proj, 0) / tot;
    if (peri >= 38) pros.push(`${pct(peri)} of your skaters' points come from shots, hits and blocks, so your weekly floor is steady.`);
    else if (peri <= 25) cons.push(`Only ${pct(peri)} of your skaters' points come from shots, hits and blocks. Your weeks will swing with goals. A hitting, shot-blocking defenceman would steady things.`);
  }
  const ros = team.map(id => P.get(id)).filter(Boolean);
  const sell = ros.filter(p => !isG(p) && !nz(p.shDiff) && p.shDiff >= 5);
  if (sell.length) cons.push(`${names(sell)} ${sell.length > 1 ? "are" : "is"} scoring on far more of ${sell.length > 1 ? "their" : "his"} shots than usual. Goals should slow down, so ${sell.length > 1 ? "their" : "his"} trade value may be at its peak.`);
  const buy = ros.filter(p => !isG(p) && !nz(p.shDiff) && p.shDiff <= -4);
  if (buy.length) pros.push(`${names(buy)} ${buy.length > 1 ? "are" : "is"} shooting below ${buy.length > 1 ? "their" : "his"} usual rate. Expect more goals soon.`);
  const hotP = ros.filter(p => !isG(p) && !nz(p.recFpg) && p.recGp >= 3 && p.recFpg - p.fpg >= 1.5);
  if (hotP.length) pros.push(`${names(hotP)} ${hotP.length > 1 ? "are" : "is"} hot, scoring well above ${hotP.length > 1 ? "their" : "his"} season pace over the last ${C.recentDays} days.`);
  const coldP = ros.filter(p => !isG(p) && !nz(p.recFpg) && p.recGp >= 3 && p.recFpg - p.fpg <= -1.5);
  if (coldP.length) cons.push(`${names(coldP)} ${coldP.length > 1 ? "have" : "has"} cooled off over the last ${C.recentDays} days. Worth watching, not panicking over.`);
  const gk = gKey();
  const light = starters.filter(p => !nz(p[gk]) && p[gk] <= 1);
  const daysLeft = Math.round((dt(D.week.sun) - dt(D.week.today)) / 864e5) + 1;
  if (light.length >= 3 && (nxtFocus() || daysLeft >= 4)) cons.push(`${plural(light.length, "starter")} ${light.length === 1 ? "has" : "have"} one game or fewer ${nxtFocus() ? "next week" : "left this week"}. Check the Moves tab for streamers.`);
  if (D.week.ps) {
    const all = Object.values(D.teams).map(t => t.po).filter(v => !nz(v));
    const lgPo = avg(all), myPo = avg(starters.map(p => p.po).filter(v => !nz(v)));
    if (all.length && myPo >= lgPo + 0.6) pros.push(`Good playoff schedule: your starters average ${f1(myPo)} games in your playoff weeks, against ${f1(lgPo)} for the league.`);
    if (all.length && myPo <= lgPo - 0.6) cons.push(`Light playoff schedule: your starters average ${f1(myPo)} games in your playoff weeks, against ${f1(lgPo)} for the league.`);
  }
  if (L.ir.length > (C.irSlots || 3)) cons.push(`You have ${L.ir.length} players on IR but only ${C.irSlots} IR spots.`);
  const activeN = team.length - L.ir.length;
  if (activeN > ROSTER_MAX) cons.push(`You have ${activeN} active players, ${activeN - ROSTER_MAX} more than your ${ROSTER_MAX} roster spots.`);
  return { pros, cons };
}

function seasonMoves(L) {
  const benchIds = new Set(L.bench.map(p => p.id));
  const roster = team.filter(id => !irSet.has(id)).map(id => P.get(id)).filter(Boolean);
  const drops = roster.slice().sort((a, b) => (a.rosVor || 0) - (b.rosVor || 0)).slice(0, 6);
  const used = new Set(), out = [];
  for (const d of drops) {
    const pool = (isG(d) ? GO : SK).filter(c => isAvail(c) && !used.has(c.id) && (isG(d) || benchIds.has(d.id) || eligOf(c).some(x => eligOf(d).includes(x))));
    const c = pool.sort((a, b) => b.rosFp - a.rosFp)[0];
    if (!c) continue;
    const gain = c.rosFp - d.rosFp;
    if (gain < 8) continue;
    used.add(c.id);
    const why = [`${esc(c.name)} projects ${f2(projTG(c))} a game against ${f2(projTG(d))}.`];
    if (isG(d) && c.share - d.share >= 0.1) why.push(`He starts ${pct(c.share * 100)} of his team's games, ${esc(d.name)} ${pct(d.share * 100)}.`);
    if (!isG(d) && c.peri - d.peri >= 10) why.push(`More of his points come from hits, blocks and shots (${pct(c.peri)}), so they're steadier.`);
    if ((c.ros || 0) - (d.ros || 0) >= 2) why.push(`His team has ${(c.ros || 0) - (d.ros || 0)} more games left.`);
    out.push({ d, c, gain, unit: "points rest of season", why: why.join(" ") });
  }
  return out.sort((a, b) => b.gain - a.gain).slice(0, 5);
}

function weekMoves(L) {
  const k = wkKey(), gk = gKey();
  const benchIds = new Set(L.bench.map(p => p.id));
  const roster = team.filter(id => !irSet.has(id)).map(id => P.get(id)).filter(Boolean);
  const byVor = roster.slice().sort((a, b) => (a.rosVor || 0) - (b.rosVor || 0));
  const droppable = byVor.slice(0, Math.max(2, Math.ceil(roster.length / 3))).sort((a, b) => (a[k] || 0) - (b[k] || 0));
  const used = new Set(), out = [];
  for (const d of droppable) {
    const pool = (isG(d) ? GO : SK).filter(c => isAvail(c) && !used.has(c.id) && (isG(d) || benchIds.has(d.id) || eligOf(c).some(x => eligOf(d).includes(x))));
    const c = pool.sort((a, b) => (b[k] || 0) - (a[k] || 0))[0];
    if (!c) continue;
    const gain = (c[k] || 0) - (d[k] || 0);
    if (gain < 3) continue;
    used.add(c.id);
    const why = isG(d)
      ? `About ${f1(nxtFocus() ? c.nxtStarts : c.wkStarts)} starts for ${esc(c.name)}, against ${f1(nxtFocus() ? d.nxtStarts : d.wkStarts)} for ${esc(d.name)}.`
      : `${esc(c.name)} has ${plural(c[gk] || 0, "game")} ${nxtFocus() ? "next week" : "left this week"}, ${esc(d.name)} has ${d[gk] || 0}. It's a short-term stream: swap back later if you like.`;
    out.push({ d, c, gain, unit: nxtFocus() ? "points next week" : "points this week", why });
  }
  return out.sort((a, b) => b.gain - a.gain).slice(0, 4);
}

function moveCard(m) {
  const side = (p, lbl, cls) => `<button class="mside ${cls}" data-pid="${p.id}"><small>${lbl}</small><b>${esc(p.name)}</b>${meta(p)}</button>`;
  return `<div class="move">
    <div class="mtop"><b>+${f0(m.gain)}</b><span>${m.unit}</span></div>
    <div class="swap">${side(m.d, "Drop", "out")}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>${side(m.c, "Add", "in")}</div>
    <p class="why">${m.why}</p>
    <div class="mact"><button class="btn" data-cmp2="${m.d.id},${m.c.id}">Compare them</button><button class="btn ghost" data-taken="${m.c.id}">${esc(m.c.name.split(" ").slice(-1)[0])} isn't available</button></div>
  </div>`;
}

function slotRow(label, p) {
  if (!p) return `<div class="row slotrow empty-slot"><span class="slot s-${label}">${label}</span><span class="who"><b class="muted">Empty</b><span class="meta">Add a ${label === "Util" ? "skater" : label} below</span></span></div>`;
  const s = `${f2(projTG(p))}`;
  return `<button class="row slotrow" data-pid="${p.id}"><span class="slot s-${label}">${label}</span>
    <span class="who"><b>${esc(p.name)}</b>${meta(p)}</span>
    <span class="val"><b>${s}</b><small>${nz(p[gKey()]) ? "" : plural(p[gKey()], "game") + (nxtFocus() ? " next wk" : " left")}</small></span></button>`;
}

function renderTeam() {
  const el = document.getElementById("v-team");
  if (!el) return;
  const L = lineup();
  const tabs = seg("ttab", [["lineup", "Lineup"], ["report", "Report"], ["moves", "Moves"]], teamState.tab, true);
  let body = "";
  if (!team.length) {
    body = `<div class="list" style="margin-top:14px"><div class="empty">Add the players on your Yahoo roster. They're saved on this device, and the app will set your best lineup, grade it, and suggest who to drop and add.<br><br>Tip: you can also tap any player in the app and choose <b>Add to My team</b>.</div></div>`;
  } else if (teamState.tab === "lineup") {
    const starters = L.slots.filter(x => x.p).map(x => x.p);
    const mineWk = starters.reduce((s, p) => s + projTG(p), 0) * GPW;
    const lgWk = SLOTS.reduce((s, sl) => s + (LG[sl] || 0), 0) * GPW;
    const thisWk = starters.reduce((s, p) => s + (p[wkKey()] || 0), 0);
    body = `<div class="tsum"><div><b>${f0(mineWk)}</b><span>points in a typical week</span></div><div><b>${f0(lgWk)}</b><span>typical team in your league</span></div><div><b>${f0(thisWk)}</b><span>${nxtFocus() ? "projected next week" : "left this week"}</span></div></div>
      <h2 class="sub2">Starters</h2>
      <div class="list">${L.slots.map(x => slotRow(x.s, x.p)).join("")}</div>
      <h2 class="sub2">Bench <span class="cnt">${L.bench.length} of ${C.bench}</span></h2>
      ${L.bench.length ? `<div class="list">${L.bench.map(p => slotRow("BN", p)).join("")}</div>` : `<p class="hint">No one on the bench.</p>`}
      <h2 class="sub2">Injured reserve <span class="cnt">${L.ir.length} of ${C.irSlots}</span></h2>
      ${L.ir.length ? `<div class="list">${L.ir.map(p => slotRow("IR", p)).join("")}</div>` : `<p class="hint">Tap a player and choose <b>Move to IR</b> when he's hurt.</p>`}
      <p class="hint" style="margin-top:14px">The app fills your starting spots with your highest projected players. Tap a player to set his Yahoo positions if he qualifies at more than one.</p>`;
  } else if (teamState.tab === "report") {
    const { pros, cons } = prosCons(L);
    body = `<h2 class="sub2">Team shape</h2>${shapeHTML(L)}
      <h2 class="sub2">Strengths</h2>${pros.length ? `<ul class="pc pro">${pros.map(t => `<li>${t}</li>`).join("")}</ul>` : `<p class="hint">Nothing stands out yet.</p>`}
      <h2 class="sub2">Weak spots</h2>${cons.length ? `<ul class="pc con">${cons.map(t => `<li>${t}</li>`).join("")}</ul>` : `<p class="hint">No weak spots found. Nice.</p>`}`;
  } else {
    const ms = teamState.moves === "season" ? seasonMoves(L) : weekMoves(L);
    body = `<div style="margin:16px 0 4px">${seg("tmoves", [["season", "Rest of season"], ["week", nxtFocus() ? "Next week" : "This week"]], teamState.moves)}</div>
      <p class="hint">${teamState.moves === "season"
        ? "Permanent upgrades: your weakest players swapped for better ones who are probably free agents."
        : "Streams: swap a low-value player for someone with more games, then swap back later."}
        It can't see your league's rosters, so check Yahoo first. Tap <b>isn't available</b> to hide a player.</p>
      ${ms.length ? ms.map(moveCard).join("") : `<div class="list"><div class="empty">No clear upgrades right now. Your roster beats what's likely on waivers.</div></div>`}
      ${taken.size ? `<button class="more" data-untake>Show hidden players again (${taken.size})</button>` : ""}`;
  }
  el.innerHTML = `<h1>My team</h1>
    <p class="intro">${team.length ? `${plural(team.length - L.ir.length, "active player")} of ${ROSTER_MAX}${L.ir.length ? `, ${L.ir.length} on IR` : ""}.${fileTeam.version ? " Loaded from your league file. After a trade or pickup, add or remove players here, or ask Claude to update the file." : ""}` : "Build your roster to get a lineup, a report card and moves to make."}</p>
    ${team.length ? tabs : ""}
    <div id="tPick" style="margin-top:14px"></div>
    ${body}`;
  picker(document.getElementById("tPick"), "Add a player to your roster", addToTeam);
}
document.getElementById("v-team").addEventListener("click", e => {
  const s = e.target.closest("[data-seg]");
  if (s) { if (s.dataset.seg === "ttab") teamState.tab = s.dataset.val; else teamState.moves = s.dataset.val; return renderTeam(); }
  const c2 = e.target.closest("[data-cmp2]");
  if (c2) { cmp = c2.dataset.cmp2.split(",").map(Number); store.set("cmp", cmp); renderCompare(); go("compare"); return; }
  const t = e.target.closest("[data-taken]");
  if (t) { taken.add(+t.dataset.taken); saveTeam(); renderTeam(); toast("Hidden from suggestions"); return; }
  if (e.target.closest("[data-untake]")) { taken.clear(); saveTeam(); renderTeam(); }
});

// ---------- GUIDE ----------
function renderGuide() {
  const s = Object.fromEntries(C.skaterScoring);
  const pts = arr => `<div class="pts">${arr.map(([k, v]) => `<span class="${v < 0 ? "neg" : ""}">${k} <b>${v}</b></span>`).join("")}</div>`;
  const slots = Object.entries(C.slots).map(([k, v]) => `${v} ${k}`).join(", ");
  const W = D.week;
  const sec = (title, body, open) => `<details ${open ? "open" : ""}><summary>${title}</summary><div class="gb">${body}</div></details>`;
  document.getElementById("v-guide").innerHTML = `<h1>Guide</h1>
    <p class="intro">How this app works. It rebuilds itself from NHL stats every morning around 5 AM.</p>
    <div class="guide">
    ${sec("Your league's scoring", `${pts(C.skaterScoring)}<div style="height:8px"></div>${pts(C.goalieScoring)}
      <p>Hits and blocks matter a lot in your league. One goal is worth the same as ${f0(s.G / s.HIT)} hits, ${f1(s.G / s.BLK)} blocks or ${f0(s.G / s.SOG)} shots. A defenceman who hits and blocks a lot can outscore a 40-point winger.</p>
      <p>For goalies, one goal against cancels about ${f1(0.85 / 0.2)} saves, so a goalie who faces lots of shots still scores in a loss.</p>`, true)}
    ${sec("The pages", `<dl>
      <dt>This week</dt><dd>Your weekly briefing: which teams play the most, the best pickups, goalie starts, who's hot or cold, and who's getting lucky. On Sundays it switches to next week.</dd>
      <dt>My team</dt><dd>Add your Yahoo roster once (it's saved on this device). The app sets your best lineup, grades each position against a typical team in your league, lists strengths and weak spots, and suggests who to drop and who to add, both for the rest of the season and for this week's schedule.</dd>
      <dt>Rankings</dt><dd>Every player ranked by your scoring. Hidden value shows players your scoring rates very differently from plain NHL points, which is your edge over league-mates.</dd>
      <dt>Compare</dt><dd>Up to four players side by side. The best number in each row is highlighted.</dd>
      <dt>Trade</dt><dd>Line up a trade. The puck slides toward whoever comes out ahead.</dd>
      <dt>Player cards</dt><dd>Tap any player, anywhere, to see his card, send him to Compare or Trade, or add him to My team.</dd></dl>`)}
    ${sec("What the numbers mean", `<dl>
      <dt>Points (FP)</dt><dd>Fantasy points with your league's scoring.</dd>
      <dt>Projected per game</dt><dd>What a player should score per game for the rest of the season. See "How projections work".</dd>
      <dt>Points left this season</dt><dd>Projected per game times the games left on his team's schedule. It assumes he plays every game.</dd>
      <dt>Value over a free agent</dt><dd>How many more points he'll score than the best player you could pick up for free at his position. With ${C.leagueTeams} teams starting ${slots}, that's the first player outside the starters. This is the fairest way to compare positions and judge trades. In trades, anyone below free-agent level counts as zero.</dd>
      <dt>The small bar</dt><dd>Blue is points from goals and assists. Gold is points from shots, hits and blocks. Gold is steady week to week; blue is streaky.</dd>
      <dt>Shooting luck</dt><dd>Goals divided by shots, compared with his usual rate (last season with 60+ shots, otherwise the league average for his position). It appears once he has 30 shots.</dd>
      <dt>Share of starts</dt><dd>How often a goalie starts his team's games. Expected starts are team games times that share.</dd>
      <dt>Your playoff weeks</dt><dd>${W.ps ? `${fd(W.ps)} to ${fd(W.pe)}` : "Not set"}. By default the last three weeks of the NHL season. Change it in the script's settings to match your league.</dd></dl>`)}
    ${sec("How projections work", `<p>Early-season numbers are noisy, so each projection blends three things:</p>
      <div class="formula"><b>This season</b>, weighted by games played<br><b>Last season</b>, counted as ${C.priorGames} games (rookies get a cautious baseline)<br><b>The last ${C.recentDays} days</b>, which nudge the result by ${Math.round(C.recentWeight * 100)}%</div>
      <p>As the season goes on, this season's numbers take over. Goalies work the same way per start (last season counts as ${C.goaliePriorStarts} starts), then get multiplied by their share of starts.</p>`)}
    ${sec("What it can't see yet", `<ul>
      <li>Injuries, line changes and confirmed starting goalies. Check Yahoo or Daily Faceoff before you set your lineup.</li>
      <li>Yahoo positions for every player. Your roster's positions come from your league file. For anyone else it uses his NHL position, and you can fix it on his player card.</li>
      <li>Your league's rosters. Pickups and suggested moves assume players ranked outside the top ${C.leagueTeams * C.rosteredPerTeam} are available. Mark anyone who isn't, and the app will skip him.</li>
      <li>Your phone and computer keep separate copies of My team, Compare and Trade.</li>
      ${C.hasLast ? "" : "<li>Last season's stats didn't load on today's update, so projections use this season only.</li>"}
      ${C.hasRecent ? "" : "<li>Recent stats didn't load on today's update, so the Form lists are empty.</li>"}
      ${C.hasSchedule ? "" : "<li>Schedules didn't load on today's update, so weekly numbers are estimates.</li>"}</ul>`)}
    ${sec("Settings", `<p>League size, roster spots, scoring and playoff dates are at the top of the script, under "YOUR LEAGUE SETTINGS". Ask Claude to change them, or edit the file on GitHub.</p>
      <p style="color:var(--muted);font-size:13px">Data from the NHL's public stats. For personal use.</p>`)}
    </div>`;
}

// ---------- start ----------
renderWeek(); renderTeam(); renderRank(); renderCompare(); renderTrade(); renderGuide();
const saved = store.get("view", "week");
go(VIEWS.includes(saved) ? saved : "week");
</script>
</body>
</html>
"""


# ===========================================================================
# MAIN
# ===========================================================================
def norm_name(name):
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def load_league_file(path=None):
    """my_league.json: Yahoo positions and your roster. Optional - the app works without it."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), LEAGUE_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log(f"  {LEAGUE_FILE}: couldn't read it ({e}), skipping")
        return {}


def apply_league_file(league, skaters, goalies):
    by_name = {}
    for p in sorted(skaters + goalies, key=lambda x: x["gp"]):
        by_name[norm_name(p["name"])] = p   # most games wins if two players share a name
    valid = {"C", "LW", "RW", "D"}
    set_count = 0
    for name, pos in (league.get("positions") or {}).items():
        p = by_name.get(norm_name(name))
        pos = [x.strip().upper() for x in (pos if isinstance(pos, list) else str(pos).split(","))]
        pos = [x for x in pos if x in valid]
        if p and p["pos"] != "G" and pos:
            p["elig"] = pos
            set_count += 1
        elif not p:
            log(f"  {LEAGUE_FILE}: no current player named '{name}' (positions)")
    team = league.get("myTeam") or {}
    roster, ir = [], []
    for key, out in (("roster", roster), ("ir", ir)):
        for name in team.get(key, []) or []:
            p = by_name.get(norm_name(name))
            if p:
                out.append(p["id"])
            else:
                log(f"  {LEAGUE_FILE}: no current player named '{name}' (myTeam.{key})")
    for pid in ir:
        if pid not in roster:
            roster.append(pid)
    version = ""
    if roster:
        import hashlib
        version = hashlib.sha1(json.dumps([roster, ir]).encode()).hexdigest()[:10]
    if league:
        log(f"  {LEAGUE_FILE}: Yahoo positions for {set_count} players, roster of {len(roster)} ({len(ir)} on IR)")
    return {"roster": roster, "ir": ir, "version": version}


def build(raw, season_id, min_gp, out_dir, today=None, open_browser=True, league=None):
    today = today or local_now().date()
    teams, week = analyze_schedule(raw.get("schedules", {}), today)
    skaters, repl = build_skaters(raw, teams, min_gp)
    goalies, repl_g = build_goalies(raw, teams, min_gp)
    repl["G"] = repl_g
    pulse = build_pulse(skaters, goalies, week)
    my_team = apply_league_file(load_league_file() if league is None else league, skaters, goalies)

    os.makedirs(out_dir, exist_ok=True)
    write_csv(os.path.join(out_dir, "skaters.csv"), skaters, SKATER_CSV_COLS, "proj")
    write_csv(os.path.join(out_dir, "goalies.csv"), goalies, GOALIE_CSV_COLS, "projTG")
    html_path = os.path.join(out_dir, "fp_rankings.html")
    payload = {
        "season": season_label(season_id),
        "generated": local_now().strftime("%b %d, %Y %I:%M %p"),
        "minGp": min_gp,
        "week": week,
        "teams": teams,
        "pulse": pulse,
        "myTeam": my_team,
        "config": {
            "skaterScoring": [[LEAGUE_LABELS[k], v] for k, v in SKATER_SCORING.items()],
            "goalieScoring": [[LEAGUE_LABELS[k], v] for k, v in GOALIE_SCORING.items()],
            "leagueTeams": LEAGUE_TEAMS, "slots": ROSTER_SLOTS, "rosteredPerTeam": SKATERS_ROSTERED_PER_TEAM,
            "bench": BENCH_SLOTS, "irSlots": IR_SLOTS,
            "recentDays": RECENT_DAYS, "recentWeight": RECENT_WEIGHT,
            "priorGames": PRIOR_GAMES, "goaliePriorStarts": GOALIE_PRIOR_STARTS,
            "repl": {k: round(v, 3) for k, v in repl.items()},
            "hasLast": bool(raw.get("lastSummary")), "hasRecent": bool(raw.get("recentSummary")),
            "hasSchedule": bool(raw.get("schedules")),
        },
        "skaters": slim(skaters),
        "goalies": slim(goalies),
    }
    write_html(html_path, payload)

    q = sorted([p for p in skaters if p["q"]], key=lambda x: x["projRank"])
    log(f"\nTop 15 skaters by projected FP/game (min {min_gp} GP):")
    log(f"{'#':>3}  {'Player':<24}{'Pos':<4}{'Team':<6}{'GP':>4}{'FP/GP':>7}{'Proj':>7}{'ROS FP':>8}")
    for p in q[:15]:
        log(f"{p['projRank']:>3}  {p['name'][:23]:<24}{p['pos']:<4}{p['team'][:5]:<6}"
            f"{int(p['gp']):>4}{p['fpg']:>7.2f}{p['proj']:>7.2f}{p['rosFp']:>8.0f}")
    log("\nTop 5 goalies by projected FP per team game:")
    for g in sorted([g for g in goalies if g["q"]], key=lambda x: x["projRank"])[:5]:
        log(f"{g['projRank']:>3}  {g['name'][:23]:<24}{g['team'][:5]:<6}{int(g['gs']):>4} GS"
            f"  {g['proj']:.2f}/start  {g['share']*100:.0f}% starts")
    log(f"\nSaved:\n  {os.path.abspath(html_path)}\n  {os.path.abspath(os.path.join(out_dir, 'skaters.csv'))}"
        f"\n  {os.path.abspath(os.path.join(out_dir, 'goalies.csv'))}")
    if open_browser:
        webbrowser.open("file://" + os.path.abspath(html_path))
    return payload


def main():
    ap = argparse.ArgumentParser(description="Rank NHL players by your Yahoo league's fantasy points.")
    ap.add_argument("--season", default=current_season_id(), help="Season id, e.g. 20262027")
    ap.add_argument("--min-gp", type=int, default=5, help="Minimum games played to be ranked (default 5)")
    ap.add_argument("--out", default="fp_output", help="Output folder (default fp_output)")
    ap.add_argument("--no-open", action="store_true", help="Don't open the report in a browser")
    args = ap.parse_args()

    today = local_now().date()
    try:
        raw = fetch_all(args.season, today)
    except RuntimeError as e:
        log(f"\nERROR: {e}\nCheck your internet connection and try again.")
        sys.exit(1)
    if not raw["summary"]:
        log("\nNo stats found for that season yet. Try last season, e.g. --season 20252026")
        sys.exit(1)
    build(raw, args.season, args.min_gp, args.out, today=today, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
