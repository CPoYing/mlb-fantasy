"""
Compute 7×7 H2H z-score values for every MLB player.

League categories (per 菜政宜的秘密花園):
  Batting:  HR, RBI, SB, AVG, OBP, OPS, E         (E lower-better)
  Pitching: W, BB, HLD, SV, K, ERA, WHIP

Pitcher scoring is split by role — SP and RP have different cat sets and
are scored against their own role's norm pool, since comparing a starter
to a closer on W/HLD/SV would distort both ends:

  SP_CATS = W, BB, K, ERA, WHIP               (HLD/SV ignored)
  RP_CATS = BB, HLD, SV, K, ERA, WHIP         (W ignored)

  Lower-better in either role: BB, ERA, WHIP

Role classification:
  - max(IP_2025, IP_2026) >= 50  → SP
  - otherwise                    → RP

Junk pitcher filter (user rule):
  - IP_2026 < 10 AND (no 2025 data OR 2025 ERA > 4) → excluded entirely
    (rationale: not enough current sample + no good prior signal)

Scoring approach (2026 dominates, 2025 supports):
  - Norms computed per (role, season) from each role's qualified pool.
  - Per cat: z = 0.80 * z_2026 + 0.20 * z_2025 when both available.
  - One-season-only: use that side directly (no blend penalty).
  - Two-way players (Ohtani-style) get BOTH a batter and a pitcher entry.
"""
import statistics
import mlb_stats

# ── Categories ────────────────────────────────────────────────

BATTING_CATS  = ["HR", "RBI", "SB", "AVG", "OBP", "OPS", "E"]
SP_CATS       = ["W", "BB", "K", "ERA", "WHIP"]
RP_CATS       = ["BB", "HLD", "SV", "K", "ERA", "WHIP"]
PITCHING_CATS = ["W", "BB", "HLD", "SV", "K", "ERA", "WHIP"]  # full display order
# Lower-better extended to cover the advanced/prospect metrics where
# "less" is unambiguously good (walks-per-9, FIP).
LOWER_BETTER  = {"E", "BB", "ERA", "WHIP", "BB9", "FIP"}

# ── Prospect ranking (MiLB) — focused on scouting-flavor cats ──
PROSPECT_BAT_CATS = ["AVG", "OBP", "OPS", "HR", "SB"]
PROSPECT_PIT_CATS = ["K9", "BB9", "ERA", "WHIP", "FIP"]
PROSPECT_LEVEL_WEIGHTS = {"AAA": 1.0, "AA": 0.75, "A+": 0.5, "A": 0.3}

# ── Blend weights ─────────────────────────────────────────────

W_CURRENT = 0.80
W_PREV    = 0.20

# ── Qualification thresholds (for norm pools — "who counts as average") ──

MIN_G_BAT_PREV  = 30
MIN_G_BAT_CUR   = 25
MIN_IP_SP_PREV  = 100
MIN_IP_SP_CUR   = 30
MIN_IP_RP_PREV  = 25
MIN_IP_RP_CUR   = 8

# ── Ranked-pool thresholds (who gets an entry at all) ──

MIN_G_BAT_RANKED  = 5     # 2026 min sample
MIN_IP_PITCH_LOW  = 10    # below this, junk-filter kicks in

# ── Role classification ──

SP_IP_THRESHOLD = 35      # dual-eligible (has any starts) with max-season IP > 35 → SP

# ── RP tier thresholds (Pitcher List style, prorated to full season) ──
# Mid-season 2026 stats get scaled by SEASON_PRORATE_FACTOR before comparing
# against these full-season totals.
SEASON_PRORATE_FACTOR = 3.0   # ~162 / ~55 days elapsed
RP_CLOSER_SV   = 10
RP_SETUP_HLD   = 12


_values_cache = None  # (batter_values, pitcher_values)


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


def _blend(z_now, z_prev):
    if z_now is not None and z_prev is not None:
        return W_CURRENT * z_now + W_PREV * z_prev
    return z_now if z_now is not None else z_prev


