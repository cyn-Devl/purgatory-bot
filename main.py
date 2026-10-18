
import os
import json
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

PURGATORY_ID = 1555374207720755333
DATA_FILE = "purgatory.json"

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
lock = asyncio.Lock()

def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_data(data):
    temp = DATA_FILE + ".tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, DATA_FILE)

def key(guild_id, user_id):
    return f"{guild_id}:{user_id}"

@bot.event
async def on_ready():
    if not getattr(bot, "synced", False):
        await bot.tree.sync()
        bot.synced = True
    print(f"Online as {bot.user}")

@bot.tree.command(
    name="purgatory",
    description="Place a member in purgatory"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.checks.has_permissions(manage_roles=True)
async def purgatory(
    interaction: discord.Interaction,
    member: discord.Member
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = guild.get_role(PURGATORY_ID)

    if role is None:
        return await interaction.followup.send(
            "Purgatory role not found."
        )

    if member.id == guild.owner_id or member.id == interaction.user.id:
        return await interaction.followup.send(
            "You cannot purgatory yourself or the server owner."
        )

    me = guild.me
    if (member.top_role >= me.top_role or
        role >= me.top_role or
        member.top_role >= interaction.user.top_role):
        return await interaction.followup.send(
            "Role hierarchy prevents this action."
        )

    async with lock:
        data = load_data()
        member_key = key(guild.id, member.id)

        if role in member.roles or member_key in data:
            return await interaction.followup.send(
                "This member is already in purgatory."
            )

        original = [
            r.id for r in member.roles
            if r != guild.default_role
        ]

        removable = [
            r for r in member.roles
            if r != guild.default_role and not r.managed
        ]

        try:
            await member.edit(
                roles=[role],
                reason=f"Purgatory by {interaction.user}"
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "I lack permission to modify this member's roles."
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"Discord error: {e}"
            )

        data[member_key] = original
        save_data(data)

    await interaction.followup.send(
        f"🔒 {member.mention} has been placed in purgatory."
    )

@bot.tree.command(
    name="unpurg",
    description="Release a member from purgatory"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.checks.has_permissions(manage_roles=True)
async def unpurg(
    interaction: discord.Interaction,
    member: discord.Member
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = guild.get_role(PURGATORY_ID)

    async with lock:
        data = load_data()
        member_key = key(guild.id, member.id)

        if member_key not in data:
            return await interaction.followup.send(
                "No saved roles found for this member."
            )

        me = guild.me
        if member.top_role >= me.top_role:
            return await interaction.followup.send(
                "My role is too low."
            )

        restored = []
        for role_id in data[member_key]:
            saved_role = guild.get_role(role_id)
            if saved_role and not saved_role.managed:
                if saved_role >= me.top_role:
                    return await interaction.followup.send(
                        "A saved role is above my highest role."
                    )
                restored.append(saved_role)

        try:
            await member.edit(
                roles=restored,
                reason=f"Released by {interaction.user}"
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "I lack permission to restore the roles."
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"Discord error: {e}"
            )

        del data[member_key]
        save_data(data)

    await interaction.followup.send(
        f"🔓 {member.mention} was released. Roles restored."
    )

@bot.tree.command(
    name="viewpurgatory",
    description="View all purgatory members"
)
@app_commands.default_permissions(manage_roles=True)
@app_commands.checks.has_permissions(manage_roles=True)
async def viewpurgatory(interaction: discord.Interaction):
    role = interaction.guild.get_role(PURGATORY_ID)

    if not role:
        return await interaction.response.send_message(
            "Purgatory role not found.", ephemeral=True
        )

    members = role.members
    if not members:
        return await interaction.response.send_message(
            "Purgatory is empty.", ephemeral=True
        )

    lines = [f"• {m.mention}" for m in members]
    description = "\n".join(lines[:30])

    embed = discord.Embed(
        title="🔒 Purgatory Members",
        description=description,
        color=discord.Color.red()
    )
    embed.add_field(
        name="Total Members",
        value=str(len(members))
    )
    if len(members) > 30:
        embed.set_footer(text="Showing first 30 members")

    await interaction.response.send_message(
        embed=embed, ephemeral=True
    )

token = os.environ.get("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN is missing")

bot.run(token)
