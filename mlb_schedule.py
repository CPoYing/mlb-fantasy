import requests
from datetime import datetime, timedelta

MLB_API = "https://statsapi.mlb.com/api/v1"

def get_weekly_schedule(year=2026):
    """
    Fetch full MLB season schedule and group by fantasy week (Mon-Sun).
    Returns: {week_start_date: {team_abbr: game_count}}
    """
    url = f"{MLB_API}/schedule"
    params = {
        "sportId": 1,
        "season": year,
        "gameType": "R",
        "hydrate": "team"
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()

    weeks = {}
    for date_entry in data.get("dates", []):
        date_str = date_entry["date"]
        date = datetime.strptime(date_str, "%Y-%m-%d")
        # Fantasy week: Monday to Sunday
        week_start = date - timedelta(days=date.weekday())
        week_key = week_start.strftime("%Y-%m-%d")
        week_end = (week_start + timedelta(days=6)).strftime("%Y-%m-%d")
        label = f"{week_start.strftime('%m/%d')} - {(week_start + timedelta(days=6)).strftime('%m/%d')}"

        if week_key not in weeks:
            weeks[week_key] = {"label": label, "end": week_end, "teams": {}}

        for game in date_entry.get("games", []):
            home = game.get("teams", {}).get("home", {}).get("team", {})
            away = game.get("teams", {}).get("away", {}).get("team", {})
            for team in [home, away]:
                abbr = team.get("abbreviation", "")
                name = team.get("name", "")
                if abbr:
                    if abbr not in weeks[week_key]["teams"]:
                        weeks[week_key]["teams"][abbr] = {"count": 0, "name": name}
                    weeks[week_key]["teams"][abbr]["count"] += 1

    # Sort weeks chronologically
    return dict(sorted(weeks.items()))

def get_current_week_key():
    today = datetime.today()
    week_start = today - timedelta(days=today.weekday())
    return week_start.strftime("%Y-%m-%d")

def get_team_schedule_for_week(week_data, mlb_team_abbr):
    """Get game count for a specific MLB team in a given week."""
    return week_data.get("teams", {}).get(mlb_team_abbr, {}).get("count", 0)
