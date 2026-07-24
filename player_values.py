"""
Compute 7×7 H2H z-score values for every MLB player, blending recent form
with full current-season stats (2025 data has been completely removed).

League categories (per 菜政宜的秘密花園):
  Batting:  HR, RBI, SB, AVG, OBP, OPS, E         (E lower-better)
  Pitching: W, BB, HLD, SV, K, ERA, WHIP

Pitcher scoring is split by role — SP and RP have different cat sets and
are scored against their own role's norm pool:

  SP_CATS = W, BB, K, ERA, WHIP               (HLD/SV ignored)
  RP_CATS = BB, HLD, SV, K, ERA, WHIP         (W ignored)

  Lower-better in either role: BB, ERA, WHIP

Role classification (uses current season GS/IP only):
  - GS >= SP_MIN_STARTS AND IP > SP_IP_THRESHOLD  → SP
  - otherwise → RP (closer/setup detection uses current season)

Junk-pitcher exclusion:
  - IP_current < 10  → excluded entirely

Z-score blend (per cat, per player):
    z = 0.222·z_14d + 0.111·z_30d + 0.667·z_2026
  Norms are built independently within each window. If some windows are
  missing data for a player, the available weights renormalize.

  Display stats prefer: 2026 full > L30 > L14.
  `source` label is "blend" if ≥ 2 windows contributed, else the single window.
"""
import statistics
import mlb_stats

# ── Categories ────────────────────────────────────────────────

BATTING_CATS  = ["HR", "RBI", "SB", "AVG", "OBP", "OPS", "E"]

# Pitcher cat sets — role-aware so each role only competes on cats it can
# actually accumulate (SP can't get SV/HLD, CP can't get HLD/W, SU can't
# get SV/W). Middle reliever keeps both HLD/SV since they occasionally
# pick up either. Stops structural zeros from dragging z-scores down.
SP_CATS  = ["W", "BB", "K", "ERA", "WHIP"]
CP_CATS  = ["SV", "BB", "K", "ERA", "WHIP"]
SU_CATS  = ["HLD", "BB", "K", "ERA", "WHIP"]
RP_CATS  = ["BB", "HLD", "SV", "K", "ERA", "WHIP"]   # middle reliever
PITCHING_CATS = ["W", "BB", "HLD", "SV", "K", "ERA", "WHIP"]  # full display order
ROLE_CATS = {"SP": SP_CATS, "CP": CP_CATS, "SU": SU_CATS, "RP": RP_CATS}

LOWER_BETTER  = {"E", "BB", "ERA", "WHIP", "BB9", "FIP"}

# ── Blend weights (sum to 1.0) ────────────────────────────────

WEIGHTS = {
    "L14":  0.222,   # 近 14 天：原 4 路 0.20，2025 的 0.10 按 20:10:60 比例分回
    "L30":  0.111,   # 近 30 天：原 0.10
    "2026": 0.667,   # 2026 整季：原 0.60（2025 整季已完全移除）
}

# ── Norm-pool thresholds per window ──
#   key picked to gate by playing-time volume so noisy small samples
#   don't poison the league mean/std.

HITTER_NORM_THRESHOLDS = {
    "L14":  ("PA", 30),
    "L30":  ("PA", 60),
    "2026": ("G",  25),
}
SP_NORM_THRESHOLDS = {
    "L14":  ("IP", 8),
    "L30":  ("IP", 18),
    "2026": ("IP", 30),
}
RP_NORM_THRESHOLDS = {
    "L14":  ("IP", 4),
    "L30":  ("IP", 8),
    "2026": ("IP", 8),
}

# ── Role classification ──

SP_IP_THRESHOLD     = 35  # dual-eligible (≥5 starts) + max-season IP > 35 → SP
SP_MIN_STARTS      = 5    # spot starts (<5 over the two seasons) → still pure RP

SEASON_PRORATE_FACTOR = 3.0
RP_CLOSER_SV = 10
RP_SETUP_HLD = 12

# ── Prospects ──

PROSPECT_BAT_CATS = ["AVG", "OBP", "OPS", "HR", "SB"]
PROSPECT_PIT_CATS = ["K9", "BB9", "ERA", "WHIP", "FIP"]
PROSPECT_LEVEL_WEIGHTS = {"AAA": 1.0, "AA": 0.75, "A+": 0.5, "A": 0.3}

_values_cache   = None  # (batter_values, pitcher_values)
_prospect_cache = None


# ── Stats helpers ────────────────────────────────────────────

def _stdev(vals):
    return statistics.stdev(vals) if len(vals) > 1 else 1.0


def _build_norms(qual_dict, cats):
    mean, std = {}, {}
    for cat in cats:
        vals = [s[cat] for s in qual_dict.values() if s.get(cat) is not None]
        if vals:
            mean[cat] = statistics.mean(vals)
            std[cat]  = _stdev(vals)
    return mean, std


def _cat_z(stat_val, mean_map, std_map, cat):
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


