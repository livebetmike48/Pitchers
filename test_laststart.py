"""
Offline tests for the level-aware last-start lookup and the name resolver.

No network: mlb_api's fetchers are monkeypatched with fixtures, so these
verify the LOGIC (which start wins, what the line reads, when we pay for
extra API calls, who the resolver can find) rather than the API's behaviour.
Confirming that sportId actually filters the gameLog endpoint is what the
/laststart command is for.
"""
import sys

import mlb_api
from bot import format_last_start


def _split(date, pitches, is_start=True, ip="5.0"):
    return {"date": date, "pitches": pitches, "is_start": is_start, "ip": ip,
            "opponent": "Someone", "so": 5, "bb": 1, "hits": 4, "er": 2, "bf": 20}


def fake_log(mapping):
    """mapping: {sport_id: [splits]}"""
    def _f(person_id, season=mlb_api.CURRENT_SEASON, sport_id=1):
        return [dict(s) for s in mapping.get(sport_id, [])]
    return _f


def fake_index(rows):
    def _f(season=mlb_api.CURRENT_SEASON, sport_ids=None):
        return rows
    return _f


def run(name, fn):
    try:
        fn()
        print("  PASS  " + name)
        return True
    except AssertionError as e:
        print("  FAIL  " + name + ": " + str(e))
        return False
    except Exception as e:
        print("  ERROR " + name + ": " + repr(e))
        return False


# --- last start, any level -------------------------------------------------

def t_healthy_mlb_arm_costs_one_call():
    calls = []
    base = fake_log({1: [_split("2026-08-16", 81)]})

    def counting(person_id, season=mlb_api.CURRENT_SEASON, sport_id=1):
        calls.append(sport_id)
        return base(person_id, season, sport_id)

    mlb_api.get_pitcher_game_log = counting
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["level"] == "MLB", last["level"]
    assert calls == [1], "expected one MLB call, got " + str(calls)


def t_stale_mlb_start_finds_aaa_rehab():
    mlb_api.get_pitcher_game_log = fake_log({
        1:  [_split("2026-07-02", 95)],
        11: [_split("2026-08-19", 62)],
    })
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["level"] == "Triple-A", last["level"]
    assert last["pitches"] == 62


def t_never_called_up_finds_double_a():
    mlb_api.get_pitcher_game_log = fake_log({12: [_split("2026-08-17", 74)]})
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["level"] == "Double-A", last["level"]


def t_mlb_wins_when_it_is_the_most_recent():
    mlb_api.get_pitcher_game_log = fake_log({
        1:  [_split("2026-08-20", 88)],
        11: [_split("2026-08-01", 70)],
    })
    last = mlb_api.last_start_any_level(1, as_of="2026-08-30")
    assert last["level"] == "MLB", last["level"]


def t_relief_appearances_are_not_starts():
    mlb_api.get_pitcher_game_log = fake_log({
        11: [_split("2026-08-14", 66, is_start=True),
             _split("2026-08-20", 28, is_start=False)],
    })
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["date"] == "2026-08-14", last["date"]


def t_one_broken_level_does_not_kill_the_lookup():
    good = fake_log({11: [_split("2026-08-19", 62)]})

    def flaky(person_id, season=mlb_api.CURRENT_SEASON, sport_id=1):
        if sport_id == 12:
            raise RuntimeError("500 from upstream")
        return good(person_id, season, sport_id)

    mlb_api.get_pitcher_game_log = flaky
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["level"] == "Triple-A", last["level"]


def t_no_starts_anywhere_returns_none():
    mlb_api.get_pitcher_game_log = fake_log({})
    assert mlb_api.last_start_any_level(1, as_of="2026-08-22") is None


# --- line formatting -------------------------------------------------------

def t_mlb_line_is_byte_identical_to_today():
    last = dict(_split("2026-08-16", 81), level="MLB")
    got = format_last_start(last, "2026-08-22")
    want = "Threw 81 pitches in his last start (2026-08-16, 5 days rest)"
    assert got == want, got