def _classify_sp(s_2026, s_2025):
    """Decide if a pitcher is SP or RP.

    Pure RP (never started in either season): always RP.
    Started in either season + max-season IP > 35: SP (per league rule —
        dual-eligible pitchers with > 35 IP count as SP).
    Otherwise: RP (spot starter / emergency starter who didn't accumulate
        enough volume to be treated as a starter).
    """
    gs_now  = (s_2026.get("GS") if s_2026 else 0) or 0
    gs_prev = (s_2025.get("GS") if s_2025 else 0) or 0
    if gs_now + gs_prev == 0:
        return False  # never started → pure RP
    ip_now  = (s_2026.get("IP") if s_2026 else 0) or 0
    ip_prev = (s_2025.get("IP") if s_2025 else 0) or 0
    return max(ip_now, ip_prev) > SP_IP_THRESHOLD


def _rp_tier(s_2026, s_2025):
    """Infer RP bullpen role tier (closer / setup / middle).

    Uses 2025 totals directly + 2026 prorated to full season, takes max
    of the two so an emerging closer who's racked up saves recently isn't
    missed.
    """
    sv_prev  = (s_2025.get("SV")  if s_2025 else 0) or 0
    hld_prev = (s_2025.get("HLD") if s_2025 else 0) or 0
    sv_now   = ((s_2026.get("SV")  or 0) * SEASON_PRORATE_FACTOR) if s_2026 else 0
    hld_now  = ((s_2026.get("HLD") or 0) * SEASON_PRORATE_FACTOR) if s_2026 else 0
    proj_sv  = max(sv_prev, sv_now)
    proj_hld = max(hld_prev, hld_now)
    if proj_sv >= RP_CLOSER_SV:
        return "closer"
    if proj_hld >= RP_SETUP_HLD:
        return "setup"
    return "middle"


def _exclude_junk_pitcher(s_2026, s_2025):
    """User rule: IP_2026 < 10 AND (no 2025 OR 2025 ERA > 4) → exclude."""
    ip_now = (s_2026.get("IP") if s_2026 else 0) or 0
    if ip_now >= MIN_IP_PITCH_LOW:
        return False
    if not s_2025:
        return True
    era_25 = s_2025.get("ERA")
    return era_25 is None or era_25 > 4.0


# ── Batting ───────────────────────────────────────────────────

def _score_batters(h_2026, h_2025):
    qual_cur  = {n: s for n, s in h_2026.items() if (s.get("G") or 0) >= MIN_G_BAT_CUR}
    qual_prev = {n: s for n, s in h_2025.items() if (s.get("G") or 0) >= MIN_G_BAT_PREV}
    mean_cur, std_cur   = _build_norms(qual_cur,  BATTING_CATS)
    mean_prev, std_prev = _build_norms(qual_prev, BATTING_CATS)

    out = {}
    for name in set(h_2026) | set(h_2025):
        s_now  = h_2026.get(name)
        s_prev = h_2025.get(name)
        # Ranked-pool gate: at least minimum sample somewhere
        if not (
            (s_now  and (s_now.get("G")  or 0) >= MIN_G_BAT_RANKED) or
            (s_prev and (s_prev.get("G") or 0) >= MIN_G_BAT_PREV)
        ):
            continue
        cat_z = {}
        for cat in BATTING_CATS:
            z_now  = _cat_z(s_now.get(cat)  if s_now  else None, mean_cur,  std_cur,  cat)
            z_prev = _cat_z(s_prev.get(cat) if s_prev else None, mean_prev, std_prev, cat)
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
            "is_batter": True,
            "role":      "BAT",
            "source":    source,
        }
    return out


# ── Pitching (split by role) ──────────────────────────────────

