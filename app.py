import os
from flask import Flask, redirect, request, session, url_for, render_template, jsonify
from requests_oauthlib import OAuth2Session
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
import yahoo_api as api
import email_report
import mlb_schedule
import mlb_stats
import player_values

load_dotenv()

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "0"

app = Flask(__name__)
_secret = os.getenv("SECRET_KEY")
if not _secret:
    raise RuntimeError("SECRET_KEY env var is required. Set it in .env before starting.")
app.secret_key = _secret

CLIENT_ID = os.getenv("YAHOO_CLIENT_ID")
CLIENT_SECRET = os.getenv("YAHOO_CLIENT_SECRET")
REDIRECT_URI = os.getenv("YAHOO_REDIRECT_URI")

AUTHORIZATION_BASE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"

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
        authorization_response=request.url,
    )
    session["access_token"] = token["access_token"]
    session["refresh_token"] = token.get("refresh_token", "")
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ── Helpers ────────────────────────────────────────────────────

# League scoring categories (7x7 H2H)
BATTING_CATS  = ["HR", "RBI", "SB", "AVG", "OBP", "OPS", "E"]
PITCHING_CATS = ["W", "BB", "HLD", "SV", "K", "ERA", "WHIP"]

# Reference stats shown beside the categories (FanGraphs / Savant flavor)
BATTING_ADV   = ["ISO", "BB_pct", "K_pct", "BABIP", "SLG"]
PITCHING_ADV  = ["K9", "BB9", "FIP", "K_BB_pct", "BABIP"]


def _build_roster_view(roster, values):
    """Attach z-score totals + cat z-scores to a roster list.
    `values` is the tuple (batter_values, pitcher_values) returned by
    player_values.compute_player_values(). Yahoo's display_position
    (passed in as `p["is_batter"]`) is the source of truth for which dict
    to consult, so two-way players get the correct categories."""
    norm = mlb_stats._normalize
    bv, pv = values
    batters, pitchers = [], []
    for p in roster:
        src = bv if p["is_batter"] else pv
        v   = src.get(norm(p["name"]), {})
        row = {
            "name":      p["name"],
            "position":  p["position"],
            "team":      p["team"],
            "headshot":  p.get("headshot", ""),
            "is_batter": p["is_batter"],
            "stats":     p.get("stats", {}),
            "total":     round(v["total"], 2) if v.get("total") is not None else None,
            "cats":      v.get("cats", {}),
            "analysis":  p.get("analysis", {"strengths": [], "weaknesses": []}),
            "stat_keys": BATTING_CATS if p["is_batter"] else PITCHING_CATS,
            "adv_keys":  BATTING_ADV  if p["is_batter"] else PITCHING_ADV,
        }
        (batters if p["is_batter"] else pitchers).append(row)
    batters.sort(key=lambda x: x["total"] or -99, reverse=True)
    pitchers.sort(key=lambda x: x["total"] or -99, reverse=True)
    return batters, pitchers


def _roster_cat_totals(roster_view, cats=None):
    """Sum z-scores per category for a roster (batters or pitchers).
    If `cats` is given, returns a dict keyed by every cat (with 0.0 for any
    cat that no roster member contributes to), so the totals row in the
    UI lines up with the table headers."""
    totals = {c: 0.0 for c in cats} if cats else {}
    for p in roster_view:
        for cat, z in p.get("cats", {}).items():
            totals[cat] = totals.get(cat, 0.0) + z
    return {k: round(v, 2) for k, v in totals.items()}


def _team_total(roster_view):
    """Sum of individual totals — overall team strength."""
    return round(sum((p.get("total") or 0) for p in roster_view), 2)


