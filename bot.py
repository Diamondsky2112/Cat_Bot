import asyncio
import os
import random
import sqlite3
import time

import discord
import praw
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DEFAULT_INTERVAL = int(os.getenv("DEFAULT_INTERVAL_MINUTES", "30"))
MIN_INTERVAL = 5
MAX_INTERVAL = 1440
DB_PATH = os.getenv("DB_PATH", "katzenbot.db")
CACHE_TTL = 300  # Sekunden: Reddit-Ergebnisse werden für alle Server geteilt

reddit = praw.Reddit(
    client_id=os.environ["REDDIT_CLIENT_ID"],
    client_secret=os.environ["REDDIT_CLIENT_SECRET"],
    user_agent=os.getenv("REDDIT_USER_AGENT", "katzen-bot/1.0 (by u/DEIN_USERNAME)"),
)

# --- Filter ----------------------------------------------------------------
# Entspricht dem "ohne Mourning/Loss"-Button auf r/cats
SEARCH_QUERY = 'NOT flair:"Mourning/Loss"'

# Zweite Sicherheitsebene, falls die Reddit-Suche das Flair mal nicht ausfiltert
BLOCKED_FLAIRS = ["mourning", "loss", "rainbow bridge", "memorial"]
BLOCKED_KEYWORDS = [
    "rainbow bridge", "rainbowbridge", "r.i.p", "passed away", "died", "lost my",
    "miss you", "goodbye", "farewell", "put down", "put to sleep", "euthan",
    "in memory", "in loving memory", "grieving", "mourning", "crossed the bridge",
    "gone too soon", "verstorben", "gestorben", "eingeschläfert",
    "regenbogenbrücke", "trauer",
]
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def is_mourning_post(post) -> bool:
    flair = (post.link_flair_text or "").lower()
    if any(f in flair for f in BLOCKED_FLAIRS):
        return True
    text = f"{post.title} {post.selftext or ''}".lower()
    return any(k in text for k in BLOCKED_KEYWORDS)


def is_image_post(post) -> bool:
    if post.stickied or post.is_self or post.is_video or post.over_18:
        return False
    if getattr(post, "is_gallery", False):
        return False
    return post.url.lower().split("?")[0].endswith(IMAGE_EXTENSIONS)


