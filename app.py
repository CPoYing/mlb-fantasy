import os
from flask import Flask, redirect, request, session, url_for, render_template, jsonify
from requests_oauthlib import OAuth2Session
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import yahoo_api as api
import email_report
import mlb_schedule
import draft_engine
import mlb_stats

load_dotenv()

# Allow OAuth over HTTPS with self-signed cert
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "0"

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "mlb-fantasy-secret-key-2024")

CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")
CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET")
REDIRECT_URI = os.getenv("YAHOO_REDIRECT_URI")

AUTHORIZATION_BASE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

# Global oauth object (also used in yahoo_api.py for token refresh)
oauth = None

def make_oauth():
    return OAuth2Session(CLIENT_ID, redirect_uri=REDIRECT_URI)

# ── Auth ───────────────────────────────────────────────────────

@app.route("/")
def index():
    if "access_token" not in session:
        return render_template("login.html")
    return redirect(url_for("dashboard"))

@app.route("/login")
def login():
    global oauth
    oauth = make_oauth()
    auth_url, state = oauth.authorization_url(AUTHORIZATION_BASE_URL)
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route("/callback")
def callback():
    global oauth
    oauth = OAuth2Session(CLIENT_ID, redirect_uri=REDIRECT_URI, state=session.get("oauth_state"))
    token = oauth.fetch_token(
        TOKEN_URL,
        client_secret=CLIENT_SECRET,
        authorization_response=request.url.replace("https://", "https://")
    )
    session["access_token"] = token["access_token"]
    session["refresh_token"] = token.get("refresh_token", "")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# ── Pages ──────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        leagues = api.get_user_leagues()
        hot_players = mlb_stats.get_hot_players(days=7)
        return render_template("dashboard.html", leagues=leagues, hot_players=hot_players)
    except Exception as e:
        return render_template("error.html", error=str(e))

