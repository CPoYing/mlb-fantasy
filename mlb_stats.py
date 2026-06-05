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


FIP_CONSTANT = 3.10   # approximate; ~ league-avg FIP adjuster (varies by season ~3.0–3.2)


def _div(num, den):
    if num is None or den is None or den == 0:
        return None
    return num / den


def get_hitting_stats_by_name(season=2025):
    """Return {norm_name: stat_dict} for hitters with ≥5 G in the season.
    Stat dict carries both 5x5 categories and advanced derived metrics
    (ISO, BB%, K%, BABIP, SLG).
    """
    splits = _fetch_all("hitting", season)
    result = {}
    for split in splits:
        name  = split.get("player", {}).get("fullName", "")
        s     = split.get("stat", {})
        games = s.get("gamesPlayed", 0) or 0
        if games < 5:
            continue
        mlb_abbr = split.get("position", {}).get("abbreviation", "")

        avg = _f(s.get("avg"))
        slg = _f(s.get("slg"))
        obp = _f(s.get("obp"))
        ops = _f(s.get("ops"))
        bb  = _f(s.get("baseOnBalls"))
        k   = _f(s.get("strikeOuts"))
        pa  = _f(s.get("plateAppearances"))
        babip = _f(s.get("babip"))

        iso = (slg - avg) if (slg is not None and avg is not None) else None
        bb_pct = _div(bb, pa)
        k_pct  = _div(k,  pa)

        result[_normalize(name)] = {
            # League categories (7x7)
            "HR":      _f(s.get("homeRuns")),
            "RBI":     _f(s.get("rbi")),
            "SB":      _f(s.get("stolenBases")),
            "AVG":     avg,
            "OBP":     obp,
            "OPS":     ops,
            "E":       _f(s.get("errors")),
            # Reference
            "R":       _f(s.get("runs")),
            "SLG":     slg,
            # Advanced (Savant / FanGraphs style)
            "ISO":     round(iso, 3) if iso is not None else None,
            "BB_pct":  round(bb_pct, 3) if bb_pct is not None else None,
            "K_pct":   round(k_pct,  3) if k_pct  is not None else None,
            "BABIP":   babip,
            # Volume
            "PA":      pa,
            "BB":      bb,
            "K":       k,
            "G":       float(games),
            # Position
            "mlb_pos": mlb_abbr,
            "fantasy_eligible": mlb_pos_to_fantasy(mlb_abbr),
        }
    return result


def get_pitching_stats_by_name(season=2025):
    """Return {norm_name: stat_dict} for pitchers with ≥2 IP in the season.
    Adds K/9, BB/9, K-BB%, FIP, BABIP on top of 5x5.
    """
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

        k    = _f(s.get("strikeOuts"))
        bb   = _f(s.get("baseOnBalls"))
        hr   = _f(s.get("homeRuns"))
        bf   = _f(s.get("battersFaced"))
        era  = _f(s.get("era"))
        whip = _f(s.get("whip"))
        babip = _f(s.get("babip"))

        k9    = _div((k  or 0) * 9, ip)
        bb9   = _div((bb or 0) * 9, ip)
        k_bb  = _div((k or 0) - (bb or 0), bf) if bf else None
        fip   = ((13 * (hr or 0) + 3 * (bb or 0) - 2 * (k or 0)) / ip + FIP_CONSTANT) if ip else None

        result[_normalize(name)] = {
            # League categories (7x7)
            "W":        _f(s.get("wins")),
            "BB":       bb,
            "HLD":      _f(s.get("holds")),
            "SV":       _f(s.get("saves")),
            "K":        k,
            "ERA":      era,
            "WHIP":     whip,
            # Advanced (Savant / FanGraphs style)
            "K9":       round(k9,   2) if k9   is not None else None,
            "BB9":      round(bb9,  2) if bb9  is not None else None,
            "K_BB_pct": round(k_bb, 3) if k_bb is not None else None,
            "FIP":      round(fip,  2) if fip  is not None else None,
            "BABIP":    babip,
            # Volume
            "IP":       ip,
            "BF":       bf,
            "HR":       hr,
            "GS":       float(started),
            "G":        float(games),
            # Position
            "mlb_pos":  "SP" if is_sp else "RP",
            "fantasy_eligible": eligible,
        }
    return result


def get_player_positions(season=2026):
    """Return {norm_name: mlb_abbr} for all players in the given season (no game filter)."""
    positions = {}
    for group in ["hitting", "pitching"]:
        try:
            splits = _fetch_all(group, season)
            for split in splits:
                name = split.get("player", {}).get("fullName", "")
                pos  = split.get("position", {}).get("abbreviation", "")
                if name and pos:
                    n = _normalize(name)
                    if n not in positions:
                        positions[n] = pos
        except Exception:
            pass
    return positions


def _apply_positions(stats_dict, positions):
    """Overwrite mlb_pos / fantasy_eligible in-place using a positions map."""
    for name, s in stats_dict.items():
        if name in positions:
            new_pos = positions[name]
            s["mlb_pos"] = new_pos
            is_sp = s.get("GS", 0) > 0 and not s.get("AVG")  # rough pitcher check
            s["fantasy_eligible"] = mlb_pos_to_fantasy(new_pos, is_sp if new_pos == "P" else None)
    return stats_dict


_merged_hitting_cache  = {}
_merged_pitching_cache = {}


def get_hitting_stats_merged(current=2026, fallback=2025):
    """Prefer current-season stats (≥5 games), fall back to previous season.
    Always uses current-season positions regardless of game count.
    Memoized per (current, fallback) pair."""
    key = (current, fallback)
    if key in _merged_hitting_cache:
        return _merged_hitting_cache[key]
    cur  = get_hitting_stats_by_name(current)
    prev = get_hitting_stats_by_name(fallback)
    result = {k: dict(v) for k, v in prev.items()}
    for name, s in cur.items():
        if (s.get("G") or 0) >= 5:
            result[name] = dict(s)
    _apply_positions(result, get_player_positions(current))
    _merged_hitting_cache[key] = result
    return result


def get_pitching_stats_merged(current=2026, fallback=2025):
    """Prefer current-season stats (≥2 IP), fall back to previous season.
    Always uses current-season positions regardless of IP.
    Memoized per (current, fallback) pair."""
    key = (current, fallback)
    if key in _merged_pitching_cache:
        return _merged_pitching_cache[key]
    cur  = get_pitching_stats_by_name(current)
    prev = get_pitching_stats_by_name(fallback)
    result = {k: dict(v) for k, v in prev.items()}
    for name, s in cur.items():
        if (s.get("IP") or 0) >= 2:
            result[name] = dict(s)
    _apply_positions(result, get_player_positions(current))
    _merged_pitching_cache[key] = result
    return result


_hot_cache = {}


def get_hot_players(days=7, season=2026, limit=8):
    """Fetch hottest hitters and pitchers from last X days.
    Falls back to 2025 season top performers if pre-season / no data.
    Cached per (date, days, season, limit) so multiple page loads in the
    same day reuse the result."""
    from datetime import date, timedelta
    end = date.today()
    cache_key = (end.isoformat(), days, season, limit)
    if cache_key in _hot_cache:
        return _hot_cache[cache_key]
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

    result = {
        "hitters": hitters[:limit],
        "pitchers": pitchers[:limit],
        "source": source,
    }
    _hot_cache[cache_key] = result
    return result


def _f(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
