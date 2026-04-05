import requests
import os
from flask import session

BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"

def get_headers():
    return {
        "Authorization": f"Bearer {session['access_token']}",
        "Accept": "application/json"
    }

def refresh_token_if_needed():
    """Refresh access token using refresh token."""
    from requests_oauthlib import OAuth2Session
    client_id = os.getenv("YAHOO_CLIENT_ID")
    client_secret = os.getenv("YAHOO_CLIENT_SECRET")
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        raise Exception("No refresh token — please log in again.")
    try:
        temp_oauth = OAuth2Session(client_id)
        new_token = temp_oauth.refresh_token(
            "https://api.login.yahoo.com/oauth2/get_token",
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token
        )
        session["access_token"] = new_token["access_token"]
        session["refresh_token"] = new_token.get("refresh_token", refresh_token)
    except Exception as e:
        print(f"Token refresh failed: {e}")
        raise Exception("Session expired — please log in again.")

def api_get(path, params=None):
    if params is None:
        params = {}
    params["format"] = "json"
    r = requests.get(f"{BASE_URL}{path}", headers=get_headers(), params=params)
    if r.status_code == 401:
        refresh_token_if_needed()  # raises if refresh fails
        r = requests.get(f"{BASE_URL}{path}", headers=get_headers(), params=params)
    r.raise_for_status()
    return r.json()

# ── User & League ──────────────────────────────────────────────

def get_user_leagues():
    """Get all MLB fantasy leagues for current user."""
    data = api_get("/users;use_login=1/games;game_codes=mlb/leagues")
    try:
        games = data["fantasy_content"]["users"]["0"]["user"][1]["games"]
        leagues = []
        for i in range(games["count"]):
            game = games[str(i)]["game"]
            game_leagues = game[1].get("leagues", {})
            for j in range(game_leagues.get("count", 0)):
                league = game_leagues[str(j)]["league"][0]
                leagues.append({
                    "league_key": league["league_key"],
                    "name": league["name"],
                    "season": league["season"],
                    "num_teams": league["num_teams"]
                })
        return leagues
    except (KeyError, TypeError) as e:
        print(f"Error parsing leagues: {e}")
        return []

def get_my_team(league_key):
    """Get current user's team in a league."""
    data = api_get(f"/leagues;league_keys={league_key}/teams")
    try:
        teams = data["fantasy_content"]["leagues"]["0"]["league"][1]["teams"]
        for i in range(teams["count"]):
            team_list = teams[str(i)]["team"][0]
            managers_list = _list_find(team_list, "managers") or []
            for m in managers_list:
                mgr = m.get("manager", {})
                if mgr.get("is_current_login") == "1":
                    standings = _list_find(team_list, "team_standings") or {}
                    outcome = standings.get("outcome_totals", {})
                    return {
                        "team_key": _list_find(team_list, "team_key"),
                        "name":     _list_find(team_list, "name"),
                        "rank":     standings.get("rank"),
                        "wins":     outcome.get("wins"),
                        "losses":   outcome.get("losses"),
                        "points":   standings.get("points_for"),
                    }
    except Exception as e:
        print(f"Error getting my team: {e}")
    return None

def get_team_roster(team_key):
    """Get full roster for a team."""
    data = api_get(f"/team/{team_key}/roster/players")
    try:
        players_data = data["fantasy_content"]["team"][1]["roster"]["0"]["players"]
        players = []
        for i in range(players_data["count"]):
            p = players_data[str(i)]["player"]
            info = parse_player_info(p[0])
            status = p[1].get("selected_position", [{}]) if len(p) > 1 else [{}]
            selected = status[1].get("position", "") if len(status) > 1 else ""
            players.append({
                "name":              info.get("name", {}).get("full", ""),
                "position":          info.get("display_position", ""),
                "team":              info.get("editorial_team_abbr", ""),
                "selected_position": selected,
                "player_key":        info.get("player_key", ""),
            })
        return players
    except Exception as e:
        print(f"Error getting roster: {e}")
        return []