def _score_pitcher_role(group_cur, group_prev, cats, role, min_ip_cur, min_ip_prev):
    qual_cur  = {n: s for n, s in group_cur.items()  if (s.get("IP") or 0) >= min_ip_cur}
    qual_prev = {n: s for n, s in group_prev.items() if (s.get("IP") or 0) >= min_ip_prev}
    mean_cur, std_cur   = _build_norms(qual_cur,  cats)
    mean_prev, std_prev = _build_norms(qual_prev, cats)

    out = {}
    for name in set(group_cur) | set(group_prev):
        s_now  = group_cur.get(name)
        s_prev = group_prev.get(name)
        cat_z = {}
        for cat in cats:
            z_now  = _cat_z(s_now.get(cat)  if s_now  else None, mean_cur,  std_cur,  cat)
            z_prev = _cat_z(s_prev.get(cat) if s_prev else None, mean_prev, std_prev, cat)
            blended = _blend(z_now, z_prev)
            if blended is not None:
                cat_z[cat] = round(blended, 2)
        if not cat_z:
            continue
        display_stats = dict(s_now) if s_now else dict(s_prev)
        source = "2026" if (s_now and not s_prev) else ("2025" if (s_prev and not s_now) else "blend")
        entry = {
            "total":     round(sum(cat_z.values()), 2),
            "cats":      cat_z,
            "stats":     display_stats,
            "is_batter": False,
            "role":      role,
            "source":    source,
        }
        if role == "RP":
            entry["tier"] = _rp_tier(s_now, s_prev)
        out[name] = entry
    return out


def _score_pitchers(p_2026, p_2025):
    sp_cur, rp_cur   = {}, {}
    sp_prev, rp_prev = {}, {}

    for name in set(p_2026) | set(p_2025):
        s_now  = p_2026.get(name)
        s_prev = p_2025.get(name)
        if _exclude_junk_pitcher(s_now, s_prev):
            continue
        if _classify_sp(s_now, s_prev):
            if s_now:  sp_cur[name]  = s_now
            if s_prev: sp_prev[name] = s_prev
        else:
            if s_now:  rp_cur[name]  = s_now
            if s_prev: rp_prev[name] = s_prev

    sp_values = _score_pitcher_role(
        sp_cur, sp_prev, SP_CATS, "SP", MIN_IP_SP_CUR, MIN_IP_SP_PREV,
    )
    rp_values = _score_pitcher_role(
        rp_cur, rp_prev, RP_CATS, "RP", MIN_IP_RP_CUR, MIN_IP_RP_PREV,
    )
    # No name collision: a pitcher is either SP-classified or RP-classified, not both.
    return {**sp_values, **rp_values}


# ── Public ────────────────────────────────────────────────────

def compute_player_values():
    """Return (batter_values, pitcher_values).
    Two-way players appear in both."""
    global _values_cache
    if _values_cache is not None:
        return _values_cache

    h_2025 = mlb_stats.get_hitting_stats_by_name(2025)
    p_2025 = mlb_stats.get_pitching_stats_by_name(2025)
    h_2026 = mlb_stats.get_hitting_stats_by_name(2026)
    p_2026 = mlb_stats.get_pitching_stats_by_name(2026)

    batter_values  = _score_batters(h_2026, h_2025)
    pitcher_values = _score_pitchers(p_2026, p_2025)

    # Overlay 2026 positions onto each entry (covers in-season position changes)
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


# ── Prospect rankings (MiLB) ──────────────────────────────────

_prospect_cache = None  # (hitters, pitchers)


def compute_prospect_rankings(top_n=100):
    """Rank minor leaguers across AAA / AA / A+ / A using a level-weighted
    composite z-score (z computed within each level's qualified pool, then
    multiplied by that level's weight so AAA performance counts more than
    A-ball performance).

    Returns ({"hitters": [...], "pitchers": [...]}, each list up to top_n
    entries sorted by composite descending).
    """
    global _prospect_cache
    if _prospect_cache is not None:
        return _prospect_cache

    all_hit = []
    all_pit = []

    for level, weight in PROSPECT_LEVEL_WEIGHTS.items():
        try:
            hit = mlb_stats.get_milb_hitting(level)
            pit = mlb_stats.get_milb_pitching(level)
        except Exception as e:
            print(f"MiLB fetch failed for {level}: {e}")
            continue

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
