import requests
from datetime import datetime, timedelta

MLB_API = "https://statsapi.mlb.com/api/v1"

def get_weekly_schedule(year=2026):
    """Fetch full MLB season schedule and group by fantasy week (Mon-Sun).

    Returns: {week_start_date: {
        "label": str,
        "end":   str,
        "teams": {team_abbr: {
            "count":   int,
            "name":    str,
            "opps":    [{"abbr": str, "home": bool}, ...],   # one entry per game
            "opp_summary": [{"abbr": str, "home": bool, "n": int}, ...], # dedup'd
        }, ...},
    }, ...}"""
    url = f"{MLB_API}/schedule"
    params = {
        "sportId":  1,
        "season":   year,
        "gameType": "R",
        "hydrate":  "team",
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    weeks = {}
    for date_entry in data.get("dates", []):
        date_str = date_entry["date"]
        date = datetime.strptime(date_str, "%Y-%m-%d")
        week_start = date - timedelta(days=date.weekday())
        week_key   = week_start.strftime("%Y-%m-%d")
        week_end   = (week_start + timedelta(days=6)).strftime("%Y-%m-%d")
        label      = f"{week_start.strftime('%m/%d')} - {(week_start + timedelta(days=6)).strftime('%m/%d')}"

        if week_key not in weeks:
            weeks[week_key] = {"label": label, "end": week_end, "teams": {}}

        for game in date_entry.get("games", []):
            home = game.get("teams", {}).get("home", {}).get("team", {})
            away = game.get("teams", {}).get("away", {}).get("team", {})
            home_abbr = home.get("abbreviation", "")
            away_abbr = away.get("abbreviation", "")
            if not (home_abbr and away_abbr):
                continue

            for own_abbr, own_name, opp_abbr, is_home in [
                (home_abbr, home.get("name", ""), away_abbr, True),
                (away_abbr, away.get("name", ""), home_abbr, False),
            ]:
                ti = weeks[week_key]["teams"].setdefault(
                    own_abbr, {"count": 0, "name": own_name, "opps": []}
                )
                ti["count"] += 1
                ti["opps"].append({"abbr": opp_abbr, "home": is_home})

    # Build deduplicated opponent summary per team/week (3-game series
    # against same opponent shows as one entry with n=3).
    for week_data in weeks.values():
        for ti in week_data["teams"].values():
            buckets = {}
            order   = []
            for o in ti["opps"]:
                k = (o["abbr"], o["home"])
                if k not in buckets:
                    buckets[k] = {"abbr": o["abbr"], "home": o["home"], "n": 0}
                    order.append(k)
                buckets[k]["n"] += 1
            ti["opp_summary"] = [buckets[k] for k in order]

    return dict(sorted(weeks.items()))

def get_current_week_key():
    today = datetime.today()
    week_start = today - timedelta(days=today.weekday())
    return week_start.strftime("%Y-%m-%d")

def get_team_schedule_for_week(week_data, mlb_team_abbr):
    """Get game count for a specific MLB team in a given week."""
    return week_data.get("teams", {}).get(mlb_team_abbr, {}).get("count", 0)
