import os
import logging
from datetime import datetime, timedelta, timezone

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


def build_lookup_embed(pitcher_name: str, splits: list[dict]) -> discord.Embed:
    if not splits:
        return discord.Embed(
            title=pitcher_name,
            description="No game log found for this season yet.",
            color=discord.Color.light_grey(),
        )
    last = splits[-1]
    tag_text = "Start" if last["is_start"] else "Relief appearance"

    last5 = stats.summarize_outings(splits, 5)
    last3 = stats.summarize_outings(splits, 3)
    hot_cold = stats.hot_cold_tag(last5)

    title = f"{pitcher_name} — Last Outing"
    if hot_cold:
        title = f"{title}  {hot_cold}"

    embed = discord.Embed(
        title=title,
        description=(
            f"{last['date']} vs {last['opponent']} ({tag_text})\n\n"
            f"**{last['pitches']} pitches** • {last['ip']} IP\n"
            f"{last['hits']}H {last['er']}ER {last['bb']}BB {last['so']}K"
        ),
        color=discord.Color.blue(),
    )

    def window_field(summary: dict | None, label: str):
        if not summary:
            return
        embed.add_field(
            name=label,
            value=(
                f"ERA: **{summary['era']}** • K/9: **{summary['k9']}** • WHIP: {summary['whip']}\n"
                f"{summary['total_ip']} IP over {summary['count']} starts, avg {summary['avg_pitches']} pitches"
            ),
            inline=False,
        )

    window_field(last3, "Last 3 Starts")
    window_field(last5, "Last 5 Starts")

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
        storage.init_db()
        try:
            self.teams = mlb_api.get_all_teams()
        except Exception as e:
            log.error("Failed to fetch team list at startup: %s", e)
            self.teams = []
        await self.refresh_player_directory()

        pitchcount_cmd = app_commands.Command(
            name="pitchcount",
            description="Show a pitcher's most recent outing",
            callback=self._pitchcount_callback,
        )
        self.tree.add_command(pitchcount_cmd)
        pitchcount_cmd.autocomplete("name")(self._name_autocomplete)

        setchannel_cmd = app_commands.Command(
            name="setchannel",
            description="Set this channel to receive starter pitch count reports",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        starters_cmd = app_commands.Command(
            name="starters",
            description="Probable starters for a date (YYYY-MM-DD, blank = today), with last pitch count",
            callback=self._starters_callback,
        )
        self.tree.add_command(starters_cmd)

        try:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    async def refresh_player_directory(self):
        try:
            teams = mlb_api.get_all_teams()
        except Exception as e:
            log.error("Failed to fetch teams for directory: %s", e)
            return
        directory = []
        for team in teams:
            try:
                pitchers = mlb_api.get_active_roster_pitchers(team["id"])
            except Exception as e:
                log.error("Failed to fetch roster for team %s: %s", team["id"], e)
                continue
            for p in pitchers:
                directory.append({"id": p["id"], "name": p["name"], "team": team["abbreviation"]})
        self.player_directory = directory
        log.info("Player directory refreshed: %d pitchers", len(directory))

    async def _name_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        matches = [p for p in self.player_directory if current_lower in p["name"].lower()]
        matches = matches[:25]
        return [
            app_commands.Choice(name=f"{p['name']} ({p['team']})", value=str(p["id"]))
            for p in matches
        ]

    async def _pitchcount_callback(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer()

        # `name` is the person_id if picked from autocomplete; fall back to a
        # substring name search if the person typed free text and hit enter.
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

        if person_id is None:
            await interaction.followup.send(
                f"Couldn't find a pitcher matching '{name}' on an active roster. "
                f"Try selecting from the suggestions as you type."
            )
            return

        try:
            splits = mlb_api.get_pitcher_game_log(person_id)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return

        await interaction.followup.send(embed=build_lookup_embed(pitcher_name, splits))

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(
            f"✅ Starter pitch count reports will post in {interaction.channel.mention}."
        )

    async def _starters_callback(self, interaction: discord.Interaction, date: str | None = None):
        await interaction.response.defer()
        date_str = date or et_date_str(0)

        try:
            entries = mlb_api.get_probable_starters(date_str)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the MLB API right now: {e}")
            return

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
            tag_str = ""
            try:
                splits = mlb_api.get_pitcher_game_log(entry["pitcher_id"])
                starts = [s for s in splits if s["is_start"]]
                if starts:
                    last = starts[-1]
                    rest_days = (
                        datetime.strptime(date_str, "%Y-%m-%d") - datetime.strptime(last["date"], "%Y-%m-%d")
                    ).days - 1
                    rest_str = f", {rest_days} days rest" if rest_days >= 0 else ""
                    last_pitch_line = f"Threw {last['pitches']} pitches in his last start ({last['date']}{rest_str})"

                    last5 = stats.summarize_outings(splits, 5)
                    tag = stats.hot_cold_tag(last5)
                    if tag and last5:
                        tag_str = f" {tag} ({last5['era']} ERA last {last5['count']})"
            except Exception as e:
                log.error("Game log lookup failed for %s: %s", entry["pitcher_name"], e)

            lines.append(f"**{team['name']}**\n{entry['pitcher_name']} makes the start.{tag_str} {last_pitch_line}.\n")

        header = f"__**Probable Starters — {date_str}**__\n\n"
        await self._send_chunked(interaction, header, lines)

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


client = StartersBot()


@tasks.loop(minutes=POLL_MINUTES)
async def poll_games(bot: StartersBot):
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


@poll_games.before_loop
async def before_poll():
    await client.wait_until_ready()


@tasks.loop(hours=ROSTER_REFRESH_HOURS)
async def refresh_directory_loop(bot: StartersBot):
    await bot.refresh_player_directory()


@refresh_directory_loop.before_loop
async def before_refresh():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file (see .env.example).")
    client.run(TOKEN)
