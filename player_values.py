"""
Compute 7×7 H2H z-score values for every MLB player.

League categories (per 菜政宜的秘密花園):
  Batting:  HR, RBI, SB, AVG, OBP, OPS, E         (E lower-better)
  Pitching: W, BB, HLD, SV, K, ERA, WHIP          (BB / ERA / WHIP lower-better)

Scoring approach (2026 dominates, 2025 is supporting):
  - Norms computed separately from each season's qualified pool.
  - For each player + category, the z-score is a blend:
        z = W_CURRENT * z_2026 + W_PREV * z_2025
    when both samples exist. Otherwise we use whichever season has data,
    no blend penalty (rookies use 2026 only; long-injured veterans use
    2025 only).
  - Two-way players (Ohtani-style) get BOTH a batter and a pitcher entry —
    callers look up by Yahoo's display_position so the right side is used.
"""
import statistics
import mlb_stats

BATTING_CATS  = ["HR", "RBI", "SB", "AVG", "OBP", "OPS", "E"]
PITCHING_CATS = ["W", "BB", "HLD", "SV", "K", "ERA", "WHIP"]
LOWER_BETTER  = {"E", "BB", "ERA", "WHIP"}

# 2026 dominates the value, 2025 just informs.
W_CURRENT = 0.80
W_PREV    = 0.20

# Qualification thresholds. 2026 is mid-season so the bar is lower.
MIN_GAMES_PREV    = 30
MIN_IP_PREV       = 15
MIN_GAMES_CURRENT = 25
MIN_IP_CURRENT    = 10

# Include any player with at least this much sample in either season,
# so rookies and bench guys still appear (they just get low-confidence z).
MIN_GAMES_RANKED  = 5
MIN_IP_RANKED     = 2

_values_cache = None  # (batter_values, pitcher_values)


def _stdev(vals):
    return statistics.stdev(vals) if len(vals) > 1 else 1.0


def _build_norms(qual_dict, cats):
    """Compute mean/std for each category over qualified players."""
    mean, std = {}, {}
    for cat in cats:
        vals = [s[cat] for s in qual_dict.values() if s.get(cat) is not None]
        if vals:
            mean[cat] = statistics.mean(vals)
            std[cat]  = _stdev(vals)
    return mean, std


def _cat_z(stat_val, mean_map, std_map, cat):
    """Single-category z-score, with lower-better flip applied."""
    if stat_val is None:
        return None
    m = mean_map.get(cat)
    s = std_map.get(cat)
    if m is None or s is None or s == 0:
        return None
    z = (stat_val - m) / s
    if cat in LOWER_BETTER:
        z = -z
    return z


def _blend(z_now, z_prev):
    """Combine current + prev season z-scores per the blend weights."""
    if z_now is not None and z_prev is not None:
        return W_CURRENT * z_now + W_PREV * z_prev
    return z_now if z_now is not None else z_prev


def _score_group(cur_stats, prev_stats, cats, is_batter, ranked_filter):
    """Build {name → entry} for one group (batter or pitcher).

    cur_stats / prev_stats: {name → stat_dict} for each season.
    ranked_filter(name, cur, prev) → True if player has enough sample.
    """
    cur_norm_pool  = {n: s for n, s in cur_stats.items()  if _meets(s, is_batter, current=True)}
    prev_norm_pool = {n: s for n, s in prev_stats.items() if _meets(s, is_batter, current=False)}

    cur_mean,  cur_std  = _build_norms(cur_norm_pool,  cats)
    prev_mean, prev_std = _build_norms(prev_norm_pool, cats)

    out = {}
    for name in set(cur_stats) | set(prev_stats):
        if not ranked_filter(name, cur_stats, prev_stats):
            continue
        s_now  = cur_stats.get(name)
        s_prev = prev_stats.get(name)
        cat_z = {}
        for cat in cats:
            z_now  = _cat_z(s_now.get(cat),  cur_mean,  cur_std,  cat) if s_now  else None
            z_prev = _cat_z(s_prev.get(cat), prev_mean, prev_std, cat) if s_prev else None
            blended = _blend(z_now, z_prev)
            if blended is not None:
                cat_z[cat] = round(blended, 2)
        if not cat_z:
            continue
        display_stats = dict(s_now) if s_now else dict(s_prev)
        source = "2026" if (s_now and not s_prev) else ("2025" if (s_prev and not s_now) else "blend")
        out[name] = {
            "total":     round(sum(cat_z.values()), 2),
            "cats":      cat_z,
            "stats":     display_stats,
            "is_batter": is_batter,
            "source":    source,
        }
    return out


def _meets(stat_dict, is_batter, current):
    """Whether a stat record clears the norm-pool threshold."""
    if is_batter:
        threshold = MIN_GAMES_CURRENT if current else MIN_GAMES_PREV
        return (stat_dict.get("G") or 0) >= threshold
    threshold = MIN_IP_CURRENT if current else MIN_IP_PREV
    return (stat_dict.get("IP") or 0) >= threshold


def _ranked_batter(name, cur, prev):
    if name in cur  and (cur[name].get("G") or 0)  >= MIN_GAMES_RANKED:
        return True
    if name in prev and (prev[name].get("G") or 0) >= MIN_GAMES_PREV:
        return True
    return False


def _ranked_pitcher(name, cur, prev):
    if name in cur  and (cur[name].get("IP") or 0) >= MIN_IP_RANKED:
        return True
    if name in prev and (prev[name].get("IP") or 0) >= MIN_IP_PREV:
        return True
    return False


def compute_player_values():
    """Return (batter_values, pitcher_values).
    Each dict maps norm_name → {total, cats, stats, is_batter, source}.
    Two-way players appear in both."""
    global _values_cache
    if _values_cache is not None:
        return _values_cache

    h_2025 = mlb_stats.get_hitting_stats_by_name(2025)
    p_2025 = mlb_stats.get_pitching_stats_by_name(2025)
    h_2026 = mlb_stats.get_hitting_stats_by_name(2026)
    p_2026 = mlb_stats.get_pitching_stats_by_name(2026)

    batter_values  = _score_group(h_2026, h_2025, BATTING_CATS,  True,  _ranked_batter)
    pitcher_values = _score_group(p_2026, p_2025, PITCHING_CATS, False, _ranked_pitcher)

    # Overlay 2026 positions onto each entry (covers position changes)
    positions_2026 = mlb_stats.get_player_positions(2026)
    for d, default_is_batter in [(batter_values, True), (pitcher_values, False)]:
        for name, entry in d.items():
            if name in positions_2026:
                new_pos = positions_2026[name]
                s = dict(entry["stats"])
                s["mlb_pos"] = new_pos
                is_sp_hint = not default_is_batter and s.get("GS", 0) > 0
                s["fantasy_eligible"] = mlb_stats.mlb_pos_to_fantasy(
                    new_pos, is_sp_hint if new_pos == "P" else None
                )
                entry["stats"] = s

    _values_cache = (batter_values, pitcher_values)
    return _values_cache


def get_value(norm_name, is_batter):
    """Look up a player's value entry. Returns {} if not found."""
    bv, pv = compute_player_values()
    src = bv if is_batter else pv
    return src.get(norm_name, {})


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
