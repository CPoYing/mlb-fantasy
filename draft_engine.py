"""
Draft recommendation engine.
Primary universe: MLB Stats API (2025 stats, ~1500 players).
Yahoo player pool: used only for position eligibility lookup.

Scoring categories:
  Batting:  R, HR, RBI, SB, AVG
  Pitching: W, SV, K, ERA (lower=better), WHIP (lower=better)
"""
import statistics
import mlb_stats

BATTING_CATS  = ["R", "HR", "RBI", "SB", "AVG"]
PITCHING_CATS = ["W", "SV", "K", "ERA", "WHIP"]
LOWER_BETTER  = {"ERA", "WHIP"}

MIN_GAMES = 30   # 降低門檻，避免遺漏因傷缺陣的精英球員
MIN_IP    = 15   # 閉幕投手局數少，需降低門檻

_values_cache = None

# ── Z-score computation ────────────────────────────────────────

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

    # ── Batting z-scores (norms from 2025 qualified pool) ──
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

    # Add 2026-only batters (rookies) scored against 2025 norms
    for name, stats in hitting_2026.items():
        if name in values or (stats.get("G") or 0) < 5:
            continue
        score_batter(name, stats)

    # ── Pitching z-scores (norms from 2025 qualified pool) ──
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
        pitch_entry = {"total": round(total, 2), "cats": cats, "stats": stats, "is_batter": False}
        existing = values.get(name)
        if existing is None or pitch_entry["total"] > existing["total"]:
            values[name] = pitch_entry

    for name, stats in qual_p.items():
        score_pitcher(name, stats)

    # Add 2026-only pitchers (rookies) scored against 2025 norms
    for name, stats in pitching_2026.items():
        if name in values or (stats.get("IP") or 0) < 2:
            continue
        score_pitcher(name, stats)

    _values_cache = values
    return values

# ── Position need ──────────────────────────────────────────────

SPECIFIC_SLOTS = ["C", "1B", "2B", "3B", "SS", "OF", "SP", "RP"]
FLEX_SLOTS     = ["P", "Util"]
BENCH_SLOTS    = ["BN", "IL"]

def get_position_needs(roster_positions, my_players):
    """Return list of unfilled required roster slots."""
    remaining = {pos: cnt for pos, cnt in roster_positions.items()
                 if pos not in BENCH_SLOTS}

    def specificity(p):
        return len([e for e in p.get("eligible_positions", []) if e not in BENCH_SLOTS + ["Util"]])

    for player in sorted(my_players, key=specificity):
        ep = player.get("eligible_positions", [])
        for pos in SPECIFIC_SLOTS:
            if pos in ep and remaining.get(pos, 0) > 0:
                remaining[pos] -= 1
                break
        else:
            for pos in FLEX_SLOTS:
                if pos in ep and remaining.get(pos, 0) > 0:
                    remaining[pos] -= 1
                    break

    return [pos for pos, cnt in remaining.items() for _ in range(cnt) if cnt > 0]

# ── Category balance ───────────────────────────────────────────

def analyze_category_balance(my_values_list):
    totals = {}
    for v in my_values_list:
        for cat, z in v.get("cats", {}).items():
            totals[cat] = totals.get(cat, 0.0) + z
    return {cat: round(z, 2) for cat, z in totals.items()}

# ── Scarcity ───────────────────────────────────────────────────

def compute_position_scarcity(available_players, values):
    norm = mlb_stats._normalize
    counts = {}
    for p in available_players:
        v = values.get(norm(p.get("name", "")), {})
        if v.get("total", 0) > 0:
            for pos in p.get("eligible_positions", []):
                if pos not in BENCH_SLOTS + ["Util"]:
                    counts[pos] = counts.get(pos, 0) + 1
    return counts

# ── Default position eligibility ──────────────────────────────

def _default_eligible(is_batter, position=""):
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

# ── Main recommendation function ───────────────────────────────

