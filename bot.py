import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone, time as dtime

import discord
from discord import app_commands
from discord.ext import tasks

import mlb_api
import storage
import stats

from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "5"))
ROSTER_REFRESH_HOURS = float(os.getenv("ROSTER_REFRESH_HOURS", "6"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("starters_bot")

intents = discord.Intents.default()


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")



def format_last_start(last: dict, as_of: str) -> str:
    """
    One line describing a pitcher's most recent start, wherever it happened.

    MLB starts read exactly as they always have -- the level phrase is added
    only when the start was somewhere other than the big leagues:

        Threw 81 pitches in his last start (2026-08-16, 5 days rest)
        Threw 81 pitches in his last start in Triple-A (2026-08-16, 5 days rest)

    Days of rest stays on every line on purpose: it is what makes a stale
    reading visible instead of silent.
    """
    rest_days = (
        datetime.strptime(as_of, "%Y-%m-%d") - datetime.strptime(last["date"], "%Y-%m-%d")
    ).days - 1
    rest_str = f", {rest_days} days rest" if rest_days >= 0 else ""

    level = last.get("level", "MLB")
    where = "" if level == "MLB" else f" in {level}"

    pitches = last.get("pitches")
    if not pitches:
        # Never invent a pitch count. Some minor league lines omit it.
        return (
            f"Last start{where} was {last['date']}{rest_str} "
            f"({last.get('ip', '0.0')} IP, pitch count not reported)"
        )
    return f"Threw {pitches} pitches in his last start{where} ({last['date']}{rest_str})"


def build_game_embed(game: dict, starters: dict) -> discord.Embed:
    away, home = starters["away"], starters["home"]
    embed = discord.Embed(
        title=f"{away['team']} @ {home['team']} — Starter Pitch Counts",
        color=discord.Color.blue(),
    )
    for side in (away, home):
        s = side["starter"]
        if s:
            value = (
                f"**{s['name']}**\n{s['pitches']} pitches, {s['ip']} IP, "
                f"{s['hits']}H {s['er']}ER {s['bb']}BB {s['so']}K"
            )
        else:
            value = "No starter data"
        embed.add_field(name=side["team"], value=value, inline=False)
    embed.set_footer(text="Data: MLB Stats API")
    return embed


def build_pitchcount_embed(pitcher_name: str, splits: list[dict]) -> discord.Embed:
    """Strictly pitch-count focused: last outing + recent pitch counts, nothing else."""
    if not splits:
        return discord.Embed(
            title=pitcher_name,
            description="No game log found for this season yet.",
            color=discord.Color.light_grey(),
        )
    last = splits[-1]
    tag_text = "Start" if last["is_start"] else "Relief appearance"

    embed = discord.Embed(
        title=f"{pitcher_name} — Pitch Count",
        description=(
            f"{last['date']} vs {last['opponent']} ({tag_text})\n\n"
            f"**{last['pitches']} pitches** • {last['ip']} IP"
        ),
        color=discord.Color.blue(),
    )

    if len(splits) >= 2:
        recent = splits[-10:][::-1]
        lines = [f"{s['date']}: **{s['pitches']}p**, {s['ip']} IP" for s in recent]
        embed.add_field(name="Recent pitch counts", value="\n".join(lines), inline=False)

    avg10 = stats.summarize_outings(splits, 10)
    if avg10:
        embed.add_field(
            name="Average (last 10 starts)",
            value=f"{avg10['avg_pitches']} pitches/start",
            inline=False,
        )

    embed.set_footer(text="Data: MLB Stats API")
    return embed


def build_pitcher_embed(pitcher_name: str, splits: list[dict]) -> discord.Embed:
    """Full breakdown: streaks, rolling windows, ERA/K9/WHIP/BF -- mirrors /batter."""
    if not splits:
        return discord.Embed(
            title=pitcher_name,
            description="No game log found for this season yet.",
            color=discord.Color.light_grey(),
        )
    last = splits[-1]
    tag_text = "Start" if last["is_start"] else "Relief appearance"

    last10 = stats.summarize_outings(splits, 10)
    hot_cold = stats.hot_cold_tag(last10)

    title = f"{pitcher_name} — Last Outing"
    if hot_cold:
        title = f"{title}  {hot_cold}"

    embed = discord.Embed(
        title=title,
        description=(
            f"{last['date']} vs {last['opponent']} ({tag_text})\n\n"
            f"**{last['pitches']} pitches** • {last['ip']} IP\n"
            f"{last['hits']}H {last['er']}ER {last['bb']}BB {last['so']}K • {last.get('bf', 0)} BF"
        ),
        color=discord.Color.blue(),
    )

    pitcher_streaks = stats.get_pitcher_streaks(splits)
    notable = stats.notable_pitcher_streak_labels(pitcher_streaks)
    if notable:
        embed.add_field(name="Active streaks", value="\n".join(notable), inline=False)

    def window_field(summary: dict | None, label: str):
        if not summary:
            return
        embed.add_field(
            name=label,
            value=(
                f"ERA: **{summary['era']}** • K/9: **{summary['k9']}** • WHIP: {summary['whip']}\n"
                f"{summary['total_ip']} IP, {summary['total_bf']} BF over {summary['count']} starts "
                f"(avg {summary['avg_pitches']} pitches)"
            ),
            inline=False,
        )

    window_field(stats.summarize_outings(splits, 5), "Last 5 Starts")
    window_field(stats.summarize_outings(splits, 10), "Last 10 Starts")
    window_field(stats.summarize_outings(splits, 20), "Last 20 Starts")
    window_field(stats.summarize_outings(splits, len(splits)), "Season")

    if len(splits) >= 2:
        recent = splits[-5:][::-1]
        lines = [f"{s['date']}: {s['pitches']}p, {s['ip']} IP" for s in recent]
        embed.add_field(name="Recent outings", value="\n".join(lines), inline=False)

    embed.set_footer(text="Data: MLB Stats API • Hot/Cold tags need 3+ starts in the window")
    return embed


class StartersBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.player_directory: list[dict] = []  # [{"id":, "name":, "team":}]
        self.teams: list[dict] = []

    async def setup_hook(self):
        try:
            storage.init_db()
        except Exception as e:
            log.error("Failed to init database at %s: %s -- falling back to local storage", storage.DB_PATH, e)
            storage.DB_PATH = "starters_bot_fallback.db"
            storage.init_db()
        try:
            self.teams = mlb_api.get_all_teams()
        except Exception as e:
            log.error("Failed to fetch team list at startup: %s", e)
            self.teams = []
        await self.refresh_player_directory()

        pitchcount_cmd = app_commands.Command(
            name="pitchcount",
            description="Just the pitch counts: last outing + recent starts, nothing else",
            callback=self._pitchcount_callback,
        )
        self.tree.add_command(pitchcount_cmd)
        pitchcount_cmd.autocomplete("name")(self._name_autocomplete)

        pitcher_cmd = app_commands.Command(
            name="pitcher",
            description="Full breakdown: streaks, Last 5/10/20/Season, ERA/K9/WHIP/BF",
            callback=self._pitcher_callback,
        )
        self.tree.add_command(pitcher_cmd)
        pitcher_cmd.autocomplete("name")(self._name_autocomplete)

        setchannel_cmd = app_commands.Command(
            name="setchannel",
            description="Set this channel to receive starter pitch count reports",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        laststart_cmd = app_commands.Command(
            name="laststart",
            description="His last start, wherever it was — MLB or the minors",
            callback=self._laststart_callback,
        )
        self.tree.add_command(laststart_cmd)
        laststart_cmd.autocomplete("name")(self._name_autocomplete)

        starters_cmd = app_commands.Command(
            name="starters",
            description="Probable starters for a date (YYYY-MM-DD, blank = today), with last pitch count",
            callback=self._starters_callback,
        )
        self.tree.add_command(starters_cmd)

        hotstarters_cmd = app_commands.Command(
            name="hotstarters",
            description="Which probable starters for a date are trending hot (last 5 starts)",
            callback=self._hotstarters_callback,
        )
        self.tree.add_command(hotstarters_cmd)

        coldstarters_cmd = app_commands.Command(
            name="coldstarters",
            description="Which probable starters for a date are trending cold (last 5 starts)",
            callback=self._coldstarters_callback,
        )
        self.tree.add_command(coldstarters_cmd)

        try:
            guild_id = os.getenv("GUILD_ID")
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s (fast, avoids global rate limits)", len(synced), guild_id)

                # One-time cleanup: earlier deploys registered these commands
                # GLOBALLY. Switching to guild-sync doesn't remove those old
                # global copies on Discord's side, so both were showing up
                # side by side -- explicitly wiping the global registration
                # here fixes the duplicate-command symptom for good.
                self.tree.clear_commands(guild=None)
                await self.tree.sync()
                log.info("Cleared stale global command registration (one-time cleanup)")
            else:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands globally (set GUILD_ID env var for faster, safer syncing)", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    async def refresh_player_directory(self):
        try:
            teams = mlb_api.get_all_teams()
        except Exception as e:
            log.error("Failed to fetch teams for directory: %s", e)
            return
        directory = []
        seen: set[int] = set()
        # "active" alone hides anyone on the IL -- which is exactly who we need
        # when a guy is making a rehab start. 40Man carries IL players, so we
        # merge both and de-dupe.
        for team in teams:
            for roster_type in ("active", "40Man"):
                try:
                    pitchers = mlb_api.get_roster_pitchers(team["id"], roster_type)
                except Exception as e:
                    log.error("Failed to fetch %s roster for team %s: %s", roster_type, team["id"], e)
                    continue
                for p in pitchers:
                    if p["id"] in seen:
                        continue
                    seen.add(p["id"])
                    directory.append({"id": p["id"], "name": p["name"], "team": team["abbreviation"]})
        self.player_directory = directory
        log.info("Player directory refreshed: %d pitchers (active + 40-man)", len(directory))

        # Warm the cross-level index here, on the slow path, so autocomplete
        # can search MLB + Triple-A + Double-A without ever making a network
        # call inside Discord's ~3 second autocomplete budget.
        try:
            n = await asyncio.to_thread(mlb_api.warm_player_index)
            log.info("Player index warmed: %d players across MLB/AAA/AA", n)
        except Exception as e:
            log.error("Player index warm failed (autocomplete falls back to rosters): %s", e)

    async def _name_autocomplete(self, interaction: discord.Interaction, current: str):
        """
        Suggestions come from the MLB roster directory FIRST, then from the
        cross-level player index (Triple-A / Double-A). Without the second
        source a rehabbing or not-yet-called-up pitcher never appears in the
        dropdown, so there is no way to reach him at all -- the whole point
        of the minor league lookup.

        Never fetches: allow_fetch=False means a cold cache just yields the
        roster names rather than blowing Discord's autocomplete timeout.
        """
        current_lower = (current or "").lower()
        choices: list[app_commands.Choice] = []
        seen: set[int] = set()

        for p in self.player_directory:
            if current_lower in p["name"].lower():
                seen.add(p["id"])
                choices.append(
                    app_commands.Choice(name=f"{p['name']} ({p['team']})", value=str(p["id"]))
                )
                if len(choices) >= 25:
                    return choices

        if current_lower:
            try:
                extra = mlb_api.find_pitchers(current_lower, allow_fetch=False)
            except Exception:
                extra = []
            for p in extra:
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                label = f"{p['name']} ({p['level']})"
                choices.append(app_commands.Choice(name=label[:100], value=str(p["id"])))
                if len(choices) >= 25:
                    break
        return choices

    def _resolve_pitcher(self, name: str):
        """`name` is the person_id if picked from autocomplete; falls back to a
        substring name search if the person typed free text and hit enter."""
        person_id = None
        pitcher_name = name
        if name.isdigit():
            person_id = int(name)
            match = next((p for p in self.player_directory if p["id"] == person_id), None)
            if match:
                pitcher_name = match["name"]
        else:
            match = next((p for p in self.player_directory if name.lower() in p["name"].lower()), None)
            if match:
                person_id = match["id"]
                pitcher_name = match["name"]
        return person_id, pitcher_name

    def _resolve_pitcher_deep(self, name: str):
        """
        Directory first, then the cross-level player index.

        The directory is built from MLB rosters, so it misses pure minor
        leaguers entirely. The index covers MLB + Triple-A + Double-A, which
        is what makes a rehabbing or not-yet-called-up arm findable at all.
        Returns (person_id, display_name, note) -- note is a short string
        explaining an unusual match, or None.
        """
        person_id, pitcher_name = self._resolve_pitcher(name)
        if person_id is not None:
            return person_id, pitcher_name, None
        if name.isdigit():
            return int(name), name, None

        matches = mlb_api.find_pitchers(name)
        if not matches:
            return None, name, None
        best = matches[0]
        note = None
        if best["level"] != "MLB":
            note = f"Not on an MLB roster — found in {best['level']}."
        if len(matches) > 1:
            others = ", ".join(f"{m['name']} ({m['level']})" for m in matches[1:4])
            note = (note or "") + f" Other matches: {others}."
        return best["id"], best["name"], note

    async def _pitchcount_callback(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        person_id, pitcher_name, _note = await asyncio.to_thread(self._resolve_pitcher_deep, name)
        if person_id is None:
            await interaction.followup.send(
                f"Couldn't find a pitcher matching '{name}'. Try selecting from the "
                f"suggestions as you type."
            )
            return
        try:
            splits = mlb_api.get_pitcher_game_log(person_id)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return
        await interaction.followup.send(embed=build_pitchcount_embed(pitcher_name, splits))

    async def _pitcher_callback(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()
        person_id, pitcher_name, _note = await asyncio.to_thread(self._resolve_pitcher_deep, name)
        if person_id is None:
            await interaction.followup.send(
                f"Couldn't find a pitcher matching '{name}'. Try selecting from the "
                f"suggestions as you type."
            )
            return
        try:
            splits = mlb_api.get_pitcher_game_log(person_id)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return
        await interaction.followup.send(embed=build_pitcher_embed(pitcher_name, splits))

    async def _laststart_callback(self, interaction: discord.Interaction, name: str):
        """
        Verification surface. Shows, per level, what the API actually returned
        and which start won -- so a wrong answer is visible instead of quietly
        wrong inside the automated starters post.
        """
        await interaction.response.defer()
        person_id, resolved, note = await asyncio.to_thread(self._resolve_pitcher_deep, name)
        if not person_id:
            await interaction.followup.send(
                f"Couldn't find a pitcher named '{name}' on an MLB roster or in the "
                f"Triple-A / Double-A player index."
            )
            return

        today = et_date_str(0)
        try:
            last = await asyncio.to_thread(
                mlb_api.last_start_any_level, person_id, today
            )
        except Exception as e:
            await interaction.followup.send(f"Lookup failed: {e}")
            return

        if not last:
            await interaction.followup.send(
                embed=discord.Embed(
                    title=resolved,
                    description="No start found at any level this season.",
                    color=discord.Color.light_grey(),
                )
            )
            return

        embed = discord.Embed(
            title=f"{resolved} — last start",
            description=format_last_start(last, today) + ".",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Line",
            value=(
                f"{last.get('ip', '0.0')} IP, {last.get('hits', 0)}H "
                f"{last.get('er', 0)}ER {last.get('bb', 0)}BB {last.get('so', 0)}K"
                + (f" vs {last['opponent']}" if last.get("opponent") else "")
            ),
            inline=False,
        )
        if note:
            embed.add_field(name="Name match", value=note, inline=False)
        embed.set_footer(text="Data: MLB Stats API (same feed as mlb.com / milb.com)")
        await interaction.followup.send(embed=embed)

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(
            f"✅ Starter pitch count reports will post in {interaction.channel.mention}."
        )

    async def _starters_callback(self, interaction: discord.Interaction, date: str | None = None):
        await interaction.response.defer()
        date_str = date or et_date_str(0)

        try:
            lines = await asyncio.to_thread(self._build_starters_lines_sync, date_str)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return

        header = f"__**Probable Starters — {date_str}**__\n\n"
        await self._send_chunked(interaction, header, lines)

    def _build_starters_lines_sync(self, date_str: str) -> list[str]:
        """Synchronous version for asyncio.to_thread -- avoids blocking the
        event loop during the ~30 sequential API calls this makes."""
        entries = mlb_api.get_probable_starters(date_str)
        entries_by_team = {e["team_id"]: e for e in entries}
        lines = []
        for team in sorted(self.teams, key=lambda t: t["name"]):
            entry = entries_by_team.get(team["id"])
            if not entry:
                lines.append(f"**{team['name']}**\nOff\n")
                continue
            if not entry["pitcher_id"]:
                lines.append(f"**{team['name']}**\nProbable starter not yet announced\n")
                continue

            last_pitch_line = "No prior start logged this season yet"
            try:
                last = mlb_api.last_start_any_level(entry["pitcher_id"], as_of=date_str)
                if last:
                    last_pitch_line = format_last_start(last, date_str)
            except Exception as e:
                log.error("Game log lookup failed for %s: %s", entry["pitcher_name"], e)

            lines.append(f"**{team['name']}**\n{entry['pitcher_name']} makes the start. {last_pitch_line}.\n")
        return lines

    async def _hotstarters_callback(self, interaction: discord.Interaction, date: str | None = None):
        await self._hot_or_cold(interaction, date, want_tag="🔥 Hot", label="Hot", emoji="🔥")

    async def _coldstarters_callback(self, interaction: discord.Interaction, date: str | None = None):
        await self._hot_or_cold(interaction, date, want_tag="🥶 Cold", label="Cold", emoji="🥶")

    async def _hot_or_cold(self, interaction: discord.Interaction, date: str | None, want_tag: str, label: str, emoji: str):
        await interaction.response.defer()
        date_str = date or et_date_str(0)

        try:
            entries = mlb_api.get_probable_starters(date_str)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return

        result_lines = []
        for entry in entries:
            if not entry["pitcher_id"]:
                continue
            try:
                splits = mlb_api.get_pitcher_game_log(entry["pitcher_id"])
            except Exception as e:
                log.error("Game log lookup failed for %s: %s", entry["pitcher_name"], e)
                continue

            last5 = stats.summarize_outings(splits, 5)
            tag = stats.hot_cold_tag(last5)
            if tag != want_tag or not last5:
                continue

            result_lines.append(
                f"**{entry['pitcher_name']}** ({entry['team_name']}) — "
                f"{last5['era']} ERA, {last5['k9']} K/9 over last {last5['count']} starts\n"
            )

        header = f"__**{emoji} {label} Starters — {date_str}**__\n\n"
        if not result_lines:
            await interaction.followup.send(header + "None qualify today.")
            return
        await self._send_chunked(interaction, header, result_lines)

    async def _send_chunked(self, interaction: discord.Interaction, header: str, lines: list[str], limit: int = 1900):
        chunk = header
        first = True
        for line in lines:
            if len(chunk) + len(line) > limit:
                await self._send_one(interaction, chunk, first)
                chunk = ""
                first = False
            chunk += line + "\n"
        if chunk.strip():
            await self._send_one(interaction, chunk, first)

    async def _send_one(self, interaction: discord.Interaction, content: str, is_first: bool):
        if is_first:
            await interaction.followup.send(content)
        else:
            await interaction.channel.send(content)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not poll_games.is_running():
            poll_games.start(self)
        if not refresh_directory_loop.is_running():
            refresh_directory_loop.start(self)
        if not watchdog.is_running():
            watchdog.start()
        if not scheduled_starters_post.is_running():
            scheduled_starters_post.start(self)


client = StartersBot()


@tasks.loop(minutes=POLL_MINUTES)
async def poll_games(bot: StartersBot):
    try:
        channel_id = storage.get_config("announce_channel_id")
        if not channel_id:
            return
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            return

        for offset in (0, -1):
            date_str = et_date_str(offset)
            try:
                games = mlb_api.get_live_games(date_str)
            except Exception as e:
                log.error("Failed to fetch schedule for %s: %s", date_str, e)
                continue

            for g in games:
                if g["abstract_state"] != "Final":
                    continue
                if storage.is_game_posted(g["game_pk"]):
                    continue
                try:
                    box = mlb_api.get_boxscore(g["game_pk"])
                    starters = mlb_api.extract_starters(box)
                except Exception as e:
                    log.error("Failed to fetch/parse boxscore for game %s: %s", g["game_pk"], e)
                    continue

                storage.mark_game_posted(g["game_pk"])
                try:
                    await channel.send(embed=build_game_embed(g, starters))
                    log.info("Posted starter report for game %s", g["game_pk"])
                except Exception as e:
                    log.error("Failed to send starter report for game %s: %s", g["game_pk"], e)
    except Exception as e:
        # Top-level safety net: discord.py's task loop permanently stops on
        # any unhandled exception with no automatic restart. This guarantees
        # that can never happen here -- worst case, this one cycle is
        # skipped and logged, but the loop itself keeps running forever.
        log.error("poll_games cycle failed unexpectedly, will retry next cycle: %s", e)


@poll_games.before_loop
async def before_poll():
    await client.wait_until_ready()


@tasks.loop(hours=ROSTER_REFRESH_HOURS)
async def refresh_directory_loop(bot: StartersBot):
    try:
        await bot.refresh_player_directory()
    except Exception as e:
        log.error("refresh_directory_loop cycle failed unexpectedly, will retry next cycle: %s", e)


@refresh_directory_loop.before_loop
async def before_refresh():
    await client.wait_until_ready()


@tasks.loop(minutes=2)
async def watchdog():
    """
    Belt-and-suspenders: if either background loop somehow stops for any
    reason not already caught above, this notices within 2 minutes and
    restarts it -- rather than the bot going silently dark for the rest
    of the day with no automatic recovery, which is what happened before
    this was added.
    """
    if not poll_games.is_running():
        log.error("poll_games was found stopped -- restarting it now")
        poll_games.start(client)
    if not refresh_directory_loop.is_running():
        log.error("refresh_directory_loop was found stopped -- restarting it now")
        refresh_directory_loop.start(client)
    if not scheduled_starters_post.is_running():
        log.error("scheduled_starters_post was found stopped -- restarting it now")
        scheduled_starters_post.start(client)


@watchdog.before_loop
async def before_watchdog():
    await client.wait_until_ready()


# 11 PM ET the night before, and 11 AM ET the day of -- approximated as
# UTC-4 (matches the rest of this bot's ET handling; will drift by an hour
# during EST in the off-season, same known limitation as elsewhere here).
# 11 PM ET = 03:00 UTC (next day). 11 AM ET = 15:00 UTC.
SCHEDULED_TIMES = [dtime(hour=3, minute=0), dtime(hour=15, minute=0)]


@tasks.loop(time=SCHEDULED_TIMES)
async def scheduled_starters_post(bot: StartersBot):
    try:
        channel_id = storage.get_config("announce_channel_id")
        if not channel_id:
            return
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            return

        now_utc = datetime.now(timezone.utc)
        if now_utc.hour < 6:
            # This is the 02:00 UTC run (10 PM ET the night before) -- post TOMORROW's slate
            date_str = et_date_str(1)
            label = "Tomorrow's"
        else:
            # This is the 15:00 UTC run (11 AM ET day-of) -- post TODAY's slate
            date_str = et_date_str(0)
            label = "Today's"

        try:
            lines = await asyncio.to_thread(bot._build_starters_lines_sync, date_str)
        except Exception as e:
            log.error("Scheduled starters post failed to fetch data for %s: %s", date_str, e)
            return

        header = f"__**{label} Probable Starters — {date_str}**__\n\n"
        chunk = header
        for line in lines:
            if len(chunk) + len(line) > 1900:
                await channel.send(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            await channel.send(chunk)
        log.info("Posted scheduled starters report for %s", date_str)
    except Exception as e:
        log.error("scheduled_starters_post cycle failed unexpectedly, will retry next scheduled time: %s", e)


@scheduled_starters_post.before_loop
async def before_scheduled_starters_post():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file (see .env.example).")
    client.run(TOKEN)