def get_team_stats(team_key):
    """Get team stats for current week."""
    data = api_get(f"/team/{team_key}/stats")
    try:
        stats_raw = data["fantasy_content"]["team"][1]["team_stats"]["stats"]
        stats = {}
        stat_map = {
            "60": "R", "7": "HR", "12": "RBI", "16": "SB",
            "3": "AVG", "55": "OPS", "50": "IP", "28": "W",
            "32": "SV", "42": "K", "26": "ERA", "27": "WHIP"
        }
        for s in stats_raw:
            sid = s["stat"]["stat_id"]
            val = s["stat"]["value"]
            if sid in stat_map:
                stats[stat_map[sid]] = val
        return stats
    except Exception as e:
        print(f"Error getting team stats: {e}")
        return {}

# ── Matchup ────────────────────────────────────────────────────

def get_current_matchup(team_key):
    """Get current week's matchup for a team."""
    data = api_get(f"/team/{team_key}/matchups")
    try:
        matchups = data["fantasy_content"]["team"][1]["matchups"]
        # Find current week matchup
        for i in range(matchups["count"]):
            m = matchups[str(i)]["matchup"]
            if m.get("status") == "midevent" or m.get("is_current_week") == "1" or m.get("week_start"):
                teams = m["0"]["teams"]
                result = {"week": m.get("week"), "teams": []}
                for j in range(teams["count"]):
                    t = teams[str(j)]["team"]
                    team_info = t[0]
                    team_points = t[1].get("team_points", {})
                    result["teams"].append({
                        "name": team_info[2]["name"],
                        "points": team_points.get("total", "0"),
                        "team_key": team_info[0]["team_key"]
                    })
                return result
        # fallback: return last matchup
        last = matchups[str(matchups["count"] - 1)]["matchup"]
        teams = last["0"]["teams"]
        result = {"week": last.get("week"), "teams": []}
        for j in range(teams["count"]):
            t = teams[str(j)]["team"]
            result["teams"].append({
                "name": t[0][2]["name"],
                "points": t[1].get("team_points", {}).get("total", "0"),
                "team_key": t[0][0]["team_key"]
            })
        return result
    except Exception as e:
        print(f"Error getting matchup: {e}")
        return {}

# ── Free Agents ────────────────────────────────────────────────

def get_free_agents(league_key, position="B", count=25):
    """Get top available free agents by position."""
    pos_map = {"B": "1B,2B,3B,SS,OF,C,1B", "SP": "SP", "RP": "RP"}
    data = api_get(
        f"/league/{league_key}/players",
        params={
            "status": "FA",
            "sort": "AR",
            "count": count,
            "position": position
        }
    )
    try:
        players_data = data["fantasy_content"]["league"][1]["players"]
        players = []
        for i in range(players_data["count"]):
            p = players_data[str(i)]["player"]
            info = p[0]
            percent = p[1].get("percent_owned", {})
            players.append({
                "name": info[2]["name"]["full"],
                "position": info[1]["display_position"],
                "team": info[6].get("editorial_team_abbr", ""),
                "percent_owned": percent.get("value", "0")
            })
        return players
    except Exception as e:
        print(f"Error getting free agents: {e}")
        return []

# ── All League Players ─────────────────────────────────────────

STAT_MAP = {
    "60": "R", "7": "HR", "12": "RBI", "16": "SB", "3": "AVG",
    "55": "OPS", "13": "OBP", "50": "IP", "28": "W", "32": "SV",
    "42": "K", "26": "ERA", "27": "WHIP", "48": "K9"
}

def _list_find(lst, key):
    """Search a list of single-key dicts for a specific key. Returns value or None."""
    if not isinstance(lst, list):
        return None
    for item in lst:
        if isinstance(item, dict) and key in item:
            return item[key]
    return None