def _blend_multi(per_window):
    """Weighted blend across windows; weights renormalize over the
    subset of windows that have data, so an injured veteran with no
    L14 stats isn't penalized for the missing weight."""
    total_w = 0.0
    total_v = 0.0
    for window, w in WEIGHTS.items():
        z = per_window.get(window)
        if z is not None:
            total_w += w
            total_v += w * z
    return (total_v / total_w) if total_w > 0 else None


# ── Pitcher classification + tier ──

def _classify_sp(s_2026):
    """SP if enough starts in current season AND sufficient IP volume.
    Otherwise treated as RP (spot starters / long relievers stay RP)."""
    gs = (s_2026.get("GS") if s_2026 else 0) or 0
    if gs < SP_MIN_STARTS:
        return False
    ip = (s_2026.get("IP") if s_2026 else 0) or 0
    return ip > SP_IP_THRESHOLD


def _exclude_junk_pitcher(s_2026):
    ip = (s_2026.get("IP") if s_2026 else 0) or 0
    return ip < 10


def _rp_tier(s_2026):
    sv_now   = ((s_2026.get("SV")  or 0) * SEASON_PRORATE_FACTOR) if s_2026 else 0
    hld_now  = ((s_2026.get("HLD") or 0) * SEASON_PRORATE_FACTOR) if s_2026 else 0
    if sv_now >= RP_CLOSER_SV:
        return "closer"
    if hld_now >= RP_SETUP_HLD:
        return "setup"
    return "middle"


# ── Core scoring (window-blended) ──

def _score_group(windows_data, cats, is_batter, norm_thresholds):
    """Score a player group across N windows, blending per cat per player.

    windows_data: {window_name: {norm_name: stats_dict}}
    cats: list of cat keys to score
    norm_thresholds: {window_name: (key, threshold)} — used to pick the
        "qualified" sub-pool for computing each window's mean/std.
    """
    norms = {}
    for window, data in windows_data.items():
        thresh_key, thresh = norm_thresholds.get(window, ("G", 0))
        qual = {n: s for n, s in data.items() if (s.get(thresh_key) or 0) >= thresh}
        norms[window] = _build_norms(qual, cats)

    all_names = set()
    for data in windows_data.values():
        all_names |= set(data.keys())

    out = {}
    for name in all_names:
        cat_z = {}
        for cat in cats:
            per_w = {}
            for window in WEIGHTS:
                stat_dict = windows_data.get(window, {}).get(name)
                if stat_dict is None:
                    continue
                mean_map, std_map = norms.get(window, ({}, {}))
                z = _cat_z(stat_dict.get(cat), mean_map, std_map, cat)
                if z is not None:
                    per_w[window] = z
            blended = _blend_multi(per_w)
            if blended is not None:
                cat_z[cat] = round(blended, 2)
        if not cat_z:
            continue

        # Prefer current full-season stats for display; cascade if missing.
        display_stats = (windows_data.get("2026", {}).get(name)
                         or windows_data.get("L30", {}).get(name)
                         or windows_data.get("L14", {}).get(name)
                         or {})

        present = [w for w in WEIGHTS if name in windows_data.get(w, {})]
        source  = "blend" if len(present) >= 2 else (present[0] if present else "")

        out[name] = {
            "total":     round(sum(cat_z.values()), 2),
            "cats":      cat_z,
            "stats":     dict(display_stats),
            "is_batter": is_batter,
            "source":    source,
            "windows":   present,
        }
    return out


def _score_pitchers(p_windows):
    """Split pitchers into SP / CP / SU / RP (middle) using current-season
    totals + tier inference, then score each role within its own pool
    using only that role's relevant cats."""
    p_2026 = p_windows.get("2026", {})

    buckets = {
        "SP": {w: {} for w in WEIGHTS},
        "CP": {w: {} for w in WEIGHTS},
        "SU": {w: {} for w in WEIGHTS},
        "RP": {w: {} for w in WEIGHTS},
    }

    for name, s_now in p_2026.items():
        if _exclude_junk_pitcher(s_now):
            continue
        if _classify_sp(s_now):
            role = "SP"
        else:
            tier = _rp_tier(s_now)
            role = {"closer": "CP", "setup": "SU"}.get(tier, "RP")
        target = buckets[role]
        for window in WEIGHTS:
            if name in p_windows.get(window, {}):
                target[window][name] = p_windows[window][name]

    sp_values = _score_group(buckets["SP"], SP_CATS, False, SP_NORM_THRESHOLDS)
    cp_values = _score_group(buckets["CP"], CP_CATS, False, RP_NORM_THRESHOLDS)
    su_values = _score_group(buckets["SU"], SU_CATS, False, RP_NORM_THRESHOLDS)
    rp_values = _score_group(buckets["RP"], RP_CATS, False, RP_NORM_THRESHOLDS)

    for v in sp_values.values():
        v["role"] = "SP"
    for v in cp_values.values():
        v["role"] = "CP"
        v["tier"] = "closer"
    for v in su_values.values():
        v["role"] = "SU"
        v["tier"] = "setup"
    for v in rp_values.values():
        v["role"] = "RP"
        v["tier"] = "middle"
    # No name collisions: each pitcher classified into exactly one bucket.
    return {**sp_values, **cp_values, **su_values, **rp_values}