# --- Datenbank (pro Server: Channel, Intervall, bereits gepostete Bilder) ---
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS guilds (
        guild_id     INTEGER PRIMARY KEY,
        channel_id   INTEGER NOT NULL,
        interval_min INTEGER NOT NULL,
        next_run     REAL    NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS seen (
        guild_id INTEGER NOT NULL,
        post_id  TEXT    NOT NULL,
        ts       REAL    NOT NULL,
        PRIMARY KEY (guild_id, post_id)
    );
    """
)
db.commit()


def seen_ids(guild_id: int) -> set[str]:
    rows = db.execute("SELECT post_id FROM seen WHERE guild_id=?", (guild_id,))
    return {r["post_id"] for r in rows}


def mark_seen(guild_id: int, post_id: str) -> None:
    db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)", (guild_id, post_id, time.time()))
    db.execute(
        "DELETE FROM seen WHERE guild_id=? AND post_id NOT IN "
        "(SELECT post_id FROM seen WHERE guild_id=? ORDER BY ts DESC LIMIT 1000)",
        (guild_id, guild_id),
    )
    db.commit()


# --- Reddit ----------------------------------------------------------------
_cache = {"ts": 0.0, "posts": []}
_cache_lock = asyncio.Lock()


def _fetch_candidates() -> list[dict]:
    """Blockierend -> läuft in einem Thread."""
    sub = reddit.subreddit("cats")
    posts = list(sub.search(SEARCH_QUERY, sort="new", time_filter="all", limit=100))
    if not posts:  # Fallback, falls die Suche nichts liefert
        posts = list(sub.new(limit=100))
    return [
        {
            "id": p.id,
            "title": p.title,
            "url": p.url,
            "permalink": p.permalink,
            "author": str(p.author) if p.author else "[deleted]",
            "score": p.score,
        }
        for p in posts
        if is_image_post(p) and not is_mourning_post(p)
    ]


async def get_candidates() -> list[dict]:
    async with _cache_lock:
        if not _cache["posts"] or time.time() - _cache["ts"] > CACHE_TTL:
            _cache["posts"] = await asyncio.to_thread(_fetch_candidates)
            _cache["ts"] = time.time()
        return _cache["posts"]


async def pick_post(guild_id: int) -> dict | None:
    candidates = await get_candidates()
    seen = seen_ids(guild_id)
    fresh = [p for p in candidates if p["id"] not in seen]
    return random.choice(fresh) if fresh else None


def build_embed(post: dict) -> discord.Embed:
    embed = discord.Embed(
        title=post["title"][:256],
        url=f"https://reddit.com{post['permalink']}",
        color=0xF5A623,
    )
    embed.set_image(url=post["url"])
    embed.set_footer(text=f"r/cats • u/{post['author']} • ⬆ {post['score']}")
    return embed


# --- Bot -------------------------------------------------------------------
class KatzenBot(commands.Bot):
    async def setup_hook(self):
        await self.tree.sync()  # globale Slash-Commands registrieren
        scheduler.start()


bot = KatzenBot(command_prefix=commands.when_mentioned, intents=discord.Intents.default())


@tasks.loop(minutes=1)
async def scheduler():
    now = time.time()
    rows = db.execute(
        "SELECT * FROM guilds WHERE enabled=1 AND next_run<=?", (now,)
    ).fetchall()
    for row in rows:
        gid = row["guild_id"]
        # next_run zuerst setzen, damit Fehler nicht jede Minute neu versucht werden
        db.execute(
            "UPDATE guilds SET next_run=? WHERE guild_id=?",
            (now + row["interval_min"] * 60, gid),
        )
        db.commit()

        channel = bot.get_channel(row["channel_id"])
        if channel is None:
            print(f"[{gid}] Channel nicht gefunden")
            continue
        try:
            post = await pick_post(gid)
            if post:
                await channel.send(embed=build_embed(post))
                mark_seen(gid, post["id"])
        except discord.Forbidden:
            print(f"[{gid}] Keine Rechte im Channel {row['channel_id']}")
        except Exception as e:
            print(f"[{gid}] Fehler: {e}")


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


@bot.tree.command(name="setup", description="Legt Channel und Intervall für Katzenbilder fest")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    channel="Channel, in dem die Katzenbilder gepostet werden",
    interval=f"Abstand in Minuten ({MIN_INTERVAL}–{MAX_INTERVAL}, Standard {DEFAULT_INTERVAL})",
)
async def setup_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    interval: app_commands.Range[int, MIN_INTERVAL, MAX_INTERVAL] = DEFAULT_INTERVAL,
):
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await interaction.response.send_message(
            f"Ich brauche in {channel.mention} die Rechte „Kanal ansehen“, "
            "„Nachrichten senden“ und „Links einbetten“.",
            ephemeral=True,
        )
        return

    db.execute(
        """
        INSERT INTO guilds (guild_id, channel_id, interval_min, next_run, enabled)
        VALUES (?,?,?,?,1)
        ON CONFLICT(guild_id) DO UPDATE SET
            channel_id=excluded.channel_id,
            interval_min=excluded.interval_min,
            next_run=excluded.next_run,
            enabled=1
        """,
        (interaction.guild_id, channel.id, interval, time.time()),
    )
    db.commit()
    await interaction.response.send_message(
        f"🐱 Alles klar! Ich poste alle {interval} Minuten Katzenbilder in {channel.mention}. "
        "Das erste kommt in Kürze."
    )


@bot.tree.command(name="stop", description="Stoppt die automatischen Katzenbilder")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def stop_cmd(interaction: discord.Interaction):
    db.execute("UPDATE guilds SET enabled=0 WHERE guild_id=?", (interaction.guild_id,))
    db.commit()
    await interaction.response.send_message("Automatische Katzenbilder gestoppt. 😿")


@bot.tree.command(name="status", description="Zeigt die aktuelle Konfiguration")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction):
    row = db.execute(
        "SELECT * FROM guilds WHERE guild_id=?", (interaction.guild_id,)
    ).fetchone()
    if row is None:
        await interaction.response.send_message(
            "Noch nicht eingerichtet. Ein Admin kann das mit `/setup` tun.", ephemeral=True
        )
        return
    state = "aktiv" if row["enabled"] else "gestoppt"
    await interaction.response.send_message(
        f"Status: **{state}** • Channel: <#{row['channel_id']}> • "
        f"Intervall: {row['interval_min']} Min.",
        ephemeral=True,
    )


@bot.tree.command(name="katze", description="Schickt sofort ein Katzenbild in diesen Channel")
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 10.0, key=lambda i: i.guild_id)
async def katze_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    post = await pick_post(interaction.guild_id)
    if post is None:
        await interaction.followup.send("Gerade keine neuen Katzenbilder gefunden 😿")
        return
    await interaction.followup.send(embed=build_embed(post))
    mark_seen(interaction.guild_id, post["id"])


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "Dafür brauchst du die Berechtigung „Server verwalten“."
    elif isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Bitte warte noch {error.retry_after:.0f} Sekunden."
    else:
        print(f"Command-Fehler: {error}")
        msg = "Da ist etwas schiefgelaufen. 😿"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


@bot.event
async def on_guild_remove(guild: discord.Guild):
    db.execute("DELETE FROM guilds WHERE guild_id=?", (guild.id,))
    db.execute("DELETE FROM seen WHERE guild_id=?", (guild.id,))
    db.commit()


@bot.event
async def on_ready():
    print(f"Eingeloggt als {bot.user} auf {len(bot.guilds)} Server(n)")


bot.run(DISCORD_TOKEN)
