"""
Yahoo Fantasy Sports API wrapper.

Only the surface area used by the current app:
  - leagues / my team / all teams
  - current matchup (this week's scoreboard)
  - team roster + merged MLB Stats season stats + 5x5 strength analysis
  - free agents / waivers with eligible_positions + headshot
"""
import os
import requests
from flask import session

BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"


def get_headers():
    return {
        "Authorization": f"Bearer {session['access_token']}",
        "Accept": "application/json",
    }


def refresh_token_if_needed():
    """Refresh access token using refresh token."""
    from requests_oauthlib import OAuth2Session
    client_id     = os.getenv("YAHOO_CLIENT_ID")
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
            refresh_token=refresh_token,
        )
        session["access_token"]  = new_token["access_token"]
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
        refresh_token_if_needed()
        r = requests.get(f"{BASE_URL}{path}", headers=get_headers(), params=params)
    r.raise_for_status()
    return r.json()


# ── Parsers ────────────────────────────────────────────────────

def _list_find(lst, key):
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


# ── User & Leagues ─────────────────────────────────────────────

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
                    "name":       league["name"],
                    "season":     league["season"],
                    "num_teams":  league["num_teams"],
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


# ── Matchup ────────────────────────────────────────────────────

def get_current_matchup(team_key):
    """Get current week's matchup for a team."""
    data = api_get(f"/team/{team_key}/matchups")
    try:
        matchups = data["fantasy_content"]["team"][1]["matchups"]
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
                        "name":     team_info[2]["name"],
                        "points":   team_points.get("total", "0"),
                        "team_key": team_info[0]["team_key"],
                    })
                return result
        # fallback: last matchup
        last = matchups[str(matchups["count"] - 1)]["matchup"]
        teams = last["0"]["teams"]
        result = {"week": last.get("week"), "teams": []}
        for j in range(teams["count"]):
            t = teams[str(j)]["team"]
            result["teams"].append({
                "name":     t[0][2]["name"],
                "points":   t[1].get("team_points", {}).get("total", "0"),
                "team_key": t[0][0]["team_key"],
            })
        return result
    except Exception as e:
        print(f"Error getting matchup: {e}")
        return {}


# ── Roster + Stats ─────────────────────────────────────────────

def get_team_roster_with_stats(team_key):
    """Roster with merged 2026/2025 stats from MLB Stats API (matched by name)."""
    import mlb_stats
    hitting  = mlb_stats.get_hitting_stats_merged()
    pitching = mlb_stats.get_pitching_stats_merged()

    data = api_get(f"/team/{team_key}/roster/players")
    try:
        players_data = data["fantasy_content"]["team"][1]["roster"]["0"]["players"]
        players = []
        for i in range(players_data["count"]):
            p = players_data[str(i)]["player"]
            info = parse_player_info(p[0])
            display_pos = info.get("display_position", "")
            is_batter   = display_pos not in ("SP", "RP", "P")
            full_name   = info.get("name", {}).get("full", "")
            norm_name   = mlb_stats._normalize(full_name)
            stats       = hitting.get(norm_name, {}) if is_batter else pitching.get(norm_name, {})
            eligible    = [
                ep.get("position", "")
                for ep in info.get("eligible_positions", [])
                if isinstance(ep, dict) and ep.get("position")
            ]
            players.append({
                "name":               full_name,
                "position":           display_pos,
                "team":               info.get("editorial_team_abbr", ""),
                "player_key":         info.get("player_key", ""),
                "headshot":           info.get("image_url", ""),
                "eligible_positions": eligible,
                "stats":              stats,
                "is_batter":          is_batter,
                "analysis":           analyze_player(stats, is_batter),
            })
        return players
    except Exception as e:
        print(f"Error getting roster with stats: {e}")
        return []


# ── Free Agents / Waivers ──────────────────────────────────────