# ── Public ────────────────────────────────────────────────────

def compute_player_values():
    """Return (batter_values, pitcher_values).
    Two-way players appear in both dicts."""
    global _values_cache
    if _values_cache is not None:
        return _values_cache

    h_windows = {
        "L14":  mlb_stats.get_hitting_last_n_days(14),
        "L30":  mlb_stats.get_hitting_last_n_days(30),
        "2026": mlb_stats.get_hitting_stats_by_name(2026),
    }
    p_windows = {
        "L14":  mlb_stats.get_pitching_last_n_days(14),
        "L30":  mlb_stats.get_pitching_last_n_days(30),
        "2026": mlb_stats.get_pitching_stats_by_name(2026),
    }

    batter_values  = _score_group(h_windows, BATTING_CATS, True, HITTER_NORM_THRESHOLDS)
    pitcher_values = _score_pitchers(p_windows)

    # Position overlay — covers in-season position changes
    positions_2026 = mlb_stats.get_player_positions(2026)
    for d, default_is_batter in [(batter_values, True), (pitcher_values, False)]:
        for name, entry in d.items():
            if name in positions_2026:
                new_pos = positions_2026[name]
                s = dict(entry["stats"])
                s["mlb_pos"] = new_pos
                is_sp_hint = (entry.get("role") == "SP") if not default_is_batter else None
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


# ── Prospect rankings (MiLB) ──────────────────────────────────

def compute_prospect_rankings(top_n=100):
    """Rank minor leaguers across AAA / AA / A+ / A using a level-weighted
    composite z-score. Players already in MLB (current season) are
    excluded, and the same player only counts at their highest level."""
    global _prospect_cache
    if _prospect_cache is not None:
        return _prospect_cache

    mlb_hit_names = set(mlb_stats.get_hitting_stats_by_name(2026).keys())
    mlb_pit_names = set(mlb_stats.get_pitching_stats_by_name(2026).keys())

    levels_hit = {}
    levels_pit = {}
    seen_hit = set(mlb_hit_names)
    seen_pit = set(mlb_pit_names)
    for level in PROSPECT_LEVEL_WEIGHTS:
        try:
            hit = mlb_stats.get_milb_hitting(level)
            pit = mlb_stats.get_milb_pitching(level)
        except Exception as e:
            print(f"MiLB fetch failed for {level}: {e}")
            continue
        levels_hit[level] = {n: s for n, s in hit.items() if n not in seen_hit}
        levels_pit[level] = {n: s for n, s in pit.items() if n not in seen_pit}
        seen_hit.update(levels_hit[level].keys())
        seen_pit.update(levels_pit[level].keys())

    all_hit = []
    all_pit = []
    for level, weight in PROSPECT_LEVEL_WEIGHTS.items():
        hit = levels_hit.get(level, {})
        pit = levels_pit.get(level, {})

        h_mean, h_std = _build_norms(hit, PROSPECT_BAT_CATS)
        p_mean, p_std = _build_norms(pit, PROSPECT_PIT_CATS)

        for name, s in hit.items():
            cats = {}
            total = 0.0
            for cat in PROSPECT_BAT_CATS:
                z = _cat_z(s.get(cat), h_mean, h_std, cat)
                if z is not None:
                    cats[cat] = round(z, 2)
                    total += z
            if not cats:
                continue
            all_hit.append({
                "name":      s.get("name", name.title()),
                "level":     level,
                "team":      s.get("team", ""),
                "pos":       s.get("mlb_pos", ""),
                "stats":     s,
                "cats":      cats,
                "z":         round(total, 2),
                "composite": round(total * weight, 2),
                "weight":    weight,
            })

        for name, s in pit.items():
            cats = {}
            total = 0.0
            for cat in PROSPECT_PIT_CATS:
                z = _cat_z(s.get(cat), p_mean, p_std, cat)
                if z is not None:
                    cats[cat] = round(z, 2)
                    total += z
            if not cats:
                continue
            all_pit.append({
                "name":      s.get("name", name.title()),
                "level":     level,
                "team":      s.get("team", ""),
                "pos":       s.get("mlb_pos", ""),
                "stats":     s,
                "cats":      cats,
                "z":         round(total, 2),
                "composite": round(total * weight, 2),
                "weight":    weight,
            })

    all_hit.sort(key=lambda x: x["composite"], reverse=True)
    all_pit.sort(key=lambda x: x["composite"], reverse=True)

    result = {
        "hitters":  all_hit[:top_n],
        "pitchers": all_pit[:top_n],
    }
    _prospect_cache = result
    return result
