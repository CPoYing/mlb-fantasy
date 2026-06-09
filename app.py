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
    Returns (batters, pitchers) — pitchers carry a `role` field
    (SP / CP / SU / RP) so callers can group / filter them."""
    norm = mlb_stats._normalize
    bv, pv = values
    batters, pitchers = [], []
    for p in roster:
        src = bv if p["is_batter"] else pv
        v   = src.get(norm(p["name"]), {})
        role = v.get("role") if not p["is_batter"] else None
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
            "role":      role,
            "tier":      v.get("tier"),
            "stat_keys": (BATTING_CATS if p["is_batter"]
                          else player_values.ROLE_CATS.get(role, PITCHING_CATS)),
            "adv_keys":  BATTING_ADV  if p["is_batter"] else PITCHING_ADV,
        }
        (batters if p["is_batter"] else pitchers).append(row)
    batters.sort(key=lambda x: x["total"] or -99, reverse=True)
    pitchers.sort(key=lambda x: x["total"] or -99, reverse=True)
    return batters, pitchers


def _group_pitchers_by_role(pitchers):
    """Split a flat pitchers list into {SP/CP/SU/RP: [pitchers]}.
    Unknown role (e.g., no stats yet) falls into RP middle bucket."""
    groups = {"SP": [], "CP": [], "SU": [], "RP": []}
    for p in pitchers:
        groups.get(p.get("role"), groups["RP"]).append(p)
    return groups


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
                pbr = _group_pitchers_by_role(pitchers)

                role_display = [
                    ("SP", "先發",       pbr["SP"], player_values.SP_CATS),
                    ("CP", "終結者",     pbr["CP"], player_values.CP_CATS),
                    ("SU", "建中 setup", pbr["SU"], player_values.SU_CATS),
                    ("RP", "中繼 middle", pbr["RP"], player_values.RP_CATS),
                ]
                role_blocks = [
                    {
                        "role":     role,
                        "label":    label,
                        "players":  players,
                        "cats":     cats,
                        "totals":   _roster_cat_totals(players, cats),
                        "team_z":   _team_total(players),
                    }
                    for role, label, players, cats in role_display
                    if players  # skip empty roles
                ]

                my_team_display = {
                    "team":           my_team,
                    "batters":        batters,
                    "league_key":     league_key,
                    "batter_totals":  _roster_cat_totals(batters,  BATTING_CATS),
                    "batter_team_z":  _team_total(batters),
                    "pitcher_role_blocks": role_blocks,
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
        # Normalize week to str so template/view comparisons don't bite us
        # when Yahoo returns int vs string inconsistently.
        for m in all_matchups:
            m["week"] = str(m.get("week", "")) if m.get("week") is not None else ""
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

        # Real Yahoo-side weekly accumulated stats for the selected week.
        # Lets the matchup page show "you have 5 HR, opp has 3 HR" rather
        # than purely z-score estimates.
        settings = api.get_league_settings(league_key)
        week_no  = scoreboard.get("week")
        real_my  = api.get_team_week_stats(my_team["team_key"], week_no, settings["cat_by_id"]) if week_no else {}
        real_opp = api.get_team_week_stats(opp_key, week_no, settings["cat_by_id"]) if (week_no and opp_key) else {}

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
            real_my=real_my,
            real_opp=real_opp,
            league_settings=settings,
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
        # don't happen. Each pitcher role (SP / CP / SU / RP) is scored
        # against its own norm pool with its own cats, so an FA can only
        # fairly be compared to my weakest player in the same role.
        my_worst_bat_by_pos    = {}  # batter position → (z, name)
        my_worst_pitcher_by_role = {}  # "SP" / "CP" / "SU" / "RP" → (z, name)
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
        # 2026 only — for the small-sample warning (we want current-season
        # sample size, not the 2025-padded merged version).
        hit_2026 = mlb_stats.get_hitting_stats_by_name(2026)
        pit_2026 = mlb_stats.get_pitching_stats_by_name(2026)

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
            # Prefer Yahoo's eligible_positions (gold standard for our league
            # — has the full multi-position list like "OF,SS,2B"). Fall back to
            # MLB Stats primary-derived eligibility, then to a positional default.
            eligible    = (fa.get("eligible_positions")
                           or stats.get("fantasy_eligible")
                           or player_values.default_eligible(is_batter, fa["position"]))

            # Small-sample warning (current season only): hitter PA < 50 or pitcher IP < 10.
            cur_2026 = hit_2026.get(nk, {}) if is_batter else pit_2026.get(nk, {})
            if is_batter:
                cur_volume   = cur_2026.get("PA") or 0
                small_sample = cur_volume < 50
            else:
                cur_volume   = cur_2026.get("IP") or 0
                small_sample = cur_volume < 10

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
                "small_sample":  small_sample,
                "cur_volume":    cur_volume,
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
        # Statcast x-stats (xAVG, xSLG, xwOBA) — for batters, these
        # are their own expected outcomes; for pitchers, they're batter
        # outcomes against, so lower = better.
        x_hit = mlb_stats.get_expected_stats(2026, "hitting")
        x_pit = mlb_stats.get_expected_stats(2026, "pitching")

        def _row(name_key, val):
            stats   = val["stats"]
            mlb_pos = stats.get("mlb_pos", "")
            eligible = stats.get("fantasy_eligible") or player_values.default_eligible(
                val["is_batter"], mlb_pos
            )
            primary = mlb_pos or ("SP" if not val["is_batter"] else "OF")
            adv_keys = (["ISO", "BB_pct", "K_pct", "BABIP", "SLG"]
                        if val["is_batter"]
                        else ["K9", "BB9", "FIP", "K_BB_pct", "BABIP"])
            adv = {k: stats.get(k) for k in adv_keys}
            x_src = x_hit if val["is_batter"] else x_pit
            adv.update(x_src.get(name_key, {}))
            return {
                "name":               stats.get("name", name_key.title()),
                "player_id":          stats.get("player_id"),
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


@app.route("/player/<int:player_id>")
def player_detail(player_id):
    """Per-player detail page: bio, season summary, last 10 game log."""
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        info = mlb_stats.get_player_info(player_id)
        is_pitcher = info.get("primary_pos") in ("P", "SP", "RP", "TWP")
        # TWP shows both sides; for v1 default to whichever has data
        group = "pitching" if is_pitcher else "hitting"
        game_log = mlb_stats.get_player_game_log(player_id, season=2026, group=group, limit=10)
        game_log_prev = mlb_stats.get_player_game_log(player_id, season=2025, group=group, limit=5)

        # Look up the player in player_values for cats / z if we have them
        bv, pv = player_values.compute_player_values()
        norm   = mlb_stats._normalize
        nk     = norm(info.get("name", ""))
        val    = (pv if is_pitcher else bv).get(nk, {})

        leagues    = api.get_user_leagues()
        league_key = leagues[0]["league_key"] if leagues else None
        return render_template(
            "player.html",
            info=info,
            is_pitcher=is_pitcher,
            game_log=game_log,
            game_log_prev=game_log_prev,
            val=val,
            league_key=league_key,
            active_page="player",
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return render_template("error.html", error=str(e))


@app.route("/prospects")
def prospects():
    """Top 100 minor-league prospects ranked by level-weighted composite z."""
    if "access_token" not in session:
        return redirect(url_for("login"))
    try:
        data = player_values.compute_prospect_rankings(top_n=100)
        # Recently-called-up players — mark with 🆙 chip so the user
        # knows to watch them on the MLB side (they may not yet have
        # enough MLB sample to be filtered out of the prospects list).
        callup_names = mlb_stats.get_recent_callups(days=14)
        norm = mlb_stats._normalize
        for group in (data["hitters"], data["pitchers"]):
            for p in group:
                p["is_callup"] = norm(p["name"]) in callup_names

        leagues    = api.get_user_leagues()
        league_key = leagues[0]["league_key"] if leagues else None
        return render_template(
            "prospects.html",
            hitters=data["hitters"],
            pitchers=data["pitchers"],
            league_key=league_key,
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
