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
ROSTER_SLOTS = {"C": 2, "LW": 2, "RW": 2, "D": 4, "G": 2}  # starting slots per team
SKATERS_ROSTERED_PER_TEAM = 14                            # starters + bench skaters (for "probably available")

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

    repl = {}
    for pos in ("C", "LW", "RW", "D"):
        pool = sorted((p["proj"] for p in q if p["pos"] == pos), reverse=True)
        idx = LEAGUE_TEAMS * ROSTER_SLOTS.get(pos, 2)
        repl[pos] = pool[min(idx, len(pool) - 1)] if pool else 0.0
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
<meta name="theme-color" content="#0b0f17">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="FP Hockey">
<title>FP Hockey</title>
<style>
  :root{
    --bg:#0b0f17; --panel:#121826; --panel2:#182033; --line:#243049; --line2:#1a2236;
    --text:#e8eef9; --muted:#8a97b0; --ice:#cfe3ff;
    --scoring:#4da3ff; --peri:#ffb547; --good:#3ddc97; --bad:#ff6b6b;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.45 "Inter","Segoe UI",system-ui,-apple-system,sans-serif}
  button{font:inherit;color:inherit}
  header{padding:22px 32px 14px;background:linear-gradient(180deg,#111a2b 0%,var(--bg) 100%)}
  .eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
  h1{margin:2px 0 4px;font-size:26px;font-weight:800;letter-spacing:-.02em}
  h1 span{color:var(--scoring)}
  .meta{color:var(--muted);font-size:12px}
  nav.tabs{display:flex;gap:4px;padding:8px 32px 0;border-bottom:1px solid var(--line)}
  nav.tabs button{background:none;border:none;color:var(--muted);font-weight:700;
    padding:10px 16px;border-radius:10px 10px 0 0;cursor:pointer;font-size:14px}
  nav.tabs button.active{background:var(--panel);color:var(--text)}
  main{padding:18px 32px 60px}
  section[hidden]{display:none}
  h2{font-size:18px;margin:0 0 4px}
  .lede{color:var(--muted);font-size:13px;margin:0 0 16px;max-width:760px}

  /* cards */
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--line2);border-radius:14px;padding:14px 14px 8px}
  .card h3{margin:0;font-size:15px;display:flex;align-items:center;gap:8px}
  .card .desc{color:var(--muted);font-size:12px;margin:3px 0 10px}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}
  .prow{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line2)}
  .prow .pn{flex:1;min-width:0}
  .prow .pn b{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .prow .pn span{font-size:11px;color:var(--muted)}
  .prow .pv{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .prow .pv small{display:block;color:var(--muted);font-size:11px}
  .add{flex:none;width:30px;height:30px;border-radius:8px;border:1px solid var(--line);
    background:var(--panel2);color:var(--ice);cursor:pointer;font-weight:700}
  .add:active{background:var(--scoring);color:#06101f}
  .empty{color:var(--muted);font-size:12px;padding:10px 0;border-top:1px solid var(--line2)}
  .sgrp{display:flex;gap:10px;align-items:flex-start;padding:7px 0;border-top:1px solid var(--line2)}
  .sn{flex:none;width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-weight:800;
    background:var(--panel2);font-size:16px}
  .sn.hi{background:#0f3a2a;color:var(--good)} .sn.lo{background:#3a1717;color:var(--bad)}
  .st{display:flex;flex-wrap:wrap;gap:5px;padding-top:5px}
  .team{font-size:11px;font-weight:700;padding:2px 6px;border-radius:5px;background:var(--panel2);color:var(--ice)}
  .up{color:var(--good);font-weight:700} .down{color:var(--bad);font-weight:700}
  .muted{color:var(--muted)}

  /* controls */
  .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px}
  input[type=search]{background:var(--panel2);border:1px solid var(--line);color:var(--text);
    border-radius:9px;padding:9px 12px;font:inherit;min-width:220px;outline:none}
  input[type=search]:focus{border-color:var(--scoring)}
  .seg{display:flex;background:var(--panel2);border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .seg button{background:none;border:none;color:var(--muted);font-weight:600;padding:8px 13px;cursor:pointer}
  .seg button.active{background:var(--scoring);color:#06101f}
  .legend{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-left:auto}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}

  /* tables */
  .scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th{background:var(--bg);text-align:right;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
    color:var(--muted);padding:8px 8px;border-bottom:1px solid var(--line);cursor:pointer;white-space:nowrap;user-select:none}
  th.l,td.l{text-align:left}
  th.sorted{color:var(--text)}
  th.sorted::after{content:" \25BE"} th.sorted.asc::after{content:" \25B4"}
  td{padding:7px 8px;border-bottom:1px solid var(--line2);text-align:right;white-space:nowrap}
  tbody tr:hover td{background:#16203a}
  td.name{font-weight:700;cursor:pointer}
  td.name:hover{color:var(--scoring)}
  td.fpg{font-weight:800;color:var(--ice)}
  .sub{display:none;font-weight:400;font-size:11px;color:var(--muted)}
  .pos{display:inline-block;min-width:28px;text-align:center;font-size:11px;font-weight:700;
    border-radius:4px;padding:1px 5px;background:var(--panel2);color:var(--ice)}
  .pos.D{background:#3a2a10;color:var(--peri)} .pos.G{background:#2a1a3a;color:#d4a7ff}
  .bar{display:flex;height:10px;width:130px;background:#0e1422;border-radius:3px;overflow:hidden}
  .bar .s{background:var(--scoring)} .bar .p{background:var(--peri)}
  .hv{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  .hv h3{margin:4px 0 2px;font-size:15px}
  .hv p{margin:0 0 10px;color:var(--muted);font-size:12px}
  .note{color:var(--muted);font-size:12px;margin-top:12px}

  /* picker */
  .picker{position:relative}
  .picker input{width:100%}
  .sugg{position:absolute;z-index:20;left:0;right:0;top:calc(100% + 4px);background:var(--panel2);
    border:1px solid var(--line);border-radius:10px;overflow:hidden;box-shadow:0 12px 30px rgba(0,0,0,.5)}
  .sugg button{display:block;width:100%;text-align:left;background:none;border:none;border-bottom:1px solid var(--line2);
    padding:9px 12px;cursor:pointer}
  .sugg button:hover,.sugg button:active{background:#22304d}
  .sugg button span{display:block;font-size:11px;color:var(--muted)}
  .sugg .none{padding:10px 12px;color:var(--muted)}

  /* compare */
  .cmpwrap{max-width:900px}
  .cmp th{text-align:center;cursor:default;vertical-align:bottom;background:var(--panel);text-transform:none;letter-spacing:0;
    font-size:13px;color:var(--text);padding:10px 8px}
  .cmp th:first-child,.cmp td:first-child{text-align:left;color:var(--muted);font-size:12px;white-space:normal;min-width:120px}
  .cmp td{text-align:center}
  .cmp td.best{color:var(--good);font-weight:800}
  .cmp .ph b{display:block}
  .cmp .ph span{display:block;font-size:11px;color:var(--muted);font-weight:400}
  .mini{display:flex;gap:4px;justify-content:center;margin-top:6px}
  .mini button{border:1px solid var(--line);background:var(--panel2);border-radius:6px;padding:2px 7px;font-size:11px;cursor:pointer}
  .mini button.x{color:var(--bad)}
  .cmp .bar{margin:0 auto;width:90px}

  /* trade */
  .tgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px;max-width:980px}
  .side h3{margin:0 0 8px;font-size:14px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
  .side .tl{min-height:20px;margin-top:8px}
  .verdict{max-width:980px;margin-top:14px;background:linear-gradient(135deg,#132036,#121826);border:1px solid var(--line);
    border-radius:16px;padding:18px}
  .vhead{font-size:24px;font-weight:800;letter-spacing:-.01em}
  .vsub{color:var(--muted);margin-top:2px}
  .meter{position:relative;height:12px;border-radius:6px;margin:16px 0 6px;
    background:linear-gradient(90deg,var(--bad),#3a4560 45%,#3a4560 55%,var(--good))}
  .meter i{position:absolute;top:-5px;width:4px;height:22px;background:#fff;border-radius:2px;transform:translateX(-2px);
    box-shadow:0 0 0 3px rgba(0,0,0,.35)}
  .mlab{display:flex;justify-content:space-between;font-size:11px;color:var(--muted)}
  .vt{margin-top:14px}
  .vt td:first-child,.vt th:first-child{text-align:left}
  .vt th{cursor:default;background:none}
  .insights{margin:14px 0 0;padding-left:18px}
  .insights li{margin:5px 0}
  .btns{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
  .btn{border:1px solid var(--line);background:var(--panel2);border-radius:9px;padding:8px 14px;cursor:pointer;font-weight:600}
  .btn.primary{background:var(--scoring);color:#06101f;border-color:var(--scoring)}

  /* guide */
  .guide{max-width:780px}
  .guide h3{margin:26px 0 6px;font-size:16px}
  .guide p,.guide li{color:#c9d3e6}
  .guide dl{margin:0}
  .guide dt{font-weight:800;margin-top:12px}
  .guide dd{margin:2px 0 0;color:#c9d3e6}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:12px;color:var(--ice)}
  .chip b{color:var(--text)} .chip.neg b{color:var(--bad)}
  code,.formula{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:2px 6px;font-size:12.5px}
  .formula{display:block;padding:10px 12px;margin:8px 0;white-space:normal;line-height:1.6}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px}

  .toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);opacity:0;
    background:#22304d;color:var(--text);padding:10px 16px;border-radius:10px;font-weight:600;
    transition:all .2s;pointer-events:none;z-index:50;box-shadow:0 8px 24px rgba(0,0,0,.5)}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

  @media (max-width:1000px){ .hv{grid-template-columns:1fr} header,nav.tabs,main{padding-left:14px;padding-right:14px} }
  @media (max-width:700px){
    body{padding-bottom:calc(64px + env(safe-area-inset-bottom))}
    header{padding-top:calc(14px + env(safe-area-inset-top));padding-bottom:10px}
    h1{font-size:22px}
    nav.tabs{position:fixed;left:0;right:0;bottom:0;z-index:40;padding:0 0 env(safe-area-inset-bottom);
      background:rgba(18,24,38,.97);border-top:1px solid var(--line);border-bottom:none;gap:0;backdrop-filter:blur(10px)}
    nav.tabs button{flex:1;border-radius:0;padding:13px 0 12px;font-size:12px}
    nav.tabs button.active{background:none;color:var(--scoring);box-shadow:inset 0 2px 0 var(--scoring)}
    main{padding:12px 10px 30px}
    .grid{grid-template-columns:1fr}
    .hs{display:none}
    .sub{display:block}
    input[type=search]{min-width:0;width:100%;font-size:16px}
    .controls .seg{width:100%} .controls .seg button{flex:1;padding:9px 0}
    .legend{margin-left:0;font-size:11px;gap:10px}
    td,th{padding:9px 5px}
    .bar{width:64px}
    .tgrid{grid-template-columns:1fr}
    .vhead{font-size:20px}
    .toast{bottom:calc(76px + env(safe-area-inset-bottom))}
  }
</style>
</head>
<body>
<header>
  <div class="eyebrow">Yahoo points league</div>
  <h1>FP <span>Hockey</span></h1>
  <div class="meta" id="meta"></div>
</header>
<nav class="tabs">
  <button data-tab="pulse" class="active">Pulse</button>
  <button data-tab="rank">Rankings</button>
  <button data-tab="compare">Compare</button>
  <button data-tab="trade">Trade</button>
  <button data-tab="guide">Guide</button>
</nav>
<main>
  <section id="tab-pulse">
    <h2 id="pulseTitle">Weekly Pulse</h2>
    <p class="lede" id="pulseLede"></p>
    <div class="grid" id="pulseGrid"></div>
  </section>

  <section id="tab-rank" hidden>
    <div class="controls">
      <div class="seg" id="rankSeg">
        <button data-v="sk" class="active">Skaters</button>
        <button data-v="g">Goalies</button>
        <button data-v="hv">Hidden Value</button>
      </div>
    </div>
    <div id="rank-sk">
      <div class="controls">
        <input type="search" id="skSearch" placeholder="Search player or team...">
        <div class="seg" id="posSeg">
          <button data-pos="ALL" class="active">All</button><button data-pos="F">F</button>
          <button data-pos="C">C</button><button data-pos="LW">LW</button>
          <button data-pos="RW">RW</button><button data-pos="D">D</button>
        </div>
        <div class="legend"><span><i style="background:var(--scoring)"></i>Scoring</span>
          <span><i style="background:var(--peri)"></i>Peripherals (SOG, HIT, BLK)</span></div>
      </div>
      <div class="scroll"><table id="skTable"></table></div>
      <div class="note">Tap a name to add the player to Compare. Sorted by projected FP per game; tap any column header to re-sort.</div>
    </div>
    <div id="rank-g" hidden>
      <div class="controls"><input type="search" id="gSearch" placeholder="Search goalie or team..."></div>
      <div class="scroll"><table id="gTable"></table></div>
      <div class="note">Goalies are ranked by projected FP per <i>team</i> game (FP per start &times; share of starts), so a 1A and a backup compare fairly.</div>
    </div>
    <div id="rank-hv" hidden>
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
    </div>
  </section>

  <section id="tab-compare" hidden>
    <h2>Compare players</h2>
    <p class="lede">Add up to 4 players. Green marks the best value in each row. Tap <b>Give</b> or <b>Get</b> to send a player to the Trade page.</p>
    <div class="cmpwrap">
      <div class="controls"><div class="picker" id="cmpPicker" style="flex:1"></div>
        <button class="btn" id="cmpClear">Clear</button></div>
      <div class="scroll"><table class="cmp" id="cmpTable"></table></div>
    </div>
  </section>

  <section id="tab-trade" hidden>
    <h2>Trade analyzer</h2>
    <p class="lede">Add the players you'd give and the players you'd get. The verdict uses rest-of-season value over replacement, so it accounts for position scarcity and roster spots.</p>
    <div class="tgrid">
      <div class="card side"><h3>You give</h3><div class="picker" id="givePicker"></div><div class="tl" id="giveList"></div></div>
      <div class="card side"><h3>You get</h3><div class="picker" id="getPicker"></div><div class="tl" id="getList"></div></div>
    </div>
    <div class="verdict" id="verdict"></div>
  </section>

  <section id="tab-guide" hidden>
    <div class="guide" id="guide"></div>
  </section>
</main>
<div class="toast" id="toast"></div>

<script>
const D = __DATA__;
const C = D.config;
const SK = D.skaters, GO = D.goalies;
const P = new Map([...SK, ...GO].map(p => [p.id, p]));
const isG = p => p.pos === "G";
const projTG = p => isG(p) ? p.projTG : p.proj;

// ---------- helpers ----------
const nz = v => v === null || v === undefined || Number.isNaN(v);
const f0 = v => nz(v) ? "—" : Math.round(v).toLocaleString();
const f1 = v => nz(v) ? "—" : Number(v).toFixed(1);
const f2 = v => nz(v) ? "—" : Number(v).toFixed(2);
const f3 = v => nz(v) ? "—" : Number(v).toFixed(3).replace(/^0/, "");
const pct = v => nz(v) ? "—" : Math.round(v) + "%";
const sgn = (v, f = f1) => nz(v) ? "—" : (v > 0 ? "+" : "") + f(v);
const pm = v => nz(v) ? "—" : (Math.round(v) > 0 ? "+" : "") + Math.round(v);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fd = s => { if (!s) return "—"; const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-CA", { month: "short", day: "numeric" }); };
const store = {
  get(k, d) { try { const v = localStorage.getItem("fp:" + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("fp:" + k, JSON.stringify(v)); } catch (e) {} },
};
let toastT;
function toast(msg) {
  const t = document.getElementById("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 1600);
}
const posTag = v => `<span class="pos ${v}">${v}</span>`;
const nameCell = (v, r) => `${esc(v)}<div class="sub">${esc(r.team)} · ${r.pos}</div>`;
const maxBar = Math.max(0.01, ...SK.filter(p => p.q).map(p => Math.max(0, p.fpgS) + Math.max(0, p.fpgP)));
const splitBar = (_, r) => {
  const s = Math.max(0, r.fpgS) / maxBar * 100, p = Math.max(0, r.fpgP) / maxBar * 100;
  return `<div class="bar" title="Scoring ${f2(r.fpgS)} / Peripherals ${f2(r.fpgP)} per game"><div class="s" style="width:${s}%"></div><div class="p" style="width:${p}%"></div></div>`;
};

document.getElementById("meta").textContent =
  `${D.season} · updated ${D.generated} · ${SK.filter(p => p.q).length} skaters, ${GO.filter(g => g.q).length} goalies ranked`;

// ---------- tabs ----------
const TABS = ["pulse", "rank", "compare", "trade", "guide"];
function showTab(name) {
  document.querySelectorAll("nav.tabs button").forEach(x => x.classList.toggle("active", x.dataset.tab === name));
  TABS.forEach(t => document.getElementById("tab-" + t).hidden = t !== name);
  store.set("tab", name);
  window.scrollTo(0, 0);
}
document.querySelectorAll("nav.tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));

// ---------- PULSE ----------
function prow(p, right, sub) {
  return `<div class="prow"><div class="pn"><b>${esc(p.name)}</b><span>${esc(p.team)} · ${p.pos}${sub ? " · " + sub : ""}</span></div>
    <div class="pv">${right}</div><button class="add" data-add="${p.id}" title="Add to Compare" aria-label="Add to Compare">+</button></div>`;
}
function card(color, title, desc, body) {
  return `<div class="card"><h3><span class="dot" style="background:${color}"></span>${title}</h3><div class="desc">${desc}</div>${body}</div>`;
}
function groupTeams(key) {
  const g = {};
  Object.entries(D.teams).forEach(([t, i]) => { const n = i[key]; if (n === null || n === undefined) return; (g[n] = g[n] || []).push(t); });
  const keys = Object.keys(g).map(Number).sort((a, b) => b - a);
  if (!keys.length) return `<div class="empty">Schedule not available.</div>`;
  return keys.map(n => `<div class="sgrp"><div class="sn ${n >= 4 ? "hi" : n <= 1 ? "lo" : ""}">${n}</div>
    <div class="st">${g[n].sort().map(t => `<span class="team">${t}</span>`).join("")}</div></div>`).join("");
}
function renderPulse() {
  const W = D.week, nxt = W.focus === "next";
  const fw = nxt ? `next week (${fd(W.nmon)} – ${fd(W.nsun)})` : `this week (rest of ${fd(W.mon)} – ${fd(W.sun)})`;
  const wkFp = p => nxt ? p.nxtFp : p.wkFp, wkG = p => nxt ? p.nxt : p.wk;
  document.getElementById("pulseTitle").textContent = `Weekly Pulse · ${fd(W.mon)} – ${fd(W.sun)}`;
  document.getElementById("pulseLede").innerHTML =
    `What matters for ${fw}. More games = more fantasy points, so start with the schedule. Tap <b>+</b> on any player to add them to Compare.`;
  const L = ids => ids.map(id => P.get(id)).filter(Boolean);
  const list = (ids, fn) => { const xs = L(ids); return xs.length ? xs.map(fn).join("") : `<div class="empty">Nothing to show yet — needs a few more games of data.</div>`; };
  const cards = [
    card("var(--good)", "Games left this week", `${fd(W.today)} – ${fd(W.sun)}. Teams with 4 are gold for streaming; 1–2 means bench or swap.`, groupTeams("wk")),
    card("var(--scoring)", "Next week", `${fd(W.nmon)} – ${fd(W.nsun)}. Plan your Sunday-night adds around this.`, groupTeams("nxt")),
    card("var(--peri)", "Stream targets", `Projected FP for ${nxt ? "next week" : "the rest of this week"} among players ranked outside the top ${D.pulse.streamCut} (probably free agents in a ${C.leagueTeams}-team league — check Yahoo).`,
      list(D.pulse.stream, p => prow(p, `<b>${f1(wkFp(p))}</b> FP<small>${wkG(p)} games · ${f2(p.proj)}/gm</small>`, `${pct(p.peri)} peri`))),
    card("#d4a7ff", "Goalie starts", `Projected starts and FP for ${nxt ? "next week" : "the rest of this week"}. Starts = team games × his share of starts.`,
      list(D.pulse.goalies, g => prow(g, `<b>${f1(wkFp(g))}</b> FP<small>~${f1(nxt ? g.nxtStarts : g.wkStarts)} starts · ${f2(g.proj)}/start</small>`, `${pct(g.share * 100)} of starts`))),
    card("var(--good)", "Heating up", `FP per game over the last ${C.recentDays} days vs. their season average.`,
      list(D.pulse.hot, p => prow(p, `<span class="up">${sgn(p.recFpg - p.fpg, f2)}</span><small>${f2(p.recFpg)} vs ${f2(p.fpg)}</small>`, `${p.recGp} GP`))),
    card("var(--bad)", "Cooling off", `Biggest drop from season average over the last ${C.recentDays} days.`,
      list(D.pulse.cold, p => prow(p, `<span class="down">${sgn(p.recFpg - p.fpg, f2)}</span><small>${f2(p.recFpg)} vs ${f2(p.fpg)}</small>`, `${p.recGp} GP`))),
    card("var(--bad)", "Sell high — shooting luck", "Scoring on a much higher % of shots than normal for them. Goals usually slow down. Good time to trade them.",
      list(D.pulse.sell, p => prow(p, `<b>${f1(p.sh)}%</b> Sh<small>normal ${f1(p.shBase)}%</small>`, `${f0(p.sog)} shots`))),
    card("var(--good)", "Buy low — due to bounce back", "Shooting well below their normal %. If the shots are there, goals usually follow.",
      list(D.pulse.buy, p => prow(p, `<b>${f1(p.sh)}%</b> Sh<small>normal ${f1(p.shBase)}%</small>`, `${f0(p.sog)} shots`))),
  ];
  document.getElementById("pulseGrid").innerHTML = cards.join("");
}
document.addEventListener("click", e => { const b = e.target.closest("[data-add]"); if (b) addCompare(+b.dataset.add); });

// ---------- sortable table ----------
function makeTable(el, cols, getRows, defaultSort, defaultAsc = false) {
  const state = { key: defaultSort, asc: defaultAsc };
  const ascFirst = new Set(["rank", "projRank", "posRank", "name", "team", "pos"]);
  function render() {
    const rows = getRows().slice().sort((a, b) => {
      let x = a[state.key], y = b[state.key];
      if (typeof x === "string" || typeof y === "string") return state.asc ? String(x).localeCompare(y) : String(y).localeCompare(x);
      x = nz(x) ? (state.asc ? Infinity : -Infinity) : x; y = nz(y) ? (state.asc ? Infinity : -Infinity) : y;
      return state.asc ? x - y : y - x;
    });
    const head = "<tr>" + cols.map(c => {
      const k = c.sort || c.key;
      const cls = [c.hs ? "hs" : "", c.left ? "l" : "", k === state.key ? "sorted" : "", k === state.key && state.asc ? "asc" : ""].join(" ");
      return `<th class="${cls}" data-k="${k}" title="${esc(c.tip || "")}">${c.label}</th>`;
    }).join("") + "</tr>";
    const body = rows.map(r => `<tr data-id="${r.id}">` + cols.map(c =>
      `<td class="${c.hs ? "hs" : ""} ${c.left ? "l" : ""} ${c.cls || ""}">${c.fmt ? c.fmt(r[c.key], r) : esc(r[c.key])}</td>`
    ).join("") + "</tr>").join("");
    el.innerHTML = `<thead>${head}</thead><tbody>${body || `<tr><td class="l muted" colspan="${cols.length}">No players match.</td></tr>`}</tbody>`;
    el.querySelectorAll("th").forEach(th => th.onclick = () => {
      const k = th.dataset.k;
      if (state.key === k) state.asc = !state.asc; else { state.key = k; state.asc = ascFirst.has(k); }
      render();
    });
  }
  el.addEventListener("click", e => { const td = e.target.closest("td.name"); if (td) addCompare(+td.parentElement.dataset.id); });
  render();
  return render;
}

// ---------- RANKINGS ----------
document.querySelectorAll("#rankSeg button").forEach(b => b.onclick = () => {
  document.querySelectorAll("#rankSeg button").forEach(x => x.classList.toggle("active", x === b));
  ["sk", "g", "hv"].forEach(v => document.getElementById("rank-" + v).hidden = v !== b.dataset.v);
});
let skPos = "ALL", skQuery = "";
const skCols = [
  { key: "projRank", label: "#", tip: "Rank by projected FP per game" },
  { key: "name", label: "Player", left: true, cls: "name", fmt: nameCell },
  { hs: true, key: "team", label: "Team", left: true },
  { key: "pos", label: "Pos", left: true, fmt: posTag },
  { hs: true, key: "gp", label: "GP", fmt: f0 },
  { hs: true, key: "g", label: "G", fmt: f0 },
  { hs: true, key: "a", label: "A", fmt: f0 },
  { hs: true, key: "ppp", label: "PPP", fmt: f0 },
  { hs: true, key: "sog", label: "SOG", fmt: f0 },
  { hs: true, key: "hit", label: "HIT", fmt: f0 },
  { hs: true, key: "blk", label: "BLK", fmt: f0 },
  { hs: true, key: "pm", label: "+/-", fmt: pm },
  { hs: true, key: "fp", label: "FP", fmt: f1 },
  { hs: true, key: "fpg", label: "FP/GP", fmt: f2, tip: "Season fantasy points per game" },
  { hs: true, key: "recFpg", label: "L" + C.recentDays, fmt: f2, tip: `FP per game, last ${C.recentDays} days` },
  { key: "proj", label: "Proj", fmt: f2, cls: "fpg", tip: "Projected FP per game for the rest of the season" },
  { key: "split", sort: "fpgP", label: "Split", left: true, fmt: splitBar, tip: "Blue = scoring, orange = peripherals. Sorts by peripheral FP/GP" },
  { hs: true, key: "peri", label: "Peri %", fmt: pct, tip: "Share of FP from SOG/HIT/BLK - higher = steadier" },
  { hs: true, key: "rosFp", label: "ROS FP", fmt: f0, tip: "Projected FP for the rest of the season" },
  { hs: true, key: "rosVor", label: "VOR", fmt: v => sgn(v, f0), tip: "Rest-of-season value over a replacement-level player at his position" },
];
const isF = p => p === "C" || p === "LW" || p === "RW";
const renderSk = makeTable(document.getElementById("skTable"), skCols, () => SK.filter(p => p.q &&
  (skPos === "ALL" || p.pos === skPos || (skPos === "F" && isF(p.pos))) &&
  (!skQuery || p.name.toLowerCase().includes(skQuery) || p.team.toLowerCase().includes(skQuery))), "proj");
document.getElementById("skSearch").oninput = e => { skQuery = e.target.value.trim().toLowerCase(); renderSk(); };
document.querySelectorAll("#posSeg button").forEach(b => b.onclick = () => {
  skPos = b.dataset.pos; document.querySelectorAll("#posSeg button").forEach(x => x.classList.toggle("active", x === b)); renderSk();
});

let gQuery = "";
const gCols = [
  { key: "projRank", label: "#" },
  { key: "name", label: "Goalie", left: true, cls: "name", fmt: nameCell },
  { hs: true, key: "team", label: "Team", left: true },
  { hs: true, key: "gp", label: "GP", fmt: f0 },
  { hs: true, key: "gs", label: "GS", fmt: f0 },
  { hs: true, key: "w", label: "W", fmt: f0 },
  { hs: true, key: "svPct", label: "SV%", fmt: f3 },
  { hs: true, key: "so", label: "SHO", fmt: f0 },
  { hs: true, key: "saFor", label: "Shots/GP", fmt: f1, tip: "Shots faced per game - workload" },
  { hs: true, key: "fpg", label: "FP/GP", fmt: f2 },
  { hs: true, key: "noWin", label: "No-win", fmt: f2, tip: "FP per game from saves and GA only - his floor" },
  { key: "proj", label: "Proj/Start", fmt: f2, cls: "fpg", tip: "Projected FP per start" },
  { key: "share", label: "Starts", fmt: v => pct(v * 100), tip: "Share of his team's games he starts" },
  { hs: true, key: "projTG", label: "Proj/Team GP", fmt: f2 },
  { key: "rosFp", label: "ROS FP", fmt: f0 },
  { hs: true, key: "rosVor", label: "VOR", fmt: v => sgn(v, f0) },
];
const renderG = makeTable(document.getElementById("gTable"), gCols, () => GO.filter(g => g.q &&
  (!gQuery || g.name.toLowerCase().includes(gQuery) || g.team.toLowerCase().includes(gQuery))), "projTG");
document.getElementById("gSearch").oninput = e => { gQuery = e.target.value.trim().toLowerCase(); renderG(); };

const hvCols = [
  { key: "name", label: "Player", left: true, cls: "name", fmt: nameCell },
  { key: "pos", label: "Pos", left: true, fmt: posTag },
  { hs: true, key: "team", label: "Team", left: true },
  { key: "fpg", label: "FP/GP", fmt: (v, r) => `${f2(v)} <span class="muted">#${r.rank}</span>`, cls: "fpg" },
  { key: "ppg", label: "NHL P/GP", fmt: (v, r) => `${f2(v)} <span class="muted">#${r.ppgRank}</span>` },
  { key: "gap", label: "Gap", fmt: v => `<span class="${v > 0 ? "up" : "down"}">${v > 0 ? "+" : ""}${v}</span>`, tip: "Rank spots gained (or lost) with your scoring" },
  { hs: true, key: "peri", label: "Peri %", fmt: pct },
];
const byGap = SK.filter(p => p.q).sort((a, b) => b.gap - a.gap);
makeTable(document.getElementById("hvUp"), hvCols, () => byGap.slice(0, 25), "gap");
makeTable(document.getElementById("hvDown"), hvCols, () => byGap.slice(-25), "gap", true);

// ---------- picker ----------
const ALL = [...P.values()].sort((a, b) => (b.rosFp || 0) - (a.rosFp || 0));
function makePicker(root, placeholder, onPick) {
  root.innerHTML = `<input type="search" placeholder="${placeholder}" autocomplete="off" autocorrect="off" spellcheck="false"><div class="sugg" hidden></div>`;
  const inp = root.querySelector("input"), box = root.querySelector(".sugg");
  inp.oninput = () => {
    const q = inp.value.trim().toLowerCase();
    if (q.length < 2) { box.hidden = true; return; }
    const m = ALL.filter(p => p.name.toLowerCase().includes(q) || p.team.toLowerCase() === q).slice(0, 8);
    box.innerHTML = m.map(p => `<button data-id="${p.id}"><b>${esc(p.name)}</b><span>${esc(p.team)} · ${p.pos} · ${f2(projTG(p))} proj FP/gm · ${f0(p.rosFp)} ROS FP</span></button>`).join("")
      || `<div class="none">No player found</div>`;
    box.hidden = false;
  };
  box.onclick = e => { const b = e.target.closest("button"); if (!b) return; onPick(+b.dataset.id); inp.value = ""; box.hidden = true; inp.blur(); };
  document.addEventListener("click", e => { if (!root.contains(e.target)) box.hidden = true; });
}

// ---------- COMPARE ----------
let cmp = store.get("cmp", []).filter(id => P.has(id));
function addCompare(id) {
  const p = P.get(id); if (!p) return;
  if (!cmp.includes(id)) { if (cmp.length >= 4) cmp.shift(); cmp.push(id); }
  store.set("cmp", cmp); renderCompare(); toast(`${p.name} added to Compare`);
}
const SKO = p => !isG(p), GOO = p => isG(p);
const CMP_ROWS = [
  { label: "Projected FP per game", get: p => projTG(p), fmt: f2, better: "hi", tip: "Goalies: per team game (per start × start share)" },
  { label: "Season FP / GP", get: p => p.fpg, fmt: f2, better: "hi" },
  { label: `Last ${C.recentDays} days FP / GP`, get: p => isG(p) ? p.recFpgs : p.recFpg, fmt: f2, better: "hi", note: "goalies: per start" },
  { label: "Scoring vs peripherals", get: p => isG(p) ? null : 1, fmt: (v, p) => isG(p) ? "—" : splitBar(null, p) },
  { label: "Peripheral share (stability)", get: p => isG(p) ? null : p.peri, fmt: pct },
  { label: "Games played", get: p => p.gp, fmt: f0 },
  { label: "Games left this week", get: p => p.wk, fmt: f0, better: "hi" },
  { label: "Projected FP this week", get: p => p.wkFp, fmt: f1, better: "hi" },
  { label: "Games next week", get: p => p.nxt, fmt: f0, better: "hi" },
  { label: "Projected FP next week", get: p => p.nxtFp, fmt: f1, better: "hi" },
  { label: "Games left in season", get: p => p.ros, fmt: f0, better: "hi" },
  { label: "Rest-of-season FP", get: p => p.rosFp, fmt: f0, better: "hi" },
  { label: "Value over replacement (ROS)", get: p => p.rosVor, fmt: v => sgn(v, f0), better: "hi" },
  { label: "Playoff-window FP", get: p => p.poFp, fmt: f0, better: "hi" },
  { label: "Shooting % (normal)", get: p => isG(p) ? null : p.sh, fmt: (v, p) => isG(p) || nz(v) ? "—" : `${f1(v)}% <span class="muted">(${f1(p.shBase)}%)</span>` },
  { label: "Position rank", get: p => isG(p) ? p.projRank : p.posRank, fmt: v => nz(v) ? "—" : "#" + v, better: "lo" },
  { label: "Goalie: share of starts", get: p => isG(p) ? p.share * 100 : null, fmt: pct, better: "hi" },
  { label: "Goalie: projected FP per start", get: p => isG(p) ? p.proj : null, fmt: f2, better: "hi" },
  { label: "Goalie: save %", get: p => isG(p) ? p.svPct : null, fmt: f3, better: "hi" },
];
makePicker(document.getElementById("cmpPicker"), "Add a player to compare...", addCompare);
document.getElementById("cmpClear").onclick = () => { cmp = []; store.set("cmp", cmp); renderCompare(); };
function renderCompare() {
  const el = document.getElementById("cmpTable");
  const ps = cmp.map(id => P.get(id)).filter(Boolean);
  if (!ps.length) { el.innerHTML = `<tbody><tr><td class="muted" style="text-align:left;padding:18px 4px">No players yet. Search above, or tap a name in Rankings or <b>+</b> in Pulse.</td></tr></tbody>`; return; }
  const anyG = ps.some(isG), anyS = ps.some(p => !isG(p));
  const head = `<tr><th></th>${ps.map(p => `<th><div class="ph"><b>${esc(p.name)}</b><span>${esc(p.team)} · ${p.pos}</span></div>
    <div class="mini"><button data-tg="give" data-id="${p.id}">Give</button><button data-tg="get" data-id="${p.id}">Get</button><button class="x" data-rm="${p.id}" aria-label="Remove">✕</button></div></th>`).join("")}</tr>`;
  const rows = CMP_ROWS.filter(r => {
    if (r.label.startsWith("Goalie:")) return anyG;
    if (["Scoring vs peripherals", "Peripheral share (stability)", "Shooting % (normal)"].includes(r.label)) return anyS;
    return true;
  }).map(r => {
    const vals = ps.map(p => r.get(p));
    let best = -1;
    if (r.better && ps.length > 1) {
      const nums = vals.map((v, i) => [v, i]).filter(([v]) => !nz(v));
      if (nums.length > 1) best = nums.reduce((b, c) => (r.better === "hi" ? c[0] > b[0] : c[0] < b[0]) ? c : b)[1];
      if (nums.length > 1 && nums.every(([v]) => v === nums[0][0])) best = -1;
    }
    return `<tr><td title="${esc(r.tip || "")}">${r.label}${r.note ? ` <span class="muted">(${r.note})</span>` : ""}</td>${ps.map((p, i) =>
      `<td class="${i === best ? "best" : ""}">${r.fmt(vals[i], p)}</td>`).join("")}</tr>`;
  }).join("");
  el.innerHTML = `<thead>${head}</thead><tbody>${rows}</tbody>`;
  el.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { cmp = cmp.filter(id => id !== +b.dataset.rm); store.set("cmp", cmp); renderCompare(); });
  el.querySelectorAll("[data-tg]").forEach(b => b.onclick = () => { addTrade(b.dataset.tg, +b.dataset.id); });
}

// ---------- TRADE ----------
let trade = { give: store.get("give", []).filter(id => P.has(id)), get: store.get("get", []).filter(id => P.has(id)) };
function addTrade(side, id) {
  const other = side === "give" ? "get" : "give";
  trade[other] = trade[other].filter(x => x !== id);
  if (!trade[side].includes(id)) trade[side].push(id);
  store.set("give", trade.give); store.set("get", trade.get);
  renderTrade(); toast(`${P.get(id).name} → ${side === "give" ? "You give" : "You get"}`);
}
makePicker(document.getElementById("givePicker"), "Add a player you'd give...", id => addTrade("give", id));
makePicker(document.getElementById("getPicker"), "Add a player you'd get...", id => addTrade("get", id));
function sideList(side) {
  const ps = trade[side].map(id => P.get(id)).filter(Boolean);
  return ps.length ? ps.map(p => `<div class="prow"><div class="pn"><b>${esc(p.name)}</b><span>${esc(p.team)} · ${p.pos} · ${f2(projTG(p))} proj/gm</span></div>
    <div class="pv"><b>${f0(p.rosFp)}</b> FP<small>VOR ${sgn(p.rosVor, f0)}</small></div>
    <button class="add" data-trm="${side}:${p.id}" aria-label="Remove">✕</button></div>`).join("")
    : `<div class="empty">No players yet.</div>`;
}
function sideTotals(side) {
  const ps = trade[side].map(id => P.get(id)).filter(Boolean);
  const sum = k => ps.reduce((s, p) => s + (p[k] || 0), 0);
  const skFp = ps.filter(p => !isG(p)).reduce((s, p) => s + p.rosFp, 0);
  const peri = skFp > 0 ? ps.filter(p => !isG(p)).reduce((s, p) => s + p.peri * p.rosFp, 0) / skFp : null;
  const val = ps.reduce((s, p) => s + Math.max(0, p.rosVor || 0), 0);
  return { ps, n: ps.length, rosFp: sum("rosFp"), rosVor: val, wkFp: sum("wkFp"), nxtFp: sum("nxtFp"), poFp: sum("poFp"), peri };
}
function renderTrade() {
  document.getElementById("giveList").innerHTML = sideList("give");
  document.getElementById("getList").innerHTML = sideList("get");
  document.querySelectorAll("[data-trm]").forEach(b => b.onclick = () => {
    const [side, id] = b.dataset.trm.split(":"); trade[side] = trade[side].filter(x => x !== +id);
    store.set("give", trade.give); store.set("get", trade.get); renderTrade();
  });
  const v = document.getElementById("verdict");
  const G = sideTotals("give"), R = sideTotals("get");
  if (!G.n || !R.n) {
    v.innerHTML = `<div class="vhead">Build a trade</div><div class="vsub">Add at least one player on each side to see the verdict.</div>`;
    return;
  }
  const net = R.rosVor - G.rosVor;
  const scale = Math.max(40, 0.5 * (Math.abs(G.rosVor) + Math.abs(R.rosVor)));
  const fairBand = Math.max(8, 0.08 * Math.max(Math.abs(G.rosVor), Math.abs(R.rosVor)));
  const head = Math.abs(net) <= fairBand ? "Fair trade" : net > 0 ? "You win this trade" : "You lose this trade";
  const color = Math.abs(net) <= fairBand ? "var(--ice)" : net > 0 ? "var(--good)" : "var(--bad)";
  const pos = 50 + Math.max(-48, Math.min(48, net / scale * 50));

  const ins = [];
  ins.push(`Rest-of-season value over replacement: <b style="color:${color}">${sgn(net, f0)} FP</b> for you (raw projected FP: ${sgn(R.rosFp - G.rosFp, f0)}).`);
  const below = [...G.ps, ...R.ps].filter(p => (p.rosVor || 0) <= 0);
  if (below.length) ins.push(`${below.map(p => esc(p.name)).join(", ")} ${below.length > 1 ? "are" : "is"} at or below replacement level, so ${below.length > 1 ? "they count" : "he counts"} as 0. You could get similar production from free agents.`);
  const wk = R.wkFp - G.wkFp, nw = R.nxtFp - G.nxtFp;
  if (Math.abs(wk) >= 3) ins.push(`This week: ${wk > 0 ? "helps" : "hurts"} you by about <b>${f1(Math.abs(wk))} FP</b> (schedule + projections).`);
  if (Math.abs(nw) >= 3) ins.push(`Next week: ${nw > 0 ? "+" : "−"}${f1(Math.abs(nw))} FP.`);
  const po = R.poFp - G.poFp;
  if (D.week.ps && Math.abs(po) >= 3) ins.push(`Playoff window (${fd(D.week.ps)} – ${fd(D.week.pe)}): ${po > 0 ? "+" : "−"}${f0(Math.abs(po))} FP for you.`);
  if (G.peri !== null && R.peri !== null && Math.abs(R.peri - G.peri) >= 8)
    ins.push(R.peri > G.peri ? `You're getting <b>steadier</b> production: ${pct(R.peri)} of it from shots/hits/blocks vs ${pct(G.peri)}.`
                             : `You're getting <b>streakier</b> production: only ${pct(R.peri)} from shots/hits/blocks vs ${pct(G.peri)}. More depends on goals and assists.`);
  const lucky = R.ps.filter(p => !isG(p) && !nz(p.shDiff) && p.shDiff >= 5);
  if (lucky.length) ins.push(`Heads up: ${lucky.map(p => esc(p.name)).join(", ")} ${lucky.length > 1 ? "are" : "is"} shooting well above normal. Expect fewer goals going forward.`);
  const due = R.ps.filter(p => !isG(p) && !nz(p.shDiff) && p.shDiff <= -4);
  if (due.length) ins.push(`Buy-low bonus: ${due.map(p => esc(p.name)).join(", ")} ${due.length > 1 ? "are" : "is"} shooting below normal and could pick up.`);
  const gk = [...G.ps, ...R.ps].filter(isG);
  if (gk.length) ins.push(`Goalie value depends on starts: ${gk.map(g => `${esc(g.name)} ${pct(g.share * 100)}`).join(", ")}. A change in the crease changes this verdict fast.`);
  if (G.n !== R.n) ins.push(`${R.n > G.n ? "You receive more players, so you'll need to drop " + (R.n - G.n) : "You open " + (G.n - R.n) + " roster spot" + (G.n - R.n > 1 ? "s" : "")} — already counted: VOR measures each player against a free-agent-level replacement.`);

  const row = (label, a, b, fmt, signed) => `<tr><td>${label}</td><td>${fmt(a)}</td><td>${fmt(b)}</td><td class="${b - a > 0 ? "up" : b - a < 0 ? "down" : ""}">${signed(b - a)}</td></tr>`;
  const summary = [`Trade check (${D.generated})`, `Give: ${G.ps.map(p => p.name).join(", ")}`, `Get: ${R.ps.map(p => p.name).join(", ")}`,
    `${head}: ${net > 0 ? "+" : ""}${Math.round(net)} FP value over replacement, ${R.rosFp - G.rosFp > 0 ? "+" : ""}${Math.round(R.rosFp - G.rosFp)} projected FP rest of season.`].join("\n");

  v.innerHTML = `<div class="vhead" style="color:${color}">${head}</div>
    <div class="vsub">${sgn(net, f0)} FP of rest-of-season value over replacement</div>
    <div class="meter"><i style="left:${pos}%"></i></div>
    <div class="mlab"><span>They win</span><span>Even</span><span>You win</span></div>
    <div class="scroll"><table class="vt"><thead><tr><th></th><th>Give</th><th>Get</th><th>Net</th></tr></thead><tbody>
      ${row("Rest-of-season FP", G.rosFp, R.rosFp, f0, v => sgn(v, f0))}
      ${row("Trade value (VOR)", G.rosVor, R.rosVor, f0, v => sgn(v, f0))}
      ${row("FP this week", G.wkFp, R.wkFp, f1, v => sgn(v, f1))}
      ${row("FP next week", G.nxtFp, R.nxtFp, f1, v => sgn(v, f1))}
      ${D.week.ps ? row("Playoff-window FP", G.poFp, R.poFp, f0, v => sgn(v, f0)) : ""}
      <tr><td>Peripheral share</td><td>${pct(G.peri)}</td><td>${pct(R.peri)}</td><td></td></tr>
    </tbody></table></div>
    <ul class="insights">${ins.map(i => `<li>${i}</li>`).join("")}</ul>
    <div class="btns"><button class="btn primary" id="tCopy">Copy summary</button><button class="btn" id="tSwap">Swap sides</button><button class="btn" id="tClear">Clear</button></div>`;
  document.getElementById("tCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(summary); toast("Summary copied"); } catch (e) { toast("Couldn't copy on this browser"); }
  };
  document.getElementById("tSwap").onclick = () => { trade = { give: trade.get, get: trade.give }; store.set("give", trade.give); store.set("get", trade.get); renderTrade(); };
  document.getElementById("tClear").onclick = () => { trade = { give: [], get: [] }; store.set("give", []); store.set("get", []); renderTrade(); };
}

// ---------- GUIDE ----------
function renderGuide() {
  const chip = ([k, v]) => `<span class="chip ${v < 0 ? "neg" : ""}">${k} <b>${v}</b></span>`;
  const s = Object.fromEntries(C.skaterScoring);
  const slots = Object.entries(C.slots).map(([k, v]) => `${v} ${k}`).join(", ");
  const repl = Object.entries(C.repl).map(([k, v]) => `<span>${k}</span><span>${f2(v)} FP per ${k === "G" ? "team game" : "game"}</span>`).join("");
  const W = D.week;
  document.getElementById("guide").innerHTML = `
  <h2>How to read this app</h2>
  <p class="lede">Everything here is built from public NHL stats, scored with your Yahoo league's settings. It rebuilds itself every morning around 5 AM (last update: ${D.generated}).</p>

  <h3>Your league's scoring</h3>
  <div class="chips">${C.skaterScoring.map(chip).join("")}</div>
  <div class="chips">${C.goalieScoring.map(chip).join("")}</div>
  <p>Hits and blocks matter a lot here: <b>1 goal = ${f0(s.G / s.HIT)} hits = ${f1(s.G / s.BLK)} blocks = ${f0(s.G / s.SOG)} shots</b>. A defenceman who hits and blocks a lot can outscore a 40-point winger. For goalies, one goal against costs about as much as ${f1(0.85 / 0.2)} saves, so goalies who face lots of shots score well even in losses.</p>

  <h3>The pages</h3>
  <dl>
    <dt>Pulse</dt><dd>Your weekly briefing: how many games each team has left this week and next, players worth streaming, goalie starts, who's hot or cold over the last ${C.recentDays} days, and shooting-luck flags. On Sundays it switches to next week.</dd>
    <dt>Rankings</dt><dd><b>Skaters</b> and <b>Goalies</b> ranked by projected fantasy points. <b>Hidden Value</b> lists players your scoring rates much higher (or lower) than plain NHL points do: your edge over league-mates who draft by points. On a phone only the key columns show; turn the phone sideways or use a computer for the full table.</dd>
    <dt>Compare</dt><dd>Up to 4 players side by side. The best value in each row is green. Players stay saved on this device.</dd>
    <dt>Trade</dt><dd>Build a mock trade. The verdict uses rest-of-season value over replacement, then adds notes on this week, next week, the playoff window, stability and luck.</dd>
  </dl>

  <h3>Glossary</h3>
  <dl>
    <dt>FP</dt><dd>Fantasy points with your league's scoring.</dd>
    <dt>FP/GP</dt><dd>Fantasy points per game played this season.</dd>
    <dt>L${C.recentDays}</dt><dd>FP per game over the last ${C.recentDays} days: recent form.</dd>
    <dt>Proj</dt><dd>Projected FP per game for the rest of the season (see "How projections work").</dd>
    <dt>ROS FP</dt><dd>Rest of season: projected FP per game × games left on his team's schedule. Assumes he plays every game (no injury info).</dd>
    <dt>Replacement level</dt><dd>What you could get from the free-agent pool. With ${C.leagueTeams} teams starting ${slots}, it's the projection of the first player outside the starters at each position:
      <div class="kv" style="margin-top:6px">${repl}</div></dd>
    <dt>VOR (value over replacement)</dt><dd>How much better than replacement level a player is, added up over the rest of the season. This is the fairest way to compare players at different positions and judge trades. A great D-man can be worth more than a winger with more points because good D are scarcer. In the Trade page, players below replacement count as 0. That's why two average players usually don't beat one star: you could pick up the average guys as free agents.</dd>
    <dt>Split bar / Peri %</dt><dd>Blue = FP from scoring (G, A, PPP, SHG, SHA, GWG, +/-). Orange = FP from peripherals (shots, hits, blocks). Peri % is the orange share. Peripherals are steady week to week; scoring is streaky. Higher Peri % = safer floor.</dd>
    <dt>Gap (Hidden Value)</dt><dd>How many rank spots a player moves when you rank by your FP/GP instead of NHL points per game.</dd>
    <dt>Shooting % and luck</dt><dd>Goals divided by shots, compared to his normal (last season if he had 60+ shots, otherwise the league average for forwards or defence). Way above normal = likely to cool off (sell high). Way below = likely to heat up (buy low). Shown once he has 30+ shots.</dd>
    <dt>Starts / share</dt><dd>The % of his team's games a goalie starts. Projected starts = team games × share.</dd>
    <dt>No-win FP</dt><dd>A goalie's FP per game from saves and goals against only. His floor when he loses.</dd>
    <dt>Playoff window</dt><dd>${W.ps ? `${fd(W.ps)} – ${fd(W.pe)}` : "Not set"}. By default, the last 3 weeks of the NHL regular season. Edit PLAYOFF_START / PLAYOFF_END in the script to match your league.</dd>
  </dl>

  <h3>How projections work</h3>
  <p>Early-season numbers are noisy, so the projection blends three things:</p>
  <span class="formula">1. <b>This season</b> FP/GP, weighted by games played<br>
  2. <b>Last season</b> FP/GP, counted as if it were ${C.priorGames} games of this season (rookies get a cautious baseline instead)<br>
  3. <b>Last ${C.recentDays} days</b> form, nudging the result ${Math.round(C.recentWeight * 100)}%</span>
  <p>As the season goes on, this season's numbers take over. Goalies use the same idea per start (last season counts as ${C.goaliePriorStarts} starts), multiplied by their share of starts.</p>

  <h3>Limits</h3>
  <ul>
    <li>No injury, line-combo or goalie-confirmation data. Check Yahoo or Daily Faceoff before setting lineups.</li>
    <li>Positions are NHL positions, not Yahoo eligibility (some players qualify at two spots on Yahoo).</li>
    <li>Not connected to your Yahoo league yet, so it doesn't know who's on a roster. "Stream targets" assumes players ranked outside the top ${C.leagueTeams * C.rosteredPerTeam} are available.</li>
    ${C.hasLast ? "" : "<li><b>Last season's stats didn't load on this run</b>, so projections lean on this season only.</li>"}
    ${C.hasRecent ? "" : "<li><b>Recent-form stats didn't load on this run</b>, so hot/cold lists are empty.</li>"}
    ${C.hasSchedule ? "" : "<li><b>Schedules didn't load on this run</b>, so weekly and rest-of-season numbers are estimates.</li>"}
  </ul>

  <h3>Settings</h3>
  <p>League size, roster slots, scoring and the playoff window are at the top of <code>fantasy_fp_rankings.py</code> under "YOUR LEAGUE SETTINGS". Edit them on GitHub, commit, and run the workflow again.</p>
  <p class="muted" style="margin-top:24px">Data: NHL public stats API (api.nhle.com, api-web.nhle.com). Personal use only.</p>`;
}

// ---------- boot ----------
renderPulse();
renderCompare();
renderTrade();
renderGuide();
const saved = store.get("tab", "pulse");
if (TABS.includes(saved)) showTab(saved);
</script>
</body>
</html>
"""


# ===========================================================================
# MAIN
# ===========================================================================
def build(raw, season_id, min_gp, out_dir, today=None, open_browser=True):
    today = today or local_now().date()
    teams, week = analyze_schedule(raw.get("schedules", {}), today)
    skaters, repl = build_skaters(raw, teams, min_gp)
    goalies, repl_g = build_goalies(raw, teams, min_gp)
    repl["G"] = repl_g
    pulse = build_pulse(skaters, goalies, week)

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
        "config": {
            "skaterScoring": [[LEAGUE_LABELS[k], v] for k, v in SKATER_SCORING.items()],
            "goalieScoring": [[LEAGUE_LABELS[k], v] for k, v in GOALIE_SCORING.items()],
            "leagueTeams": LEAGUE_TEAMS, "slots": ROSTER_SLOTS, "rosteredPerTeam": SKATERS_ROSTERED_PER_TEAM,
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
