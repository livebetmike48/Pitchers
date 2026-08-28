"""
Thin client for the free public MLB Stats API. No key required.
"""
import os

import requests

BASE = "https://statsapi.mlb.com/api/v1"
CURRENT_SEASON = 2026


def get_all_teams() -> list[dict]:
    resp = requests.get(f"{BASE}/teams", params={"sportId": 1}, timeout=15)
    resp.raise_for_status()
    return [
        {"id": t["id"], "name": t["name"], "abbreviation": t["abbreviation"]}
        for t in resp.json().get("teams", [])
    ]


def get_probable_starters(date_str: str) -> list[dict]:
    """
    One entry per team per game on this date, with their probable starter
    if MLB has announced one yet (None if TBD).
    """
    resp = requests.get(
        f"{BASE}/schedule",
        params={"sportId": 1, "date": date_str, "hydrate": "probablePitcher"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    entries = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            for side in ("home", "away"):
                team = g["teams"][side]["team"]
                pp = g["teams"][side].get("probablePitcher")
                entries.append({
                    "team_id": team["id"],
                    "team_name": team["name"],
                    "pitcher_id": pp["id"] if pp else None,
                    "pitcher_name": pp["fullName"] if pp else None,
                })
    return entries


def get_roster_pitchers(team_id: int, roster_type: str = "active") -> list[dict]:
    """
    Pitchers on a team's roster.

    rosterType matters more than it looks. "active" excludes anyone on the
    injured list -- which is exactly the population this bot cares about when
    a guy is making a rehab start in the minors. Use "40Man" to include them.
    """
    resp = requests.get(
        f"{BASE}/teams/{team_id}/roster", params={"rosterType": roster_type}, timeout=15
    )
    resp.raise_for_status()
    pitchers = []
    for entry in resp.json().get("roster", []):
        if (entry.get("position") or {}).get("abbreviation") == "P":
            pitchers.append({"id": entry["person"]["id"], "name": entry["person"]["fullName"]})
    return pitchers


def get_active_roster_pitchers(team_id: int) -> list[dict]:
    """Back-compat alias. Prefer get_roster_pitchers."""
    return get_roster_pitchers(team_id, "active")


def get_live_games(date_str: str) -> list[dict]:
    resp = requests.get(
        f"{BASE}/schedule", params={"sportId": 1, "date": date_str}, timeout=15
    )
    resp.raise_for_status()
    games = []
    for date_entry in resp.json().get("dates", []):
        for g in date_entry.get("games", []):
            games.append({
                "game_pk": g["gamePk"],
                "abstract_state": g["status"].get("abstractGameState"),
                "home_team": g["teams"]["home"]["team"]["name"],
                "away_team": g["teams"]["away"]["team"]["name"],
            })
    return games


def get_boxscore(game_pk: int) -> dict:
    resp = requests.get(f"{BASE}/game/{game_pk}/boxscore", timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_starters(boxscore_json: dict) -> dict:
    """Returns {"home": {...starter line...}, "away": {...}} -- index 0 pitcher only."""
    result = {}
    for side in ("home", "away"):
        team_block = boxscore_json["teams"][side]
        team_name = team_block["team"]["name"]
        pitcher_ids = team_block.get("pitchers", [])
        players = team_block.get("players", {})

        starter = None
        if pitcher_ids:
            p = players.get(f"ID{pitcher_ids[0]}")
            if p:
                pitching = (p.get("stats") or {}).get("pitching") or {}
                starter = {
                    "id": pitcher_ids[0],
                    "name": p["person"]["fullName"],
                    "pitches": pitching.get("numberOfPitches", pitching.get("pitchesThrown", 0)),
                    "ip": pitching.get("inningsPitched", "0.0"),
                    "hits": pitching.get("hits", 0),
                    "er": pitching.get("earnedRuns", 0),
                    "bb": pitching.get("baseOnBalls", 0),
                    "so": pitching.get("strikeOuts", 0),
                }
        result[side] = {"team": team_name, "starter": starter}
    return result


def get_pitcher_game_log(
    person_id: int, season: int = CURRENT_SEASON, sport_id: int = 1
) -> list[dict]:
    """
    Most recent starts/appearances for one pitcher, via MLB's dedicated
    game-log endpoint -- much cheaper than scanning box scores when you only
    need one player's history.
    """
    resp = requests.get(
        f"{BASE}/people/{person_id}/stats",
        params={
            "stats": "gameLog",
            "group": "pitching",
            "season": season,
            "gameType": "R",
            "sportId": sport_id,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    splits = []
    for stat_block in data.get("stats", []):
        for split in stat_block.get("splits", []):
            stat = split.get("stat", {}) or {}
            splits.append({
                "date": split.get("date"),
                "opponent": (split.get("opponent") or {}).get("name"),
                "is_home": split.get("isHome"),
                "pitches": stat.get("numberOfPitches", stat.get("pitchesThrown", 0)),
                "ip": stat.get("inningsPitched", "0.0"),
                "hits": stat.get("hits", 0),
                "er": stat.get("earnedRuns", 0),
                "bb": stat.get("baseOnBalls", 0),
                "so": stat.get("strikeOuts", 0),
                "bf": stat.get("battersFaced", 0),
                "is_start": bool(stat.get("gamesStarted")),
                "decision": stat.get("decision") or stat.get("note"),
            })

    splits.sort(key=lambda s: s["date"] or "")
    return splits


# ---------------------------------------------------------------------------
# Minor league support
# ---------------------------------------------------------------------------
# MLB's Stats API is the same feed behind mlb.com and milb.com. The gameLog
# endpoint returns MLB only unless you pass an explicit sportId, which is why
# a pitcher's Double-A rehab start looks like "no game log" to an MLB-only
# query. These are the level ids we search, in descending order of level.
LEVELS = {
    1:  "MLB",
    11: "Triple-A",
    12: "Double-A",
    13: "High-A",
    14: "Single-A",
    16: "Rookie",
}
MINOR_SPORT_IDS = [11, 12, 13, 14, 16]

# Only go looking in the minors when the MLB log is empty or this stale.
# Keeps /starters at one API call per pitcher on the normal path.
STALE_START_DAYS = int(os.getenv("STALE_START_DAYS", "10"))


def get_pitcher_game_log_multi(
    person_id: int,
    season: int = CURRENT_SEASON,
    sport_ids: list[int] | None = None,
) -> list[dict]:
    """
    Game log across several levels, merged and sorted oldest-first.

    Each split carries the sport_id it came from plus a human "level" label,
    so callers can say "in his last start in Triple-A" without a second lookup.
    A level that errors or returns nothing is skipped -- one bad level must
    never take down the whole lookup.
    """
    if sport_ids is None:
        sport_ids = [1] + MINOR_SPORT_IDS

    merged: list[dict] = []
    for sid in sport_ids:
        try:
            rows = get_pitcher_game_log(person_id, season=season, sport_id=sid)
        except Exception:
            continue
        for r in rows:
            r["sport_id"] = sid
            r["level"] = LEVELS.get(sid, f"sportId {sid}")
            merged.append(r)

    merged.sort(key=lambda s: (s["date"] or "", s.get("sport_id", 0)))
    return merged


def last_start_any_level(
    person_id: int,
    as_of: str,
    season: int = CURRENT_SEASON,
    stale_days: int = None,
) -> dict | None:
    """
    The pitcher's most recent START, wherever it happened.

    Cheap path first: query MLB only. If that produces a start inside the
    staleness window we stop there -- one API call, same cost as today. We
    only pay for the minor league lookups when the MLB answer is missing or
    old, which is exactly the case Mike cares about (guys coming back from
    injury, or just called up).

    Returns the split dict (with "level" and "sport_id") or None.
    """
    if stale_days is None:
        stale_days = STALE_START_DAYS

    def _last_start(rows):
        starts = [s for s in rows if s.get("is_start")]
        return starts[-1] if starts else None

    try:
        mlb_rows = get_pitcher_game_log(person_id, season=season, sport_id=1)
    except Exception:
        mlb_rows = []
    for r in mlb_rows:
        r["sport_id"] = 1
        r["level"] = "MLB"

    mlb_last = _last_start(mlb_rows)
    if mlb_last and _days_between(mlb_last["date"], as_of) <= stale_days:
        return mlb_last

    # MLB answer is missing or stale -- now it's worth checking the minors.
    minor_rows = get_pitcher_game_log_multi(person_id, season=season, sport_ids=MINOR_SPORT_IDS)
    minor_last = _last_start(minor_rows)

    if mlb_last and minor_last:
        return minor_last if minor_last["date"] > mlb_last["date"] else mlb_last
    return minor_last or mlb_last


def _days_between(earlier: str, later: str) -> int:
    from datetime import datetime as _dt
    try:
        return (_dt.strptime(later, "%Y-%m-%d") - _dt.strptime(earlier, "%Y-%m-%d")).days
    except Exception:
        return 10 ** 6



# ---------------------------------------------------------------------------
# Player index -- the fallback when a name isn't on any MLB roster
# ---------------------------------------------------------------------------
# The autocomplete directory is built from MLB rosters, so it cannot see a
# pure minor leaguer, and (with rosterType "active") it cannot see anyone on
# the IL either. This index is the safety net: one cheap call per level,
# cached, covering MLB + Triple-A + Double-A.
PLAYER_INDEX_SPORT_IDS = [1, 11, 12]
_player_index_cache: dict = {}


def get_player_index(season: int = CURRENT_SEASON, sport_ids: list[int] = None) -> list[dict]:
    """
    All players at the given levels for a season: [{id, name, level, sport_id}].
    Cached per (season, levels) -- these lists change slowly and the whole
    point is to avoid paying for them on every lookup.
    """
    if sport_ids is None:
        sport_ids = PLAYER_INDEX_SPORT_IDS
    key = (season, tuple(sport_ids))
    if key in _player_index_cache:
        return _player_index_cache[key]

    out: list[dict] = []
    seen: set[int] = set()
    for sid in sport_ids:
        try:
            resp = requests.get(
                f"{BASE}/sports/{sid}/players", params={"season": season}, timeout=30
            )
            resp.raise_for_status()
            people = resp.json().get("people", [])
        except Exception:
            continue
        for p in people:
            pid = p.get("id")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            out.append({
                "id": pid,
                "name": p.get("fullName", ""),
                "level": LEVELS.get(sid, f"sportId {sid}"),
                "sport_id": sid,
                "position": ((p.get("primaryPosition") or {}).get("abbreviation") or ""),
            })
    if out:
        _player_index_cache[key] = out
    return out


def find_pitchers(name: str, season: int = CURRENT_SEASON) -> list[dict]:
    """
    Substring name search across the player index, pitchers first.
    Returns [] rather than raising -- callers decide how to report a miss.
    """
    needle = (name or "").strip().lower()
    if not needle:
        return []
    idx = get_player_index(season)
    matches = [p for p in idx if needle in p["name"].lower()]
    matches.sort(key=lambda p: (p["position"] != "P", p["sport_id"], p["name"]))
    return matches


def clear_player_index_cache():
    _player_index_cache.clear()
