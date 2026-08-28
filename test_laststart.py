"""
Offline tests for the level-aware last-start lookup.

No network: mlb_api.get_pitcher_game_log is monkeypatched with fixtures, so
these verify the LOGIC (which start wins, what the line reads, when we pay
for the extra API calls) rather than the API's behaviour. Confirming that
sportId actually filters the gameLog endpoint is what /laststart is for.
"""
import sys, types

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


def run(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
        return True
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        return False


def t_healthy_mlb_arm_costs_one_call():
    calls = []
    base = fake_log({1: [_split("2026-08-16", 81)]})

    def counting(person_id, season=mlb_api.CURRENT_SEASON, sport_id=1):
        calls.append(sport_id)
        return base(person_id, season, sport_id)

    mlb_api.get_pitcher_game_log = counting
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["level"] == "MLB", last["level"]
    assert calls == [1], f"expected one MLB call, got {calls}"


def t_stale_mlb_start_finds_aaa_rehab():
    mlb_api.get_pitcher_game_log = fake_log({
        1:  [_split("2026-07-02", 95)],          # 50+ days ago
        11: [_split("2026-08-19", 62)],          # rehab start 3 days ago
    })
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["level"] == "Triple-A", last["level"]
    assert last["pitches"] == 62


def t_never_called_up_finds_double_a():
    mlb_api.get_pitcher_game_log = fake_log({12: [_split("2026-08-17", 74)]})
    last = mlb_api.last_start_any_level(1, as_of="2026-08-22")
    assert last["level"] == "Double-A", last["level"]
    assert last["pitches"] == 74


def t_mlb_wins_when_it_is_the_most_recent():
    """A guy who was optioned, came back, and has since started in MLB."""
    mlb_api.get_pitcher_game_log = fake_log({
        1:  [_split("2026-08-20", 88)],
        11: [_split("2026-08-01", 70)],
    })
    last = mlb_api.last_start_any_level(1, as_of="2026-08-30")
    assert last["level"] == "MLB", last["level"]


def t_relief_appearances_are_not_starts():
    mlb_api.get_pitcher_game_log = fake_log({
        11: [_split("2026-08-20", 28, is_start=False),
             _split("2026-08-14", 66, is_start=True)],
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
    assert last["level"] == "Triple-A"


def t_no_starts_anywhere_returns_none():
    mlb_api.get_pitcher_game_log = fake_log({})
    assert mlb_api.last_start_any_level(1, as_of="2026-08-22") is None


def t_mlb_line_is_byte_identical_to_today():
    last = dict(_split("2026-08-16", 81), level="MLB")
    got = format_last_start(last, "2026-08-22")
    assert got == "Threw 81 pitches in his last start (2026-08-16, 5 days rest)", got


def t_minor_line_adds_the_level_phrase():
    last = dict(_split("2026-08-16", 81), level="Triple-A")
    got = format_last_start(last, "2026-08-22")
    assert got == "Threw 81 pitches in his last start in Triple-A (2026-08-16, 5 days rest)", got


def t_missing_pitch_count_is_never_invented():
    last = dict(_split("2026-08-16", 0, ip="4.2"), level="Double-A")
    got = format_last_start(last, "2026-08-22")
    assert "pitch count not reported" in got, got
    assert "Threw 0 pitches" not in got, got


if __name__ == "__main__":
    print("last-start (any level) tests")
    results = [
        run("healthy MLB arm still costs one API call", t_healthy_mlb_arm_costs_one_call),
        run("stale MLB start finds the AAA rehab start", t_stale_mlb_start_finds_aaa_rehab),
        run("never-called-up arm found in Double-A", t_never_called_up_finds_double_a),
        run("MLB wins when it is the most recent", t_mlb_wins_when_it_is_the_most_recent),
        run("relief appearances are not starts", t_relief_appearances_are_not_starts),
        run("one broken level does not kill the lookup", t_one_broken_level_does_not_kill_the_lookup),
        run("no starts anywhere returns None", t_no_starts_anywhere_returns_none),
        run("MLB line byte-identical to today", t_mlb_line_is_byte_identical_to_today),
        run("minor league line adds the level phrase", t_minor_line_adds_the_level_phrase),
        run("missing pitch count is never invented", t_missing_pitch_count_is_never_invented),
    ]
    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)