def get_free_agents(league_key, position="B", count=50):
    """Top available waiver + FA players (deduped, sorted by % owned)."""
    players = []
    for status in ["W", "FA"]:
        params = {"status": status, "sort": "AR", "count": count}
        if position != "ALL":
            params["position"] = position
        try:
            data = api_get(f"/league/{league_key}/players", params=params)
            players_data = data["fantasy_content"]["league"][1]["players"]
            for i in range(players_data.get("count", 0)):
                p = players_data[str(i)]["player"]
                info = parse_player_info(p[0])
                percent = p[1].get("percent_owned", {}) if len(p) > 1 else {}
                eligible = [
                    ep.get("position", "")
                    for ep in info.get("eligible_positions", [])
                    if isinstance(ep, dict) and ep.get("position")
                ]
                players.append({
                    "name":               info.get("name", {}).get("full", ""),
                    "position":           info.get("display_position", ""),
                    "team":               info.get("editorial_team_abbr", ""),
                    "headshot":           info.get("image_url", ""),
                    "eligible_positions": eligible,
                    "percent_owned":      percent.get("value", "0") if isinstance(percent, dict) else "0",
                    "status":             status,
                })
        except Exception as e:
            print(f"Error getting {status} players: {e}")

    seen = set()
    result = []
    for p in sorted(players, key=lambda x: float(x["percent_owned"] or 0), reverse=True):
        if p["name"] not in seen:
            seen.add(p["name"])
            result.append(p)
    return result[:count]


# ── Strength / Weakness Analysis (5x5) ─────────────────────────

def analyze_player(stats, is_batter):
    """Return strengths/weaknesses tuned for league 7x7 cats.
    Batting:  HR, RBI, SB, AVG, OBP, OPS, E
    Pitching: W, BB, HLD, SV, K, ERA, WHIP
    """
    strengths, weaknesses = [], []

    if is_batter:
        avg = stats.get("AVG"); hr  = stats.get("HR"); rbi = stats.get("RBI")
        sb  = stats.get("SB");  obp = stats.get("OBP"); ops = stats.get("OPS")
        e   = stats.get("E")

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
            if sb >= 20: strengths.append("盜壘威脅")
            elif sb < 5: weaknesses.append("缺乏速度")
        if obp is not None:
            if obp >= 0.370: strengths.append("高上壘率")
            elif obp < 0.310: weaknesses.append("上壘率低")
        if ops is not None:
            if ops >= 0.870: strengths.append("超高 OPS")
            elif ops < 0.700: weaknesses.append("OPS 偏低")
        if e is not None:
            if e <= 5:  strengths.append("守備穩定")
            elif e >= 15: weaknesses.append("失誤偏多")
    else:
        era  = stats.get("ERA"); whip = stats.get("WHIP"); k = stats.get("K")
        w    = stats.get("W");   sv   = stats.get("SV");   hld = stats.get("HLD")
        bb9  = stats.get("BB9"); k9   = stats.get("K9")

        if sv  is not None and sv  >= 15: strengths.append("守護神")
        if hld is not None and hld >= 20: strengths.append("中繼戰將")
        if era is not None:
            if era <= 3.20: strengths.append("頂級防禦率")
            elif era > 4.50: weaknesses.append("防禦率偏高")
        if whip is not None:
            if whip <= 1.10: strengths.append("WHIP 頂級")
            elif whip > 1.40: weaknesses.append("WHIP 偏高")
        if k is not None:
            if k >= 180: strengths.append("三振機器")
            elif k < 80: weaknesses.append("三振數少")
        if w is not None:
            if w >= 14: strengths.append("高勝投數")
            elif w < 6: weaknesses.append("勝投偏少")
        if bb9 is not None:
            if bb9 <= 2.5: strengths.append("控球精準")
            elif bb9 >= 4.0: weaknesses.append("保送多")
        if k9 is not None and k9 >= 10.0:
            strengths.append("K/9 出色")

    return {"strengths": strengths, "weaknesses": weaknesses}
