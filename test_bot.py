import os
import discord
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

print("STEP 1: Script started, about to create client", flush=True)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"STEP 2: SUCCESS - Logged in as {client.user}", flush=True)

print("STEP 3: About to call client.run()", flush=True)

if not TOKEN:
    print("ERROR: DISCORD_TOKEN is not set!", flush=True)
    raise SystemExit("Set DISCORD_TOKEN")

client.run(TOKEN)