# ── Pages ──────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        leagues     = api.get_user_leagues()
        hot_players = mlb_stats.get_hot_players(days=7)

        my_team_display = None
        if leagues:
            league_key = leagues[0]["league_key"]
            my_team    = api.get_my_team(league_key)
            if my_team:
                roster  = api.get_team_roster_with_stats(my_team["team_key"])
                values  = player_values.compute_player_values()
                batters, pitchers = _build_roster_view(roster, values)
                my_team_display = {
                    "team":           my_team,
                    "batters":        batters,
                    "pitchers":       pitchers,
                    "league_key":     league_key,
                    "batter_totals":  _roster_cat_totals(batters,  BATTING_CATS),
                    "pitcher_totals": _roster_cat_totals(pitchers, PITCHING_CATS),
                    "batter_team_z":  _team_total(batters),
                    "pitcher_team_z": _team_total(pitchers),
                }

        return render_template(
            "dashboard.html",
            leagues=leagues,
            hot_players=hot_players,
            my_team_display=my_team_display,
            league_key=(leagues[0]["league_key"] if leagues else None),
            active_page="dashboard",
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return render_template("error.html", error=str(e))


@app.route("/matchup/<league_key>")
def matchup(league_key):
    """Weekly scoreboard + H2H roster deep-dive against any chosen opponent.

    Query params:
      week     — pick a specific week (default: this week)
      opp_key  — explicit opponent (default: opponent in the selected week)
    """
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        all_teams = api.get_all_league_teams(league_key)
        my_team   = next((t for t in all_teams if t["is_mine"]), None)
        if not my_team:
            return render_template("error.html", error="找不到你的隊伍。")

        # All weeks for the season (one Yahoo call), pick which to show.
        all_matchups = api.get_team_all_matchups(my_team["team_key"])
        current      = next((m for m in all_matchups if m["is_current_week"]), None)
        if not current:
            current = (next((m for m in all_matchups if m["status"] == "midevent"), None)
                       or next((m for m in reversed(all_matchups) if m["status"] == "postevent"), None)
                       or (all_matchups[0] if all_matchups else None))

        week_arg = request.args.get("week", "").strip()
        selected = next((m for m in all_matchups if m["week"] == week_arg), current) if week_arg else current
        scoreboard = selected or {}

        # Opponent for H2H deep-dive — explicit param wins, otherwise
        # use whoever I'm matched against in the selected week.
        opp_key = request.args.get("opp_key", "").strip()
        if not opp_key and scoreboard.get("teams"):
            for t in scoreboard["teams"]:
                if t["team_key"] != my_team["team_key"]:
                    opp_key = t["team_key"]
                    break

        values     = player_values.compute_player_values()
        my_roster  = api.get_team_roster_with_stats(my_team["team_key"])
        my_bat, my_pit = _build_roster_view(my_roster, values)

        opp_team   = next((t for t in all_teams if t["team_key"] == opp_key), None)
        opp_bat, opp_pit = [], []
        if opp_key:
            opp_roster = api.get_team_roster_with_stats(opp_key)
            opp_bat, opp_pit = _build_roster_view(opp_roster, values)

        my_cats  = {**_roster_cat_totals(my_bat),  **_roster_cat_totals(my_pit)}
        opp_cats = {**_roster_cat_totals(opp_bat), **_roster_cat_totals(opp_pit)}

        return render_template(
            "matchup.html",
            league_key=league_key,
            scoreboard=scoreboard,
            all_matchups=all_matchups,
            current_week=current["week"] if current else None,
            selected_week=scoreboard.get("week"),
            my_team=my_team,
            opp_team=opp_team,
            opp_key=opp_key,
            all_teams=all_teams,
            my_batters=my_bat,
            my_pitchers=my_pit,
            opp_batters=opp_bat,
            opp_pitchers=opp_pit,
            my_cats=my_cats,
            opp_cats=opp_cats,
            batting_cats=BATTING_CATS,
            pitching_cats=PITCHING_CATS,
            active_page="matchup",
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return render_template("error.html", error=str(e))


@app.route("/waiver/<league_key>")
def waiver(league_key):
    """Waiver / FA recommendations — focused on marginal upgrade to my roster."""
    if "access_token" not in session:
        return redirect(url_for("login"))
    position = request.args.get("position", "B")
    try:
        # FA pool (Yahoo)
        fa_pool = api.get_free_agents(league_key, position=position, count=50)

        # My roster (for marginal upgrade)
        all_teams = api.get_all_league_teams(league_key)
        my_team   = next((t for t in all_teams if t["is_mine"]), None)
        my_roster = api.get_team_roster_with_stats(my_team["team_key"]) if my_team else []

        bv, pv    = player_values.compute_player_values()
        norm      = mlb_stats._normalize

        # Worst z on my roster — split by role so cross-role comparisons
        # don't happen (SP and RP are scored against different norms).
        my_worst_bat_by_pos    = {}  # batter position → (z, name)
        my_worst_pitcher_by_role = {}  # "SP" or "RP" → (z, name)
        for p in my_roster:
            src = bv if p["is_batter"] else pv
            v = src.get(norm(p["name"]), {})
            z = v.get("total")
            if z is None:
                continue
            if p["is_batter"]:
                for pos in (p.get("eligible_positions") or [p.get("position", "")]):
                    if pos in ("BN", "IL"):
                        continue
                    cur = my_worst_bat_by_pos.get(pos)
                    if cur is None or z < cur[0]:
                        my_worst_bat_by_pos[pos] = (z, p["name"])
            else:
                role = v.get("role")  # "SP" or "RP"
                if role:
                    cur = my_worst_pitcher_by_role.get(role)
                    if cur is None or z < cur[0]:
                        my_worst_pitcher_by_role[role] = (z, p["name"])

        # Hot players (last 7 days) — flag matching FAs
        hot = mlb_stats.get_hot_players(days=7)
        hot_names = set()
        for grp in ("hitters", "pitchers"):
            for h in hot.get(grp, []):
                hot_names.add(norm(h.get("name", "")))

        # MLB merged stats by name (covers FAs that aren't qualified for values)
        hitting  = mlb_stats.get_hitting_stats_merged()
        pitching = mlb_stats.get_pitching_stats_merged()

        enriched = []
        for fa in fa_pool:
            nk          = norm(fa["name"])
            # Yahoo display_position is the source of truth for FA classification.
            # Use the helper so compound positions like "SP,RP" classify correctly.
            is_batter   = api.is_batter_position(fa["position"])
            src         = bv if is_batter else pv
            v           = src.get(nk, {})
            stats       = v.get("stats") or (hitting.get(nk, {}) if is_batter else pitching.get(nk, {}))
            z_total     = v.get("total")
            cats        = v.get("cats", {})
            analysis    = api.analyze_player(stats, is_batter)
            eligible    = stats.get("fantasy_eligible") or player_values.default_eligible(is_batter, fa["position"])

            # Marginal upgrade — compare within the right pool only:
            # batter FAs vs my worst batter at each eligible position;
            # SP FAs vs my worst SP; RP FAs vs my worst RP. Cross-role
            # comparison would be unfair (different cat sets / norms).
            upgrade = None
            upgrade_over = None
            if z_total is not None:
                if is_batter:
                    for pos in eligible:
                        if pos in ("BN", "IL"):
                            continue
                        cur = my_worst_bat_by_pos.get(pos)
                        if cur is None:
                            continue
                        delta = z_total - cur[0]
                        if upgrade is None or delta > upgrade:
                            upgrade = round(delta, 2)
                            upgrade_over = f"{cur[1]} ({pos})"
                else:
                    role = v.get("role")
                    cur  = my_worst_pitcher_by_role.get(role) if role else None
                    if cur is not None:
                        upgrade = round(z_total - cur[0], 2)
                        upgrade_over = f"{cur[1]} ({role})"

            enriched.append({
                "name":          fa["name"],
                "position":      fa["position"],
                "eligible":      [p for p in eligible if p not in ("BN", "IL")],
                "team":          fa["team"],
                "status":        fa["status"],
                "percent_owned": fa["percent_owned"],
                "is_batter":     is_batter,
                "role":          v.get("role"),
                "tier":          v.get("tier"),
                "stats":         stats,
                "z_total":       round(z_total, 2) if z_total is not None else None,
                "cats":          cats,
                "analysis":      analysis,
                "upgrade":       upgrade,
                "upgrade_over":  upgrade_over,
                "is_hot":        nk in hot_names,
            })

        # Sort: best marginal upgrade first; fall back to z_total; fall back to percent_owned
        enriched.sort(key=lambda p: (
            -(p["upgrade"] if p["upgrade"] is not None else -99),
            -(p["z_total"] if p["z_total"] is not None else -99),
            -float(p["percent_owned"] or 0),
        ))

        return render_template(
            "waiver.html",
            players=enriched,
            league_key=league_key,
            position=position,
            batting_cats=BATTING_CATS,
            pitching_cats=PITCHING_CATS,
            my_team=my_team,
            active_page="waiver",
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return render_template("error.html", error=str(e))


@app.route("/rankings/<league_key>")
def rankings(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    return render_template("rankings.html", league_key=league_key, active_page="rankings")


@app.route("/api/rankings/<league_key>")
def api_rankings(league_key):
    if "access_token" not in session:
        return jsonify({"error": "not authenticated"}), 401
    try:
        bv, pv = player_values.compute_player_values()

        def _row(name_key, val):
            stats   = val["stats"]
            mlb_pos = stats.get("mlb_pos", "")
            eligible = stats.get("fantasy_eligible") or player_values.default_eligible(
                val["is_batter"], mlb_pos
            )
            primary = mlb_pos or ("SP" if not val["is_batter"] else "OF")
            # Filter advanced stats to display only the meaningful ones
            adv_keys = (["ISO", "BB_pct", "K_pct", "BABIP", "SLG"]
                        if val["is_batter"]
                        else ["K9", "BB9", "FIP", "K_BB_pct", "BABIP"])
            adv = {k: stats.get(k) for k in adv_keys}
            return {
                "name":               name_key.title(),
                "position":           primary,
                "eligible_positions": eligible,
                "z_score":            round(val["total"], 2),
                "cats":               val["cats"],
                "adv":                adv,
                "source":             val.get("source", ""),
                "role":               val.get("role", ""),
                "tier":               val.get("tier", ""),
                "is_batter":          val["is_batter"],
            }

        rows = [_row(n, v) for n, v in bv.items()] + [_row(n, v) for n, v in pv.items()]
        rows.sort(key=lambda x: x["z_score"], reverse=True)
        result = [{**r, "rank": i + 1} for i, r in enumerate(rows)]
        return jsonify({
            "players":       result,
            "batting_cats":  BATTING_CATS,
            "pitching_cats": PITCHING_CATS,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/prospects")
def prospects():
    """Top 100 minor-league prospects ranked by level-weighted composite z."""
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        data = player_values.compute_prospect_rankings(top_n=100)
        return render_template(
            "prospects.html",
            hitters=data["hitters"],
            pitchers=data["pitchers"],
            active_page="prospects",
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return render_template("error.html", error=str(e))


@app.route("/schedule/<league_key>")
def schedule(league_key):
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        from datetime import datetime
        year          = datetime.today().year
        weekly        = mlb_schedule.get_weekly_schedule(year)
        current_week  = mlb_schedule.get_current_week_key()
        selected_week = request.args.get("week", current_week)
        week_data     = weekly.get(selected_week, {})
        sorted_teams  = sorted(
            week_data.get("teams", {}).items(),
            key=lambda x: x[1]["count"], reverse=True
        )
        return render_template(
            "schedule.html",
            league_key=league_key,
            weeks=weekly,
            selected_week=selected_week,
            week_data=week_data,
            sorted_teams=sorted_teams,
            current_week=current_week,
            active_page="schedule",
        )
    except Exception as e:
        return render_template("error.html", error=str(e))


# ── Scheduler ─────────────────────────────────────────────────

scheduler = BackgroundScheduler()


def scheduled_report():
    print("Running scheduled email report...")
    email_report.send_weekly_report()


scheduler.add_job(scheduled_report, "cron", day_of_week="mon", hour=8, minute=0)
scheduler.start()


# ── Run ────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
