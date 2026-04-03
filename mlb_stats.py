"""
Fetch season stats from MLB Stats API (free, no auth required).
Index by player full name. Paginated to get all players.
"""
import requests

MLB_API = "https://statsapi.mlb.com/api/v1"
_cache  = {}

def _fetch_all(group, season=2025):
    key = f"{group}_{season}"
    if key in _cache:
        return _cache[key]

    all_splits = []
    offset     = 0
    page_size  = 1000

    while True:
        r = requests.get(f"{MLB_API}/stats", params={
            "stats":      "season",
            "season":     season,
            "group":      group,
            "playerPool": "all",
            "sportId":    1,
            "limit":      page_size,
            "offset":     offset,
        }, timeout=45)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        all_splits.extend(splits)
        if len(splits) < page_size:
            break
        offset += page_size

    _cache[key] = all_splits
    return all_splits


def _normalize(name):
    import unicodedata
    n = unicodedata.normalize("NFD", name)
    return "".join(c for c in n if unicodedata.category(c) != "Mn").lower().strip()


# MLB position abbreviation → fantasy-eligible positions
_POS_MAP = {
    "C":  ["C",  "Util"],
    "1B": ["1B", "Util"],
    "2B": ["2B", "Util"],
    "3B": ["3B", "Util"],
    "SS": ["SS", "Util"],
    "LF": ["OF", "Util"],
    "CF": ["OF", "Util"],
    "RF": ["OF", "Util"],
    "OF": ["OF", "Util"],
    "DH": ["Util"],
    "SP": ["SP", "P"],
    "RP": ["RP", "P"],
    "P":  ["P"],
}

def mlb_pos_to_fantasy(mlb_abbr, is_starter=None):
    """Convert MLB position abbreviation to fantasy eligible list."""
    if mlb_abbr == "P":
        if is_starter is True:
            return ["SP", "P"]
        if is_starter is False:
            return ["RP", "P"]
        return ["P"]
    return _POS_MAP.get(mlb_abbr, ["Util"])


def get_hitting_stats_by_name(season=2025):
    splits = _fetch_all("hitting", season)
    result = {}
    for split in splits:
        name  = split.get("player", {}).get("fullName", "")
        s     = split.get("stat", {})
        games = s.get("gamesPlayed", 0) or 0
        if games < 5:
            continue
        mlb_abbr = split.get("position", {}).get("abbreviation", "")
        result[_normalize(name)] = {
            "AVG":     _f(s.get("avg")),
            "HR":      _f(s.get("homeRuns")),
            "RBI":     _f(s.get("rbi")),
            "R":       _f(s.get("runs")),
            "SB":      _f(s.get("stolenBases")),
            "OBP":     _f(s.get("obp")),
            "OPS":     _f(s.get("ops")),
            "G":       float(games),
            "mlb_pos": mlb_abbr,                        # e.g. "3B", "CF", "C"
            "fantasy_eligible": mlb_pos_to_fantasy(mlb_abbr),
        }
    return result


def get_pitching_stats_by_name(season=2025):
    splits = _fetch_all("pitching", season)
    result = {}
    for split in splits:
        name = split.get("player", {}).get("fullName", "")
        s    = split.get("stat", {})
        ip   = _f(s.get("inningsPitched"))
        if ip is None or ip < 2:
            continue
        games   = s.get("gamesPlayed", 0) or 0
        started = s.get("gamesStarted", 0) or 0
        is_sp   = (started / games) >= 0.5 if games > 0 else False
        eligible = ["SP", "P"] if is_sp else ["RP", "P"]
        result[_normalize(name)] = {
            "ERA":      _f(s.get("era")),
            "WHIP":     _f(s.get("whip")),
            "K":        _f(s.get("strikeOuts")),
            "W":        _f(s.get("wins")),
            "SV":       _f(s.get("saves")),
            "IP":       ip,
            "GS":       float(started),
            "G":        float(games),
            "mlb_pos":  "SP" if is_sp else "RP",
            "fantasy_eligible": eligible,
        }
    return result


def _f(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