def parse_player_info(info_list):
    """Merge the list of single-key dicts into one flat dict."""
    merged = {}
    for item in info_list:
        if isinstance(item, dict):
            merged.update(item)
    return merged

def parse_player_stats(stats_raw):
    stats = {}
    for s in stats_raw:
        sid = str(s["stat"]["stat_id"])
        val = s["stat"]["value"]
        if sid in STAT_MAP:
            try:
                # "3/5" format → take first number
                if isinstance(val, str) and "/" in val:
                    val = val.split("/")[0]
                stats[STAT_MAP[sid]] = float(val) if val not in ("-", "", None) else None
            except (ValueError, TypeError):
                stats[STAT_MAP[sid]] = None
    return stats

def get_league_players(league_key, position="ALL", sort="AR", start=0, count=50):
    """Get players from Yahoo + merged 2026/2025 stats from MLB Stats API (matched by name)."""
    import mlb_stats
    hitting = mlb_stats.get_hitting_stats_merged()
    pitching = mlb_stats.get_pitching_stats_merged()

    params = {"sort": sort, "start": start, "count": count}
    if position != "ALL":
        params["position"] = position

    data = api_get(f"/league/{league_key}/players", params=params)
    try:
        players_data = data["fantasy_content"]["league"][1]["players"]
        players = []
        for i in range(players_data["count"]):
            p = players_data[str(i)]["player"]
            info = parse_player_info(p[0])
            display_pos = info.get("display_position", "")
            is_batter = display_pos not in ("SP", "RP", "P")
            full_name = info.get("name", {}).get("full", "")
            norm_name = mlb_stats._normalize(full_name)
            stats = hitting.get(norm_name, {}) if is_batter else pitching.get(norm_name, {})
            players.append({
                "player_key": info.get("player_key", ""),
                "name": full_name,
                "position": display_pos,
                "team": info.get("editorial_team_abbr", ""),
                "headshot": info.get("image_url", ""),
                "percent_owned": "0",
                "stats": stats,
                "is_batter": is_batter,
                "analysis": analyze_player(stats, is_batter)
            })
        return players
    except Exception as e:
        print(f"Error getting league players: {e}")
        return []

def analyze_player(stats, is_batter):
    """Return strengths, weaknesses, and a role label."""
    strengths = []
    weaknesses = []

    if is_batter:
        avg = stats.get("AVG")
        hr  = stats.get("HR")
        rbi = stats.get("RBI")
        sb  = stats.get("SB")
        ops = stats.get("OPS")
        r   = stats.get("R")

        if avg is not None:
            if avg >= 0.295: strengths.append("高打擊率")
            elif avg < 0.245: weaknesses.append("打擊率低")

        if hr is not None:
            if hr >= 28: strengths.append("強力長打")
            elif hr < 10: weaknesses.append("長打不足")

        if rbi is not None:
            if rbi >= 85: strengths.append("優秀打點機")
            elif rbi < 45: weaknesses.append("打點貢獻低")

        if sb is not None:
            if sb >= 20: strengths.append("高盜壘威脅")
            elif sb < 5: weaknesses.append("缺乏速度")

        if ops is not None:
            if ops >= 0.880: strengths.append("攻擊性極強")
            elif ops < 0.700: weaknesses.append("攻擊效率差")

        if r is not None:
            if r >= 85: strengths.append("高得分能力")

    else:  # pitcher
        era  = stats.get("ERA")
        whip = stats.get("WHIP")
        k    = stats.get("K")
        w    = stats.get("W")
        sv   = stats.get("SV")
        ip   = stats.get("IP")

        if sv is not None and sv >= 15:
            strengths.append("守護神")

        if era is not None:
            if era <= 3.20: strengths.append("頂級防禦率")
            elif era > 4.50: weaknesses.append("防禦率偏高")

        if whip is not None:
            if whip <= 1.10: strengths.append("控球精準")
            elif whip > 1.40: weaknesses.append("保送/安打多")

        if k is not None:
            if k >= 180: strengths.append("三振機器")
            elif k < 80: weaknesses.append("三振數少")

        if w is not None:
            if w >= 14: strengths.append("高勝投數")

        if ip is not None and ip >= 170:
            strengths.append("大量局數")

    return {"strengths": strengths, "weaknesses": weaknesses}

