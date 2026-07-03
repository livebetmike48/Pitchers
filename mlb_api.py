"""
Thin client for the free public MLB Stats API. No key required.
"""
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


def get_active_roster_pitchers(team_id: int) -> list[dict]:
    resp = requests.get(
        f"{BASE}/teams/{team_id}/roster", params={"rosterType": "active"}, timeout=15
    )
    resp.raise_for_status()
    pitchers = []
    for entry in resp.json().get("roster", []):
        if (entry.get("position") or {}).get("abbreviation") == "P":
            pitchers.append({"id": entry["person"]["id"], "name": entry["person"]["fullName"]})
    return pitchers


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


def get_pitcher_game_log(person_id: int, season: int = CURRENT_SEASON) -> list[dict]:
    """
    Most recent starts/appearances for one pitcher, via MLB's dedicated
    game-log endpoint -- much cheaper than scanning box scores when you only
    need one player's history.
    """
    resp = requests.get(
        f"{BASE}/people/{person_id}/stats",
        params={"stats": "gameLog", "group": "pitching", "season": season, "gameType": "R"},
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
                "is_start": bool(stat.get("gamesStarted")),
                "decision": stat.get("decision") or stat.get("note"),
            })

    splits.sort(key=lambda s: s["date"] or "")
    return splits