@app.route("/team/<league_key>")
def team(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        my_team = api.get_my_team(league_key)
        if not my_team:
            return render_template("error.html", error="找不到你的隊伍，請確認你有加入這個聯盟。")
        roster = api.get_team_roster(my_team["team_key"])
        stats = api.get_team_stats(my_team["team_key"])
        return render_template("team.html", team=my_team, roster=roster, stats=stats, league_key=league_key)
    except Exception as e:
        return render_template("error.html", error=str(e))

@app.route("/matchup/<league_key>")
def matchup(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        my_team = api.get_my_team(league_key)
        if not my_team:
            return render_template("error.html", error="找不到你的隊伍。")
        matchup_data = api.get_current_matchup(my_team["team_key"])
        return render_template("matchup.html", matchup=matchup_data, my_team=my_team, league_key=league_key)
    except Exception as e:
        return render_template("error.html", error=str(e))

@app.route("/free-agents/<league_key>")
def free_agents(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    position = request.args.get("position", "B")
    try:
        players = api.get_free_agents(league_key, position=position)
        return render_template("free_agents.html", players=players, league_key=league_key, position=position)
    except Exception as e:
        return render_template("error.html", error=str(e))

@app.route("/trades/<league_key>")
def trades(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        my_team = api.get_my_team(league_key)
        all_teams = api.get_all_teams_rosters(league_key)
        suggestions = generate_trade_suggestions(my_team, all_teams)
        return render_template("trades.html", suggestions=suggestions, my_team=my_team, league_key=league_key)
    except Exception as e:
        return render_template("error.html", error=str(e))

@app.route("/debug/players/<league_key>")
def debug_players(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    import json
    data = api.api_get(f"/league/{league_key}/players;out=stats", params={"sort": "AR", "start": 0, "count": 3, "format": "json"})
    return f"<pre>{json.dumps(data, indent=2)}</pre>"

@app.route("/draft/<league_key>")
def draft(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        settings = api.get_league_settings(league_key)
        my_team  = api.get_my_team(league_key)
        return render_template("draft.html", league_key=league_key,
                               settings=settings, my_team=my_team)
    except Exception as e:
        return render_template("error.html", error=str(e))

# Server-side cache for draft results (10-second TTL)
_draft_cache = {}
def _get_cached_draft(league_key):
    import time
    now = time.time()
    cached = _draft_cache.get(league_key)
    if cached and (now - cached["t"]) < 10:
        return cached["data"]
    data = api.get_draft_results(league_key)
    _draft_cache[league_key] = {"t": now, "data": data}
    return data

@app.route("/api/draft/<league_key>")
def api_draft(league_key):
    if "access_token" not in session:
        return jsonify({"error": "not authenticated"}), 401
    try:
        settings    = api.get_league_settings(league_key)
        player_pool = api.get_draft_player_pool(league_key)
        draft_results = _get_cached_draft(league_key)
        my_team     = api.get_my_team(league_key)
        all_teams   = api.get_all_league_teams(league_key)
        teams_dict  = {t["team_key"]: t for t in all_teams}

        result = draft_engine.get_recommendations(
            player_pool     = player_pool,
            draft_results   = draft_results,
            my_team_key     = my_team["team_key"] if my_team else "",
            teams_dict      = teams_dict,
            roster_positions= settings["roster_positions"],
            num_teams       = settings["num_teams"],
        )
        result["draft_status"] = settings.get("draft_status", "predraft")
        result["pick_time"]    = settings.get("draft_pick_time", 45)
        return jsonify(result)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/debug/draft/<league_key>")
def debug_draft(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    import json
    results = {}
    results["settings"] = api.api_get(f"/league/{league_key}/settings")
    results["draftresults"] = api.api_get(f"/league/{league_key}/draftresults")
    results["players_adp"] = api.api_get(f"/league/{league_key}/players", params={"sort": "AD", "count": 3})
    return f"<pre>{json.dumps(results, indent=2)}</pre>"

@app.route("/rankings/<league_key>")
def rankings(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        return render_template("rankings.html", league_key=league_key)
    except Exception as e:
        return render_template("error.html", error=str(e))

@app.route("/api/rankings/<league_key>")
def api_rankings(league_key):
    if "access_token" not in session:
        return jsonify({"error": "not authenticated"}), 401
    try:
        import mlb_stats as mlb
        player_pool = api.get_draft_player_pool(league_key)
        values = draft_engine.compute_player_values()
        norm = mlb._normalize

        pool_by_name = {norm(p["name"]): p for p in player_pool}

        # Build full ranked list from MLB Stats values universe
        all_players = []
        for name_key, val in values.items():
            pool_info = pool_by_name.get(name_key, {})
            is_batter = val["is_batter"]
            mlb_pos   = val["stats"].get("mlb_pos", "")
            mlb_elig  = val["stats"].get("fantasy_eligible", [])
            position  = pool_info.get("position") or mlb_pos or ("SP" if not is_batter else "OF")
            eligible  = pool_info.get("eligible_positions") or mlb_elig or draft_engine._default_eligible(is_batter, position)
            all_players.append({
                "name":               pool_info.get("name", name_key.title()),
                "team":               pool_info.get("team", ""),
                "headshot":           pool_info.get("headshot", ""),
                "position":           position,
                "eligible_positions": eligible,
                "yahoo_rank":         pool_info.get("yahoo_rank", 9999),
                "z_score":            val["total"],
                "cats":               val["cats"],
                "is_batter":          is_batter,
            })

        # Add Yahoo pool players not in values (rookies)
        for p in player_pool:
            nk = norm(p["name"])
            if nk not in values:
                is_batter = p.get("is_batter", True)
                all_players.append({
                    "name":               p["name"],
                    "team":               p.get("team", ""),
                    "headshot":           p.get("headshot", ""),
                    "position":           p.get("position", ""),
                    "eligible_positions": p.get("eligible_positions", []),
                    "yahoo_rank":         p.get("yahoo_rank", 9999),
                    "z_score":            0.0,
                    "cats":               {},
                    "is_batter":          is_batter,
                })

        # Sort by z_score → assign BPA rank
        all_players.sort(key=lambda x: x["z_score"], reverse=True)

        result = []
        for i, p in enumerate(all_players):
            bpa_rank = i + 1
            has_adp  = p["yahoo_rank"] < 900
            adp_rank = p["yahoo_rank"] if has_adp else None
            value    = (adp_rank - bpa_rank) if has_adp else None
            result.append({
                "rank":               bpa_rank,
                "name":               p["name"],
                "team":               p["team"],
                "headshot":           p["headshot"],
                "position":           p["position"],
                "eligible_positions": p["eligible_positions"],
                "adp":                adp_rank,
                "value":              value,
                "z_score":            round(p["z_score"], 2),
                "cats":               p["cats"],
                "is_batter":          p["is_batter"],
            })

        return jsonify({"players": result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/players/<league_key>")
def players(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    position = request.args.get("position", "ALL")
    sort_cat = request.args.get("sort", "z_score")
    start    = int(request.args.get("start", 0))
    try:
        import mlb_stats as mlb
        from yahoo_api import analyze_player
        player_pool = api.get_draft_player_pool(league_key)
        values      = draft_engine.compute_player_values()
        norm        = mlb._normalize
        pool_by_name = {norm(p["name"]): p for p in player_pool}

        # Build full list from MLB Stats universe
        all_players = []
        for name_key, val in values.items():
            pool_info = pool_by_name.get(name_key, {})
            is_batter = val["is_batter"]
            mlb_pos   = val["stats"].get("mlb_pos", "")
            mlb_elig  = val["stats"].get("fantasy_eligible", [])
            position_str = pool_info.get("position") or mlb_pos or ("SP" if not is_batter else "OF")
            eligible     = pool_info.get("eligible_positions") or mlb_elig or draft_engine._default_eligible(is_batter, position_str)

            # Position filter
            if position != "ALL" and position not in eligible and position != position_str:
                continue

            all_players.append({
                "name":       pool_info.get("name", name_key.title()),
                "position":   position_str,
                "team":       pool_info.get("team", ""),
                "headshot":   pool_info.get("headshot", ""),
                "is_batter":  is_batter,
                "stats":      val["stats"],
                "z_score":    val["total"],
                "cats":       val["cats"],
                "analysis":   analyze_player(val["stats"], is_batter),
                "percent_owned": pool_info.get("yahoo_rank", 999),
            })

        # Sort
        reverse = True
        if sort_cat == "ERA" or sort_cat == "WHIP":
            reverse = False
        if sort_cat == "z_score":
            all_players.sort(key=lambda p: p["z_score"], reverse=True)
        else:
            all_players.sort(key=lambda p: p["stats"].get(sort_cat) or 0, reverse=reverse)

        total        = len(all_players)
        player_list  = all_players[start:start + 50]
        return render_template("players.html", players=player_list, league_key=league_key,
                               position=position, sort=sort_cat, start=start, total=total)
    except Exception as e:
        import traceback; traceback.print_exc()
        return render_template("error.html", error=str(e))

@app.route("/schedule/<league_key>")
def schedule(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        from datetime import datetime
        year = datetime.today().year
        weekly = mlb_schedule.get_weekly_schedule(year)
        current_week = mlb_schedule.get_current_week_key()
        selected_week = request.args.get("week", current_week)
        week_data = weekly.get(selected_week, {})
        # Sort teams by game count descending
        sorted_teams = sorted(
            week_data.get("teams", {}).items(),
            key=lambda x: x[1]["count"], reverse=True
        )
        return render_template("schedule.html",
                               league_key=league_key,
                               weeks=weekly,
                               selected_week=selected_week,
                               week_data=week_data,
                               sorted_teams=sorted_teams,
                               current_week=current_week)
    except Exception as e:
        return render_template("error.html", error=str(e))

@app.route("/h2h/<league_key>")
def h2h_analysis(league_key):
    """Head-to-head matchup analysis with player strengths/weaknesses."""
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        all_teams = api.get_all_league_teams(league_key)
        my_team = next((t for t in all_teams if t["is_mine"]), None)

        # Get opponent team key from query param or find from matchup
        opp_key = request.args.get("opp_key")
        if not opp_key and my_team:
            matchup_data = api.get_current_matchup(my_team["team_key"])
            if matchup_data and matchup_data.get("teams"):
                for t in matchup_data["teams"]:
                    if t["team_key"] != my_team["team_key"]:
                        opp_key = t["team_key"]
                        break

        my_roster = api.get_team_roster_with_stats(my_team["team_key"]) if my_team else []
        opp_roster = api.get_team_roster_with_stats(opp_key) if opp_key else []
        opp_team = next((t for t in all_teams if t["team_key"] == opp_key), {})

        return render_template("h2h.html",
                               league_key=league_key,
                               all_teams=all_teams,
                               my_team=my_team,
                               opp_team=opp_team,
                               opp_key=opp_key,
                               my_roster=my_roster,
                               opp_roster=opp_roster)
    except Exception as e:
        return render_template("error.html", error=str(e))

def generate_trade_suggestions(my_team, all_teams):
    """Simple trade suggestion: find teams with excess at your weak positions."""
    if not my_team or not all_teams:
        return []
    suggestions = []
    my_roster = next((t for t in all_teams if t["team_key"] == my_team["team_key"]), None)
    if not my_roster:
        return []

    my_positions = {}
    for p in my_roster["players"]:
        for pos in p["position"].split(","):
            my_positions[pos.strip()] = my_positions.get(pos.strip(), 0) + 1

    for team in all_teams:
        if team["team_key"] == my_team["team_key"]:
            continue
        their_positions = {}
        for p in team["players"]:
            for pos in p["position"].split(","):
                their_positions[pos.strip()] = their_positions.get(pos.strip(), 0) + 1

        for pos in ["SP", "RP", "OF", "1B", "2B", "3B", "SS", "C"]:
            my_count = my_positions.get(pos, 0)
            their_count = their_positions.get(pos, 0)
            if their_count > my_count + 1:
                suggestions.append({
                    "team": team["name"],
                    "suggestion": f"對方在 {pos} 位置有多餘球員（{their_count} 人），你只有 {my_count} 人，可以考慮交易。"
                })
    return suggestions[:10]

# ── Scheduler ─────────────────────────────────────────────────

scheduler = BackgroundScheduler()

def scheduled_report():
    """Called by scheduler - needs stored tokens."""
    print("Running scheduled email report...")
    # Token will be read from a persisted file (see email_report.py)
    email_report.send_weekly_report()

scheduler.add_job(scheduled_report, "cron", day_of_week="mon", hour=8, minute=0)
scheduler.start()

# ── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
