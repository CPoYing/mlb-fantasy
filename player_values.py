"""
Compute 5x5 H2H z-score values for every MLB player.

Primary universe: MLB Stats API (2025 stats + 2026-only rookies).
Norms (mean / std) come from 2025 qualified pool.

Used by: dashboard, rankings, waiver analyzer.
"""
import statistics
import mlb_stats

BATTING_CATS  = ["R", "HR", "RBI", "SB", "AVG"]
PITCHING_CATS = ["W", "SV", "K", "ERA", "WHIP"]
LOWER_BETTER  = {"ERA", "WHIP"}

MIN_GAMES = 30
MIN_IP    = 15

_values_cache = None


def _stdev(vals):
    return statistics.stdev(vals) if len(vals) > 1 else 1.0


def compute_player_values():
    """Return {norm_name: {total, cats, stats, is_batter}} for all qualified players."""
    global _values_cache
    if _values_cache:
        return _values_cache

    hitting_2025  = mlb_stats.get_hitting_stats_by_name(2025)
    pitching_2025 = mlb_stats.get_pitching_stats_by_name(2025)
    hitting_2026  = mlb_stats.get_hitting_stats_by_name(2026)
    pitching_2026 = mlb_stats.get_pitching_stats_by_name(2026)
    values = {}

    qual_b = {n: s for n, s in hitting_2025.items() if (s.get("G") or 0) >= MIN_GAMES}
    b_mean, b_std = {}, {}
    for cat in BATTING_CATS:
        vals = [s[cat] for s in qual_b.values() if s.get(cat) is not None]
        if vals:
            b_mean[cat] = statistics.mean(vals)
            b_std[cat]  = _stdev(vals)

    def score_batter(name, stats):
        total, cats = 0.0, {}
        for cat in BATTING_CATS:
            if cat not in b_mean or stats.get(cat) is None:
                continue
            z = (stats[cat] - b_mean[cat]) / b_std[cat]
            cats[cat] = round(z, 2)
            total += z
        values[name] = {"total": round(total, 2), "cats": cats, "stats": stats, "is_batter": True}

    for name, stats in qual_b.items():
        score_batter(name, stats)

    for name, stats in hitting_2026.items():
        if name in values or (stats.get("G") or 0) < 5:
            continue
        score_batter(name, stats)

    qual_p = {n: s for n, s in pitching_2025.items() if (s.get("IP") or 0) >= MIN_IP}
    p_mean, p_std = {}, {}
    for cat in PITCHING_CATS:
        vals = [s[cat] for s in qual_p.values() if s.get(cat) is not None]
        if vals:
            p_mean[cat] = statistics.mean(vals)
            p_std[cat]  = _stdev(vals)

    def score_pitcher(name, stats):
        total, cats = 0.0, {}
        for cat in PITCHING_CATS:
            if cat not in p_mean or stats.get(cat) is None:
                continue
            z = (stats[cat] - p_mean[cat]) / p_std[cat]
            if cat in LOWER_BETTER:
                z = -z
            cats[cat] = round(z, 2)
            total += z
        entry = {"total": round(total, 2), "cats": cats, "stats": stats, "is_batter": False}
        existing = values.get(name)
        if existing is None or entry["total"] > existing["total"]:
            values[name] = entry

    for name, stats in qual_p.items():
        score_pitcher(name, stats)

    for name, stats in pitching_2026.items():
        if name in values or (stats.get("IP") or 0) < 2:
            continue
        score_pitcher(name, stats)

    positions_2026 = mlb_stats.get_player_positions(2026)
    for name, entry in values.items():
        if name in positions_2026:
            new_pos = positions_2026[name]
            s = dict(entry["stats"])
            s["mlb_pos"] = new_pos
            is_starter = not entry["is_batter"] and s.get("GS", 0) > 0
            s["fantasy_eligible"] = mlb_stats.mlb_pos_to_fantasy(
                new_pos, is_starter if new_pos == "P" else None)
            entry["stats"] = s

    _values_cache = values
    return values


def default_eligible(is_batter, position=""):
    """Fallback when player not found in Yahoo pool."""
    if not is_batter:
        if position == "SP":
            return ["SP", "P"]
        if position in ("RP", "CL"):
            return ["RP", "P"]
        return ["P"]
    pos_map = {
        "C": ["C", "Util"], "1B": ["1B", "Util"], "2B": ["2B", "Util"],
        "3B": ["3B", "Util"], "SS": ["SS", "Util"], "OF": ["OF", "Util"],
        "DH": ["Util"], "2B,SS": ["2B", "SS", "Util"],
        "1B,3B": ["1B", "3B", "Util"],
    }
    return pos_map.get(position, ["Util"])
