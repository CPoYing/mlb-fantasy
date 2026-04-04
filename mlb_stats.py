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


def get_hot_players(days=7, season=2026, limit=8):
    """Fetch hottest hitters and pitchers from last X days.
    Falls back to 2025 season top performers if pre-season / no data."""
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=days)
    start_str = start.strftime("%m/%d/%Y")
    end_str   = end.strftime("%m/%d/%Y")

    def fetch_recent(group):
        try:
            r = requests.get(f"{MLB_API}/stats", params={
                "stats": "byDateRange",
                "startDate": start_str,
                "endDate": end_str,
                "group": group,
                "playerPool": "all",
                "sportId": 1,
                "season": season,
                "limit": 500,
            }, timeout=15)
            r.raise_for_status()
            return r.json().get("stats", [{}])[0].get("splits", [])
        except Exception:
            return []

    hit_splits = fetch_recent("hitting")
    pit_splits = fetch_recent("pitching")

    if not hit_splits and not pit_splits:
        hit_splits = _fetch_all("hitting", 2025)
        pit_splits = _fetch_all("pitching", 2025)
        source = "2025 整季"
    else:
        source = f"{start_str} – {end_str}"

    hitters = []
    for split in hit_splits:
        s = split.get("stat", {})
        name = split.get("player", {}).get("fullName", "")
        pos = split.get("position", {}).get("abbreviation", "")
        team = split.get("team", {}).get("name", "")
        hr  = _f(s.get("homeRuns")) or 0
        rbi = _f(s.get("rbi")) or 0
        r   = _f(s.get("runs")) or 0
        sb  = _f(s.get("stolenBases")) or 0
        avg = _f(s.get("avg")) or 0
        ops = _f(s.get("ops")) or 0
        g   = _f(s.get("gamesPlayed")) or 0
        if g < 2:
            continue
        score = hr * 4 + rbi * 1.5 + r + sb * 2 + avg * 50
        hitters.append({
            "name": name, "pos": pos, "team": team,
            "HR": int(hr), "RBI": int(rbi), "R": int(r), "SB": int(sb),
            "AVG": f"{avg:.3f}", "OPS": f"{ops:.3f}",
            "G": int(g), "score": score,
        })
    hitters.sort(key=lambda x: x["score"], reverse=True)

    pitchers = []
    for split in pit_splits:
        s = split.get("stat", {})
        name = split.get("player", {}).get("fullName", "")
        team = split.get("team", {}).get("name", "")
        w    = _f(s.get("wins")) or 0
        sv   = _f(s.get("saves")) or 0
        k    = _f(s.get("strikeOuts")) or 0
        era  = _f(s.get("era")) or 99
        whip = _f(s.get("whip")) or 99
        ip   = _f(s.get("inningsPitched")) or 0
        g    = int(_f(s.get("gamesPlayed")) or 0)
        gs   = int(_f(s.get("gamesStarted")) or 0)
        if ip < 1:
            continue
        role = "SP" if g > 0 and (gs / g) >= 0.5 else "RP"
        score = w * 5 + sv * 5 + k * 0.5 - era * 2 - whip * 3
        pitchers.append({
            "name": name, "pos": role, "team": team,
            "W": int(w), "SV": int(sv), "K": int(k),
            "ERA": f"{era:.2f}", "WHIP": f"{whip:.2f}", "IP": f"{ip:.1f}",
            "G": g, "score": score,
        })
    pitchers.sort(key=lambda x: x["score"], reverse=True)

    return {
        "hitters": hitters[:limit],
        "pitchers": pitchers[:limit],
        "source": source,
    }


def _f(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