def get_all_league_teams(league_key):
    """Get all teams in the league with manager info."""
    data = api_get(f"/league/{league_key}/teams")
    try:
        teams_data = data["fantasy_content"]["league"][1]["teams"]
        teams = []
        for i in range(teams_data["count"]):
            t = teams_data[str(i)]["team"][0]
            managers_list = _list_find(t, "managers") or []
            mgr = managers_list[0].get("manager", {}) if managers_list else {}
            teams.append({
                "team_key": _list_find(t, "team_key"),
                "name":     _list_find(t, "name"),
                "manager":  mgr.get("nickname", ""),
                "is_mine":  mgr.get("is_current_login") == "1",
            })
        return teams
    except Exception as e:
        print(f"Error getting teams: {e}")
        return []

def get_team_roster_with_stats(team_key):
    """Get roster with merged 2026/2025 stats from MLB Stats API (matched by name)."""
    import mlb_stats
    hitting = mlb_stats.get_hitting_stats_merged()
    pitching = mlb_stats.get_pitching_stats_merged()

    data = api_get(f"/team/{team_key}/roster/players")
    try:
        players_data = data["fantasy_content"]["team"][1]["roster"]["0"]["players"]
        players = []
        for i in range(players_data["count"]):
            p = players_data[str(i)]["player"]
            info = parse_player_info(p[0])
            display_pos = info.get("display_position", "")
            is_batter = display_pos not in ("SP", "RP", "P")
            full_name = info.get("name", {}).get("full", "")
            norm_name = mlb_stats._normalize(full_name)
            stats = hitting.get(norm_name, {}) if is_batter else pitching.get(norm_name, {})
            players.append({
                "name": full_name,
                "position": display_pos,
                "team": info.get("editorial_team_abbr", ""),
                "player_key": info.get("player_key", ""),
                "headshot": info.get("image_url", ""),
                "stats": stats,
                "is_batter": is_batter,
                "analysis": analyze_player(stats, is_batter)
            })
        return players
    except Exception as e:
        print(f"Error getting roster with stats: {e}")
        return []

# ── Draft ──────────────────────────────────────────────────────

_settings_cache = {}
_pool_cache = {}

def get_league_settings(league_key):
    """Get roster positions and league info.
    Static parts (roster_positions, num_teams, pick_time) are cached.
    draft_status is always fetched fresh."""
    data = api_get(f"/league/{league_key}/settings")
    try:
        league_info = data["fantasy_content"]["league"][0]
        settings    = data["fantasy_content"]["league"][1]["settings"][0]
        draft_status = league_info.get("draft_status", "predraft")

        if league_key not in _settings_cache:
            roster_positions = {}
            for rp in settings.get("roster_positions", []):
                pos_data = rp.get("roster_position", {})
                pos   = pos_data.get("position", "")
                count = int(pos_data.get("count", 0))
                if pos:
                    roster_positions[pos] = count
            _settings_cache[league_key] = {
                "roster_positions": roster_positions,
                "num_teams":        int(league_info.get("num_teams", 14)),
                "draft_pick_time":  int(settings.get("draft_pick_time", 45)),
            }

        result = dict(_settings_cache[league_key])
        result["draft_status"] = draft_status
        return result
    except Exception as e:
        print(f"Error getting settings: {e}")
        return {"roster_positions": {}, "num_teams": 14, "draft_pick_time": 45, "draft_status": "predraft"}

