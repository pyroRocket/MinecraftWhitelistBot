import json
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from mcrcon import MCRcon
import aiohttp

# --- Load config.json ---
with open("config.json", "r") as f:
    CONFIG = json.load(f)

TOKEN = CONFIG["DISCORD_TOKEN"]
GUILD_ID = int(CONFIG["GUILD_ID"])
ALLOWED_ROLE_IDS = set(int(r) for r in CONFIG["ALLOWED_ROLE_IDS"])
ADMIN_ROLE_IDS = set(int(r) for r in CONFIG.get("ADMIN_ROLE_IDS", []))

RCON_HOST = CONFIG["RCON_HOST"]
RCON_PORT = int(CONFIG["RCON_PORT"])
RCON_PASSWORD = CONFIG["RCON_PASSWORD"]

LINKS_FILE = "links.json"
links = {}


# --- JSON persistence for links ---
def load_links():
    global links
    try:
        with open(LINKS_FILE, "r") as f:
            links = {int(k): tuple(v) for k, v in json.load(f).items()}
        print(f"[DEBUG] Loaded {len(links)} linked accounts from {LINKS_FILE}")
    except FileNotFoundError:
        print(f"[DEBUG] No {LINKS_FILE} found, starting empty.")
        links = {}
    except Exception as e:
        print(f"[ERROR] Failed to load {LINKS_FILE}: {e}")
        links = {}


def save_links():
    try:
        with open(LINKS_FILE, "w") as f:
            json.dump({str(k): v for k, v in links.items()}, f, indent=2)
        print(f"[DEBUG] Saved {len(links)} links to {LINKS_FILE}")
    except Exception as e:
        print(f"[ERROR] Failed to save {LINKS_FILE}: {e}")


# --- Discord setup ---
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# --- Helpers ---
async def mojang_resolve(name: str):
    url = f"https://api.mojang.com/users/profiles/minecraft/{name}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            print(f"[DEBUG] Mojang API GET {url} -> {resp.status}")
            if resp.status != 200:
                raise ValueError("Name not found")
            data = await resp.json()
            raw = data["id"]
            dashed = f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
            return data["name"], dashed


async def rcon_command(*commands):
    results = []
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, port=RCON_PORT) as mcr:
            for c in commands:
                print(f"[DEBUG] RCON -> {c}")
                results.append(mcr.command(c))
    except Exception as e:
        print(f"[ERROR] RCON command failed: {e}")
    return results


async def whitelist_add(name: str):
    print(f"[DEBUG] Adding {name} to whitelist")
    await rcon_command(f"whitelist add {name}", "whitelist reload")

async def whitelist_remove(name: str):
    print(f"[DEBUG] Removing {name} from whitelist")
    await rcon_command(f"whitelist remove {name}", "whitelist reload")


def has_allowed_role(member: discord.Member):
    has_role = any(r.id in ALLOWED_ROLE_IDS for r in member.roles)
    print(f"[DEBUG] Checking allowed role for {member} -> {has_role}")
    return has_role


def has_admin_role(member: discord.Member):
    has_role = any(r.id in ADMIN_ROLE_IDS for r in member.roles)
    print(f"[DEBUG] Checking admin role for {member} -> {has_role} (roles={ [r.id for r in member.roles] })")
    return has_role


# --- Commands ---
@tree.command(description="Link your Discord to a Minecraft username")
async def mc_link(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        mc_name, mc_uuid = await mojang_resolve(name)
        links[interaction.user.id] = (mc_name, mc_uuid)
        save_links()

        if has_allowed_role(interaction.user):
            await whitelist_add(mc_name)
            await interaction.followup.send(f"Linked to **{mc_name}** and whitelisted.", ephemeral=True)
        else:
            await interaction.followup.send(f"Linked to **{mc_name}**. You’ll be whitelisted once you have the role.", ephemeral=True)
    except Exception as e:
        print(f"[ERROR] link command failed: {e}")
        await interaction.followup.send("That is not a valid Minecraft username. Please try again.", ephemeral=True)


@tree.command(description="Unlink your Minecraft account")
async def mc_unlink(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if interaction.user.id in links:
        mc_name, _ = links.pop(interaction.user.id)
        save_links()
        await whitelist_remove(mc_name)
        await interaction.followup.send(f"Unlinked and removed **{mc_name}** from whitelist.", ephemeral=True)
    else:
        await interaction.followup.send("You don’t have a link set.", ephemeral=True)


@tree.command(description="Resync MC whitelist (admin only)")
async def mc_sync(interaction: discord.Interaction):
    print(f"[DEBUG] /mc_sync called by {interaction.user} (ID={interaction.user.id})")

    if not interaction.guild:
        return await interaction.response.send_message("This command must be used inside a server, not in DMs.", ephemeral=True)

    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception as e:
            print(f"[DEBUG] fetch_member failed: {e}")
            return await interaction.response.send_message("Could not fetch your member info.", ephemeral=True)

    print(f"[DEBUG] Roles for {member}: {[r.id for r in member.roles]}")
    if not has_admin_role(member):
        return await interaction.response.send_message("Admins only.", ephemeral=True)

    await interaction.response.defer(ephemeral=True, thinking=True)

    added, removed, failed = 0, 0, 0
    for discord_id, (mc_name, _) in links.items():
        m = interaction.guild.get_member(discord_id)
        if not m:
            try:
                m = await interaction.guild.fetch_member(discord_id)
            except Exception:
                m = None

        print(f"[DEBUG] Syncing {mc_name} for {discord_id} -> {m}")

        if m and has_allowed_role(m):
            try:
                await whitelist_add(mc_name)
                added += 1
            except Exception as e:
                print(f"[SYNC ERROR] Could not add {mc_name}: {e}")
                failed += 1
        else:
            try:
                await whitelist_remove(mc_name)
                removed += 1
            except Exception as e:
                print(f"[SYNC ERROR] Could not remove {mc_name}: {e}")
                failed += 1

    await interaction.followup.send(
        f"✅ Sync complete:\n"
        f"➕ Added: {added}\n"
        f"➖ Removed: {removed}\n"
        f"⚠️ Failed: {failed}\n"
        f"📦 Total links: {len(links)}",
        ephemeral=True
    )


# --- Role monitor ---
@bot.event
async def on_member_update(before, after):
    before_has = has_allowed_role(before)
    after_has = has_allowed_role(after)
    if before_has == after_has:
        return

    if after.id in links:
        mc_name, _ = links[after.id]
        if after_has:
            print(f"[DEBUG] {after} gained role, adding {mc_name}")
            await whitelist_add(mc_name)
        else:
            print(f"[DEBUG] {after} lost role, removing {mc_name}")
            await whitelist_remove(mc_name)


# --- Startup ---
@bot.event
async def on_ready():
    load_links()
    guild = discord.Object(id=GUILD_ID)
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception as e:
        print(f"[ERROR] Command sync failed: {e}")
        await bot.tree.sync()
    print(f"[DEBUG] Logged in as {bot.user} (ID={bot.user.id})")
    print(f"[DEBUG] ALLOWED_ROLE_IDS: {ALLOWED_ROLE_IDS}")
    print(f"[DEBUG] ADMIN_ROLE_IDS: {ADMIN_ROLE_IDS}")


bot.run(TOKEN)