def t_minor_line_adds_the_level_phrase():
    last = dict(_split("2026-08-16", 81), level="Triple-A")
    got = format_last_start(last, "2026-08-22")
    want = "Threw 81 pitches in his last start in Triple-A (2026-08-16, 5 days rest)"
    assert got == want, got


def t_missing_pitch_count_is_never_invented():
    last = dict(_split("2026-08-16", 0, ip="4.2"), level="Double-A")
    got = format_last_start(last, "2026-08-22")
    assert "pitch count not reported" in got, got
    assert "Threw 0 pitches" not in got, got


# --- name resolver (the Davis Martin case) ---------------------------------

def t_il_pitcher_found_via_index():
    mlb_api.get_player_index = fake_index([
        {"id": 669952, "name": "Davis Martin", "level": "MLB", "sport_id": 1, "position": "P"},
    ])
    got = mlb_api.find_pitchers("davis martin")
    assert got and got[0]["id"] == 669952, got


def t_pitchers_rank_above_position_players():
    mlb_api.get_player_index = fake_index([
        {"id": 2, "name": "Chris Martin", "level": "MLB", "sport_id": 1, "position": "OF"},
        {"id": 1, "name": "Davis Martin", "level": "MLB", "sport_id": 1, "position": "P"},
    ])
    got = mlb_api.find_pitchers("martin")
    assert got[0]["position"] == "P", got


def t_mlb_ranks_above_minors_on_ties():
    mlb_api.get_player_index = fake_index([
        {"id": 2, "name": "Davis Martin", "level": "Double-A", "sport_id": 12, "position": "P"},
        {"id": 1, "name": "Davis Martin", "level": "MLB", "sport_id": 1, "position": "P"},
    ])
    got = mlb_api.find_pitchers("davis martin")
    assert got[0]["sport_id"] == 1, got


def t_unknown_name_returns_empty_not_error():
    mlb_api.get_player_index = fake_index([])
    assert mlb_api.find_pitchers("nobody at all") == []


def t_blank_name_short_circuits():
    hits = []

    def counting(season=mlb_api.CURRENT_SEASON, sport_ids=None):
        hits.append(1)
        return []

    mlb_api.get_player_index = counting
    assert mlb_api.find_pitchers("   ") == []
    assert not hits, "should not hit the index for a blank name"


TESTS = [
    ("healthy MLB arm still costs one API call", t_healthy_mlb_arm_costs_one_call),
    ("stale MLB start finds the AAA rehab start", t_stale_mlb_start_finds_aaa_rehab),
    ("never-called-up arm found in Double-A", t_never_called_up_finds_double_a),
    ("MLB wins when it is the most recent", t_mlb_wins_when_it_is_the_most_recent),
    ("relief appearances are not starts", t_relief_appearances_are_not_starts),
    ("one broken level does not kill the lookup", t_one_broken_level_does_not_kill_the_lookup),
    ("no starts anywhere returns None", t_no_starts_anywhere_returns_none),
    ("MLB line byte-identical to today", t_mlb_line_is_byte_identical_to_today),
    ("minor league line adds the level phrase", t_minor_line_adds_the_level_phrase),
    ("missing pitch count is never invented", t_missing_pitch_count_is_never_invented),
    ("IL pitcher found via cross-level index", t_il_pitcher_found_via_index),
    ("pitchers rank above position players", t_pitchers_rank_above_position_players),
    ("MLB ranks above minors on name ties", t_mlb_ranks_above_minors_on_ties),
    ("unknown name returns empty, not an error", t_unknown_name_returns_empty_not_error),
    ("blank name never hits the index", t_blank_name_short_circuits),
]

if __name__ == "__main__":
    print("last-start + resolver tests")
    results = [run(name, fn) for name, fn in TESTS]
    print("\n%d/%d passed" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)