def get_draft_results(league_key):
    """Get current draft picks. Always fresh (called on every poll)."""
    data = api_get(f"/league/{league_key}/draftresults")
    try:
        raw = data["fantasy_content"]["league"][1].get("draft_results", [])
        picks = []
        for item in raw:
            if isinstance(item, dict):
                dr = item.get("draft_result", {})
                picks.append({
                    "pick":       int(dr.get("pick", 0)),
                    "round":      int(dr.get("round", 0)),
                    "team_key":   dr.get("team_key", ""),
                    "player_key": dr.get("player_key", "")
                })
        return sorted(picks, key=lambda x: x["pick"])
    except Exception as e:
        print(f"Error getting draft results: {e}")
        return []

def get_draft_player_pool(league_key, total=600):
    """Pre-fetch top players by Yahoo rank (= ADP proxy). Cached per league."""
    if league_key in _pool_cache:
        return _pool_cache[league_key]
    players = []
    for start in range(0, total, 50):
        try:
            data = api_get(f"/league/{league_key}/players", params={"sort": "AR", "start": start, "count": 50})
            players_data = data["fantasy_content"]["league"][1]["players"]
            count = players_data.get("count", 0)
            for i in range(count):
                p = players_data[str(i)]["player"]
                info = parse_player_info(p[0])
                eligible = [
                    ep.get("position", "")
                    for ep in info.get("eligible_positions", [])
                    if isinstance(ep, dict) and ep.get("position")
                ]
                display_pos = info.get("display_position", "")
                players.append({
                    "player_key":        info.get("player_key", ""),
                    "name":              info.get("name", {}).get("full", ""),
                    "position":          display_pos,
                    "eligible_positions": eligible,
                    "team":              info.get("editorial_team_abbr", ""),
                    "headshot":          info.get("image_url", ""),
                    "yahoo_rank":        len(players) + 1,
                    "is_batter":         display_pos not in ("SP", "RP", "P"),
                })
            if count < 50:
                break
        except Exception as e:
            print(f"Error fetching pool start={start}: {e}")
            break
    _pool_cache[league_key] = players
    return players

def get_players_by_keys(player_keys):
    """Batch-fetch player info for a list of player_keys (25 per request)."""
    result = {}
    for i in range(0, len(player_keys), 25):
        batch = player_keys[i:i+25]
        try:
            data = api_get(f"/players;player_keys={','.join(batch)}")
            players_data = data["fantasy_content"]["players"]
            for j in range(players_data.get("count", 0)):
                p = players_data[str(j)]["player"]
                info = parse_player_info(p[0])
                pk = info.get("player_key", "")
                eligible = [
                    ep.get("position", "")
                    for ep in info.get("eligible_positions", [])
                    if isinstance(ep, dict) and ep.get("position")
                ]
                result[pk] = {
                    "name":              info.get("name", {}).get("full", ""),
                    "position":          info.get("display_position", ""),
                    "eligible_positions": eligible,
                    "team":              info.get("editorial_team_abbr", ""),
                    "headshot":          info.get("image_url", ""),
                }
        except Exception as e:
            print(f"Error fetching player keys batch: {e}")
    return result

# ── Trade Suggestions ──────────────────────────────────────────

def get_all_teams_rosters(league_key):
    """Get all teams and their rosters for trade analysis."""
    data = api_get(f"/league/{league_key}/teams/roster/stats")
    try:
        teams_data = data["fantasy_content"]["league"][1]["teams"]
        teams = []
        for i in range(teams_data["count"]):
            t = teams_data[str(i)]["team"]
            team_list = t[0]
            roster = t[1].get("roster", {}).get("0", {}).get("players", {})
            players = []
            for j in range(roster.get("count", 0)):
                p = roster[str(j)]["player"]
                info = parse_player_info(p[0])
                players.append({
                    "name":     info.get("name", {}).get("full", ""),
                    "position": info.get("display_position", ""),
                })
            teams.append({
                "team_key": _list_find(team_list, "team_key"),
                "name":     _list_find(team_list, "name"),
                "players":  players,
            })
        return teams
    except Exception as e:
        print(f"Error getting all rosters: {e}")
        return []
