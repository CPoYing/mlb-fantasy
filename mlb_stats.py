"""
Fetch season stats from MLB Stats API (free, no auth required).
Index by player full name. Paginated to get all players.

Current season focus (2025 data completely removed).
"""
import requests

MLB_API = "https://statsapi.mlb.com/api/v1"
_cache  = {}

MILB_SPORTS = {
    "AAA": 11,
    "AA":  12,
    "A+":  13,
    "A":   14,
}


def _fetch_all(group, season=2026, sport_id=1):
    """Fetch all season-stat splits for a (group, season, sport_id).

    sport_id 1 = MLB; 11/12/13/14 = AAA/AA/A+/A (see MILB_SPORTS).
    Paginated to grab every player. Cached per key for the process life.
    """
    key = f"{group}_{sport_id}_{season}"
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
            "sportId":    sport_id,
            "limit":      page_size,
            "offset":     offset,
        }, timeout=60)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        all_splits.extend(splits)
        if len(splits) < page_size:
            break
        offset += page_size

    _cache[key] = all_splits
    return all_splits


import re as _re
import unicodedata as _ud

_SUFFIX_RE = _re.compile(r"\s+(jr\.?|sr\.?|ii|iii|iv)\s*$", _re.IGNORECASE)
_PUNCT_RE  = _re.compile(r"[.,'`]")
_SPACE_RE  = _re.compile(r"\s+")


def _normalize(name):
    """Map a player display name to a comparable key.

    Strips accents, lowercases, drops common suffixes (Jr./Sr./II/III/IV)
    and punctuation (periods, commas, apostrophes), normalizes hyphens
    to spaces, and collapses runs of whitespace. Keeps Yahoo names like
    "José Ramírez Jr." matchable against MLB Stats' "Jose Ramirez".
    """
    n = _ud.normalize("NFD", name)
    n = "".join(c for c in n if _ud.category(c) != "Mn")
    n = n.replace("-", " ").lower()
    n = _PUNCT_RE.sub("", n)
    n = _SUFFIX_RE.sub("", n)
    n = _SPACE_RE.sub(" ", n).strip()
    return n