def get_recommendations(player_pool, draft_results, my_team_key,
                        teams_dict, roster_positions, num_teams, top_n=15):
    norm   = mlb_stats._normalize
    values = compute_player_values()

    # Pool lookups (for position eligibility and display info)
    pool_by_key  = {p["player_key"]: p for p in player_pool}
    pool_by_name = {norm(p["name"]): p for p in player_pool}

    # Drafted player names
    drafted_names = set()
    for dr in draft_results:
        pk = dr["player_key"]
        if pk in pool_by_key:
            drafted_names.add(norm(pool_by_key[pk]["name"]))

    my_names = set()
    for dr in draft_results:
        if dr["team_key"] == my_team_key:
            pk = dr["player_key"]
            if pk in pool_by_key:
                my_names.add(norm(pool_by_key[pk]["name"]))

    # My drafted players (for position need + category balance)
    my_players = []
    for name_key in my_names:
        pool_info = pool_by_name.get(name_key, {})
        val = values.get(name_key, {})
        is_batter = val.get("is_batter", True)
        position  = pool_info.get("position", "")
        eligible  = pool_info.get("eligible_positions") or _default_eligible(is_batter, position)
        my_players.append({
            "name":              pool_info.get("name", name_key),
            "position":          position,
            "team":              pool_info.get("team", ""),
            "headshot":          pool_info.get("headshot", ""),
            "eligible_positions": eligible,
            "stats":             val.get("stats", {}),
            "cats":              val.get("cats", {}),
            "is_batter":         is_batter,
            "base_value":        val.get("total", 0),
        })

    # Position needs
    needs     = get_position_needs(roster_positions, my_players)
    needs_set = set(needs)

    # Category balance
    cat_balance = analyze_category_balance([values.get(n, {}) for n in my_names])
    all_cats    = BATTING_CATS + PITCHING_CATS
    weak_cats   = sorted([(c, cat_balance.get(c, 0)) for c in all_cats], key=lambda x: x[1])[:3]
    weak_cat_names = [c for c, _ in weak_cats]

    # ── Build available universe (MLB Stats + Yahoo pool rookies) ──
    all_candidates = {}  # norm_name → candidate dict

    # 1. All players in values not yet drafted
    for name_key, val in values.items():
        if name_key in drafted_names:
            continue
        pool_info = pool_by_name.get(name_key, {})
        is_batter = val["is_batter"]
        # Position: Yahoo pool > MLB Stats stats dict > fallback
        mlb_pos   = val["stats"].get("mlb_pos", "")
        mlb_elig  = val["stats"].get("fantasy_eligible", [])
        position  = pool_info.get("position") or mlb_pos or ("SP" if not is_batter else "OF")
        eligible  = pool_info.get("eligible_positions") or mlb_elig or _default_eligible(is_batter, position)
        all_candidates[name_key] = {
            "name":              pool_info.get("name", name_key.title()),
            "position":          position,
            "eligible_positions": eligible,
            "team":              pool_info.get("team", ""),
            "headshot":          pool_info.get("headshot", ""),
            "yahoo_rank":        pool_info.get("yahoo_rank", 999),
            "is_batter":         is_batter,
        }

    # 2. Yahoo pool players with no 2025 stats (rookies, returning from injury)
    for p in player_pool:
        name_key = norm(p["name"])
        if name_key not in drafted_names and name_key not in all_candidates:
            all_candidates[name_key] = {**p}

    available = list(all_candidates.values())

    # Scarcity
    scarcity = compute_position_scarcity(available, values)

    # BPA rank = rank by z-score across ALL available
    bpa_sorted   = sorted(available, key=lambda p: values.get(norm(p["name"]), {}).get("total", -99), reverse=True)
    bpa_rank_map = {norm(p["name"]): i + 1 for i, p in enumerate(bpa_sorted)}

    # ADP round: use yahoo_rank if available, else use BPA rank
    total_picks   = len(draft_results)
    current_round = (total_picks // num_teams) + 1

    def adp_round_of(p):
        yr = p.get("yahoo_rank", 0)
        if yr and yr < 900:
            return max(1, ((yr - 1) // num_teams) + 1)
        bpa_r = bpa_rank_map.get(norm(p["name"]), 999)
        return max(1, ((bpa_r - 1) // num_teams) + 1)

    # Score each available player
    scored = []
    for player in available:
        name_key   = norm(player["name"])
        val        = values.get(name_key, {})
        base_score = val.get("total", -5.0)
        ep         = player.get("eligible_positions", [])
        adp_r      = adp_round_of(player)
        bpa_r      = bpa_rank_map.get(name_key, 999)
        reasons    = []

        if bpa_r <= 30:
            reasons.append(f"BPA 第 {bpa_r} 名")

        # Position need bonus
        pos_bonus, pos_label = 0.0, None
        for pos in SPECIFIC_SLOTS:
            if pos in ep and pos in needs_set:
                pos_bonus, pos_label = 2.0, pos
                break
        if not pos_label and "P" in ep and "P" in needs_set:
            pos_bonus, pos_label = 1.0, "P"
        elif not pos_label and "Util" in ep and "Util" in needs_set:
            pos_bonus, pos_label = 0.5, "Util"
        if pos_label:
            reasons.append(f"補 {pos_label} 空缺")

        # Category balance bonus
        cat_bonus = 0.0
        for wcat in weak_cat_names[:2]:
            z = val.get("cats", {}).get(wcat, 0)
            if z > 1.0:
                cat_bonus += 0.5
                reasons.append(f"補強 {wcat}（z={z:.1f}）")

        # Scarcity bonus
        scar_bonus = 0.0
        primary = player.get("position", "")
        if primary not in BENCH_SLOTS + ["Util"] and primary:
            ql = scarcity.get(primary, 99)
            if ql <= 5:
                scar_bonus, reasons_add = 1.5, f"{primary} 稀缺（只剩 {ql} 位）"
            elif ql <= 10:
                scar_bonus, reasons_add = 0.7, f"{primary} 剩餘不多"
            else:
                reasons_add = None
            if scar_bonus and reasons_add:
                reasons.append(reasons_add)

        # ADP urgency
        if adp_r < current_round:
            reasons.append("低於 ADP，可能快被選走！")
        elif adp_r > current_round + 1 and bpa_r > 14:
            reasons.append(f"可等下輪（約第 {adp_r} 輪）")

        # Flag no-stats players
        if not val:
            reasons.append("無歷史成績（新秀/傷兵）")

        scored.append({
            "name":              player["name"],
            "position":          player.get("position", ""),
            "eligible_positions": ep,
            "team":              player.get("team", ""),
            "headshot":          player.get("headshot", ""),
            "yahoo_rank":        player.get("yahoo_rank", 999),
            "adp_round":         adp_r,
            "base_value":        round(base_score, 2),
            "final_score":       round(base_score + pos_bonus + cat_bonus + scar_bonus, 2),
            "stats":             val.get("stats", {}),
            "cats":              val.get("cats", {}),
            "is_batter":         val.get("is_batter", player.get("is_batter", True)),
            "reasons":           reasons,
        })

    scored.sort(key=lambda x: x["final_score"], reverse=True)

    # Draft board
    draft_board = []
    for dr in draft_results:
        pk = dr["player_key"]
        p  = pool_by_key.get(pk, {})
        t  = teams_dict.get(dr["team_key"], {})
        draft_board.append({
            "round":       dr["round"],
            "pick":        dr["pick"],
            "team_name":   t.get("name", ""),
            "is_mine":     dr["team_key"] == my_team_key,
            "player_name": p.get("name", pk),
            "position":    p.get("position", ""),
            "team":        p.get("team", ""),
        })

    my_roster_out = sorted(my_players, key=lambda x: (not x["is_batter"], x["position"]))

    # Available by position (top 8)
    avail_by_pos = {}
    for pos in ["C", "1B", "2B", "3B", "SS", "OF", "SP", "RP"]:
        cands = [p for p in available if pos in p.get("eligible_positions", []) or p.get("position") == pos]
        cands.sort(key=lambda p: values.get(norm(p["name"]), {}).get("total", -99), reverse=True)
        avail_by_pos[pos] = [
            {
                "name":      p["name"],
                "team":      p.get("team", ""),
                "yahoo_rank": p.get("yahoo_rank", 999),
                "value":     round(values.get(norm(p["name"]), {}).get("total", 0), 2),
                "adp_round": adp_round_of(p),
            }
            for p in cands[:8]
        ]

    return {
        "draft_board":      draft_board,
        "my_roster":        my_roster_out,
        "position_needs":   needs,
        "category_balance": cat_balance,
        "weak_cats":        weak_cat_names,
        "recommendations":  scored[:top_n],
        "available_by_pos": avail_by_pos,
        "total_picks":      total_picks,
        "current_round":    current_round,
        "num_teams":        num_teams,
    }