# MLB position abbreviation → fantasy-eligible positions
_POS_MAP = {
    "C":   ["C",  "Util"],
    "1B":  ["1B", "Util"],
    "2B":  ["2B", "Util"],
    "3B":  ["3B", "Util"],
    "SS":  ["SS", "Util"],
    "LF":  ["OF", "Util"],
    "CF":  ["OF", "Util"],
    "RF":  ["OF", "Util"],
    "OF":  ["OF", "Util"],
    "DH":  ["Util"],
    # Two-way player (Ohtani-style). For the batter-side entry MLB Stats
    # tags him "TWP"; the value dict only knows about his hitting role
    # there, so Util is the right cell. His pitching record gets a
    # separate entry classified as SP/RP via _pitcher_row.
    "TWP": ["Util"],
    "SP":  ["SP", "P"],
    "RP":  ["RP", "P"],
    "P":   ["P"],
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


PITCHER_ABBRS = {"P", "SP", "RP"}


# ── Player profile + per-game logs (for /player detail page) ──

def get_player_info(player_id):
    """Biographical info: name, birthdate, age, height, weight, throws,
    bats, primary position, current team. Cached per player_id."""
    key = f"info_{player_id}"
    if key in _cache:
        return _cache[key]
    try:
        r = requests.get(f"{MLB_API}/people/{player_id}", timeout=15)
        r.raise_for_status()
        ppl = r.json().get("people", [{}])
        info = ppl[0] if ppl else {}
    except Exception as e:
        print(f"player info fetch failed: {e}")
        info = {}
    result = {
        "id":          info.get("id"),
        "name":        info.get("fullName", ""),
        "birthdate":   info.get("birthDate", ""),
        "age":         info.get("currentAge"),
        "height":      info.get("height", ""),
        "weight":      info.get("weight"),
        "bats":        (info.get("batSide", {}) or {}).get("description", ""),
        "throws":      (info.get("pitchHand", {}) or {}).get("description", ""),
        "primary_pos": (info.get("primaryPosition", {}) or {}).get("abbreviation", ""),
        "team":        (info.get("currentTeam", {}) or {}).get("name", ""),
        "mlb_debut":   info.get("mlbDebutDate", ""),
        "headshot":    f"https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/{info.get('id', 0)}/headshot/67/current"
                       if info.get("id") else "",
    }
    _cache[key] = result
    return result


def get_player_game_log(player_id, season=2026, group="hitting", limit=10):
    """Per-game stats for a player, most recent first. Cached per
    (player_id, season, group, date)."""
    from datetime import date
    key = f"gamelog_{player_id}_{group}_{season}_{date.today().isoformat()}"
    if key in _cache:
        return _cache[key]
    try:
        r = requests.get(
            f"{MLB_API}/people/{player_id}/stats",
            params={"stats": "gameLog", "group": group, "season": season},
            timeout=20,
        )
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
    except Exception as e:
        print(f"game log fetch failed: {e}")
        splits = []

    # Most recent first
    splits.sort(key=lambda s: s.get("date", ""), reverse=True)
    games = []
    for s in splits[:limit]:
        st = s.get("stat", {})
        row = {
            "date":     s.get("date", ""),
            "opponent": (s.get("opponent", {}) or {}).get("abbreviation", ""),
            "home":     s.get("isHome", False),
            "team":     (s.get("team", {}) or {}).get("abbreviation", ""),
        }
        if group == "hitting":
            row.update({
                "AB":   _f(st.get("atBats")),
                "H":    _f(st.get("hits")),
                "HR":   _f(st.get("homeRuns")),
                "RBI":  _f(st.get("rbi")),
                "R":    _f(st.get("runs")),
                "SB":   _f(st.get("stolenBases")),
                "BB":   _f(st.get("baseOnBalls")),
                "K":    _f(st.get("strikeOuts")),
                "AVG":  _f(st.get("avg")),
                "OBP":  _f(st.get("obp")),
                "OPS":  _f(st.get("ops")),
            })
        else:
            row.update({
                "IP":   _f(st.get("inningsPitched")),
                "H":    _f(st.get("hits")),
                "R":    _f(st.get("runs")),
                "ER":   _f(st.get("earnedRuns")),
                "BB":   _f(st.get("baseOnBalls")),
                "K":    _f(st.get("strikeOuts")),
                "HR":   _f(st.get("homeRuns")),
                "W":    _f(st.get("wins")),
                "L":    _f(st.get("losses")),
                "SV":   _f(st.get("saves")),
                "HLD":  _f(st.get("holds")),
                "ERA":  _f(st.get("era")),
                "GS":   _f(st.get("gamesStarted")),
            })
        games.append(row)
    _cache[key] = games
    return games


def _hitter_row(split, min_g, min_pa):
    """Extract one hitter's stat dict from an MLB Stats API split,
    or return (None, None) if filtered out.
    Used for both MLB and MiLB extraction."""
    name = split.get("player", {}).get("fullName", "")
    s    = split.get("stat", {})
    mlb_abbr = split.get("position", {}).get("abbreviation", "")
    if mlb_abbr in PITCHER_ABBRS:
        return None, None
    games  = s.get("gamesPlayed", 0) or 0
    pa_raw = s.get("plateAppearances", 0) or 0
    if games < min_g or pa_raw < min_pa:
        return None, None

    avg = _f(s.get("avg"))
    slg = _f(s.get("slg"))
    obp = _f(s.get("obp"))
    ops = _f(s.get("ops"))
    bb  = _f(s.get("baseOnBalls"))
    k   = _f(s.get("strikeOuts"))
    pa  = _f(s.get("plateAppearances"))
    babip = _f(s.get("babip"))

    iso    = (slg - avg) if (slg is not None and avg is not None) else None
    bb_pct = _div(bb, pa)
    k_pct  = _div(k,  pa)

    return _normalize(name), {
        # League categories (7x7) — used at MLB level
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
        # Display
        "name":      name,
        "player_id": split.get("player", {}).get("id"),
        "team":      split.get("team", {}).get("name", ""),
        "mlb_pos":   mlb_abbr,
        "fantasy_eligible": mlb_pos_to_fantasy(mlb_abbr),
    }


def get_hitting_stats_by_name(season=2026):
    """Return {norm_name: stat_dict} for hitters with meaningful playing time.
    Defensively filters out pitchers (who can appear with G > 0 but PA = 0
    in the hitting endpoint) — without this they leak into the batter pool.
    """
    result = {}
    for split in _fetch_all("hitting", season):
        k, v = _hitter_row(split, min_g=5, min_pa=20)
        if k:
            result[k] = v
    return result


def get_milb_hitting(level, season=2026, min_pa=50, min_g=15):
    """Return {norm_name: stat_dict} for hitters at a single MiLB level."""
    sport_id = MILB_SPORTS[level]
    result = {}
    for split in _fetch_all("hitting", season, sport_id=sport_id):
        k, v = _hitter_row(split, min_g=min_g, min_pa=min_pa)
        if k:
            v["level"] = level
            result[k] = v
    return result


def _pitcher_row(split, min_ip):
    """Extract one pitcher's stat dict from an MLB Stats API split,
    or return (None, None) if filtered out."""
    name = split.get("player", {}).get("fullName", "")
    s    = split.get("stat", {})
    ip   = _f(s.get("inningsPitched"))
    if ip is None or ip < min_ip:
        return None, None
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

    return _normalize(name), {
        "W":        _f(s.get("wins")),
        "BB":       bb,
        "HLD":      _f(s.get("holds")),
        "SV":       _f(s.get("saves")),
        "K":        k,
        "ERA":      era,
        "WHIP":     whip,
        "K9":       round(k9,   2) if k9   is not None else None,
        "BB9":      round(bb9,  2) if bb9  is not None else None,
        "K_BB_pct": round(k_bb, 3) if k_bb is not None else None,
        "FIP":      round(fip,  2) if fip  is not None else None,
        "BABIP":    babip,
        "IP":       ip,
        "BF":       bf,
        "HR":       hr,
        "GS":       float(started),
        "G":        float(games),
        "name":      name,
        "player_id": split.get("player", {}).get("id"),
        "team":      split.get("team", {}).get("name", ""),
        "mlb_pos":   "SP" if is_sp else "RP",
        "fantasy_eligible": eligible,
    }


def get_pitching_stats_by_name(season=2026):
    """Return {norm_name: stat_dict} for pitchers with ≥2 IP in the season."""
    result = {}
    for split in _fetch_all("pitching", season):
        k, v = _pitcher_row(split, min_ip=2)
        if k:
            result[k] = v
    return result


def get_milb_pitching(level, season=2026, min_ip=15):
    """Return {norm_name: stat_dict} for pitchers at a single MiLB level."""
    sport_id = MILB_SPORTS[level]
    result = {}
    for split in _fetch_all("pitching", season, sport_id=sport_id):
        k, v = _pitcher_row(split, min_ip=min_ip)
        if k:
            v["level"] = level
            result[k] = v
    return result


# ── Last-N-days windows (for recent-form weighting) ──

def _fetch_date_range(group, days, season=2026, sport_id=1):
    """Fetch stats over the last N days for a (group, sport_id).
    Cached per (group, sport_id, days, end_date) so multiple page loads
    on the same day reuse the result; the next calendar day refetches.
    """
    from datetime import date, timedelta
    end       = date.today()
    start     = end - timedelta(days=days)
    key       = f"{group}_{sport_id}_lastN_{days}_{end.isoformat()}"
    if key in _cache:
        return _cache[key]

    all_splits = []
    offset     = 0
    page_size  = 1000
    while True:
        r = requests.get(f"{MLB_API}/stats", params={
            "stats":      "byDateRange",
            "startDate":  start.strftime("%m/%d/%Y"),
            "endDate":    end.strftime("%m/%d/%Y"),
            "group":      group,
            "playerPool": "all",
            "sportId":    sport_id,
            "season":     season,
            "limit":      page_size,
            "offset":     offset,
        }, timeout=60)
        r.raise_for_status()
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        all_splits.extend(splits)
        if len(splits) < page_size:
            break
        offset += page_size

    _cache[key] = all_splits
    return all_splits


def get_hitting_last_n_days(days=14, season=2026, min_pa=5):
    """Return {norm_name: stat_dict} for hitters with PA in the last N days."""
    result = {}
    for split in _fetch_date_range("hitting", days, season):
        k, v = _hitter_row(split, min_g=1, min_pa=min_pa)
        if k:
            result[k] = v
    return result


def get_pitching_last_n_days(days=14, season=2026, min_ip=1):
    """Return {norm_name: stat_dict} for pitchers with IP in the last N days."""
    result = {}
    for split in _fetch_date_range("pitching", days, season):
        k, v = _pitcher_row(split, min_ip=min_ip)
        if k:
            result[k] = v
    return result


# ── Statcast expected statistics (xAVG / xSLG / xwOBA) ──

def get_expected_stats(season=2026, group="hitting"):
    """Statcast-derived expected stats from MLB Stats API.

    For hitting: xAVG, xSLG, xwOBA, xwOBA_CON — what a hitter SHOULD be
    batting based on quality of contact, independent of luck. Hitters
    with xAVG > AVG have been unlucky; xAVG < AVG have been lucky.

    For pitching: same fields but interpreted as expected stats AGAINST
    the pitcher (lower = better, since these are batter outcomes).
    """
    key = f"xstats_{group}_{season}"
    if key in _cache:
        return _cache[key]

    all_splits = []
    offset     = 0
    page_size  = 1000
    while True:
        try:
            r = requests.get(f"{MLB_API}/stats", params={
                "stats":   "expectedStatistics",
                "season":  season,
                "group":   group,
                "sportId": 1,
                "limit":   page_size,
                "offset":  offset,
            }, timeout=60)
            r.raise_for_status()
        except Exception as e:
            print(f"x-stats fetch failed ({group} {season}): {e}")
            break
        splits = r.json().get("stats", [{}])[0].get("splits", [])
        all_splits.extend(splits)
        if len(splits) < page_size:
            break
        offset += page_size

    result = {}
    for split in all_splits:
        name = split.get("player", {}).get("fullName", "")
        s    = split.get("stat", {})
        result[_normalize(name)] = {
            "xAVG":    _f(s.get("avg")),
            "xSLG":    _f(s.get("slg")),
            "xwOBA":   _f(s.get("woba")),
            "xwOBA_CON": _f(s.get("wobaCon")),
        }
    _cache[key] = result
    return result


# ── Recent MLB call-ups (for prospect 🆙 marker) ──

_CALLUP_TYPE_CODES = {"CU", "SE"}  # CU = Recalled from minors, SE = Selected (contract purchase)


def get_recent_callups(days=30, season=2026):
    """Return set of normalized names of players called up in the last N days.

    Filters MLB transactions for typeCode in {CU, SE} which is what MLB
    uses for promotions from MiLB to MLB. Cached per (days, end_date)."""
    from datetime import date, timedelta
    end       = date.today()
    start     = end - timedelta(days=days)
    cache_key = f"callups_{days}_{end.isoformat()}"
    if cache_key in _cache:
        return _cache[cache_key]

    try:
        r = requests.get(f"{MLB_API}/transactions", params={
            "startDate": start.strftime("%m/%d/%Y"),
            "endDate":   end.strftime("%m/%d/%Y"),
            "sportId":   1,
        }, timeout=30)
        r.raise_for_status()
        transactions = r.json().get("transactions", [])
    except Exception as e:
        print(f"Callup transactions fetch failed: {e}")
        transactions = []

    callups = set()
    for tx in transactions:
        if tx.get("typeCode") in _CALLUP_TYPE_CODES:
            name = (tx.get("person") or {}).get("fullName", "")
            if name:
                callups.add(_normalize(name))

    _cache[cache_key] = callups
    return callups


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


def get_hitting_stats_merged(current=2026):
    """Return hitting stats for the current season (no historical fallback).
    Kept for backward compatibility with callers; now purely current season."""
    key = (current,)
    if key in _merged_hitting_cache:
        return _merged_hitting_cache[key]
    result = {k: dict(v) for k, v in get_hitting_stats_by_name(current).items()}
    _apply_positions(result, get_player_positions(current))
    _merged_hitting_cache[key] = result
    return result


def get_pitching_stats_merged(current=2026):
    """Return pitching stats for the current season (no historical fallback).
    Kept for backward compatibility with callers; now purely current season."""
    key = (current,)
    if key in _merged_pitching_cache:
        return _merged_pitching_cache[key]
    result = {k: dict(v) for k, v in get_pitching_stats_by_name(current).items()}
    _apply_positions(result, get_player_positions(current))
    _merged_pitching_cache[key] = result
    return result


_hot_cache = {}


def get_hot_players(days=7, season=2026, limit=8):
    """Fetch hottest hitters and pitchers from last X days.
    Falls back to full current season if no recent activity data.
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
        # No recent data: fall back to full current season leaders as source
        hit_splits = _fetch_all("hitting", season)
        pit_splits = _fetch_all("pitching", season)
        source = f"{season} 整季"
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
