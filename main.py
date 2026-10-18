
import os
import json
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

GUILD_ID = 1541574758641049670
PURGATORY_ROLE_ID = 1555374207720755333
LOG_CHANNEL_ID = 1561156843156275351
DATA_FILE = "purgatory.json"

# Role names from your screenshot.
# Replace with role IDs later for stronger security.
STAFF_NAMES = {
    "server managers",
    "server manager assistant's",
    ".bot",
    "duck secret",
    "senior moderator",
    "head purgatory moderator",
    "bots",
    "akuhple",
    "moderator",
    "purgatory moderators",
}

DEEP_NAMES = {
    "server managers",
    "senior moderator",
    "head purgatory moderator",
}

BULK_NAMES = {
    "server managers",
    "head purgatory moderator",
}

IMMUNE_NAMES = STAFF_NAMES.copy()

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
data_lock = asyncio.Lock()


def load_data():
    try:
        with open(DATA_FILE, "r") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}


def save_data(data):
    temp = DATA_FILE + ".tmp"
    with open(temp, "w") as file:
        json.dump(data, file, indent=2)
    os.replace(temp, DATA_FILE)


def member_key(member):
    return f"{member.guild.id}:{member.id}"


def has_role(member, names):
    return any(role.name.lower() in names for role in member.roles)


def allowed(member, level="staff"):
    if member.id == member.guild.owner_id:
        return True

    names = {
        "staff": STAFF_NAMES,
        "deep": DEEP_NAMES,
        "bulk": BULK_NAMES
    }[level]

    return has_role(member, names)


def can_manage(actor, target, bot_member):
    guild = target.guild

    if target.id == guild.owner_id:
        return False

    if target.id == bot_member.id:
        return False

    if target.top_role >= bot_member.top_role:
        return False

    if actor.id != guild.owner_id:
        if target.top_role >= actor.top_role:
            return False

    return True


async def respond(interaction, message):
    if interaction.response.is_done():
        await interaction.followup.send(
            message, ephemeral=True
        )
    else:
        await interaction.response.send_message(
            message, ephemeral=True
        )


async def announce(guild, message, moderator, target, action):
    channel = guild.get_channel(LOG_CHANNEL_ID)

    if channel:
        embed = discord.Embed(
            title=f"🔥 Purgatory Logs | {action}",
            description=message,
            color=discord.Color.red()
        )
        embed.add_field(
            name="Member",
            value=target.mention
        )
        embed.add_field(
            name="Moderator",
            value=moderator.mention
        )
        embed.timestamp = datetime.now(timezone.utc)

        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


async def send_public(interaction, message):
    try:
        await interaction.channel.send(message)
    except discord.HTTPException:
        pass


async def put_in_purgatory(member, moderator, deep=False):
    guild = member.guild
    role = guild.get_role(PURGATORY_ROLE_ID)
    bot_member = guild.me

    if role is None:
        return False, "Purgatory role not found."

    if role >= bot_member.top_role:
        return False, "Move my bot role above Purgatory."

    if not can_manage(moderator, member, bot_member):
        return False, "Role hierarchy prevents this punishment."

    if not deep and has_role(member, IMMUNE_NAMES):
        return False, "This member has an immune role."

    async with data_lock:
        data = load_data()
        key = member_key(member)

        if key in data or role in member.roles:
            return False, "Member is already in Purgatory."

        original_roles = [
            r.id for r in member.roles
            if r != guild.default_role
        ]

        # Discord-managed roles cannot be manually removed.
        managed_roles = [
            r for r in member.roles
            if r.managed
        ]

        new_roles = managed_roles + [role]

        expires = None
        if deep:
            expires = (
                datetime.now(timezone.utc)
                + timedelta(minutes=2)
            ).isoformat()

        try:
            await member.edit(
                roles=new_roles,
                reason=f"Purgatory by {moderator}"
            )

            if deep:
                await member.timeout(
                    timedelta(minutes=2),
                    reason=f"Deepest Pits by {moderator}"
                )

        except discord.HTTPException as error:
            return False, f"Discord error: {error}"

        data[key] = {
            "roles": original_roles,
            "deep": deep,
            "expires": expires
        }
        save_data(data)

    return True, "Success"


async def release_member(member, moderator=None):
    guild = member.guild
    bot_member = guild.me

    async with data_lock:
        data = load_data()
        key = member_key(member)

        if key not in data:
            return False, "No saved roles found."

        if member.top_role >= bot_member.top_role:
            return False, "My bot role is too low."

        record = data[key]
        restored = []

        for role_id in record["roles"]:
            role = guild.get_role(role_id)

            if role and not role.managed:
                if role >= bot_member.top_role:
                    return False, "A saved role is too high."
                restored.append(role)

        # Keep current managed roles.
        restored += [
            r for r in member.roles
            if r.managed
        ]

        try:
            await member.edit(
                roles=restored,
                reason="Released from Purgatory"
            )

            if record.get("deep"):
                await member.timeout(
                    None,
                    reason="Released from Deepest Pits"
                )

        except discord.HTTPException as error:
            return False, f"Discord error: {error}"

        del data[key]
        save_data(data)

    return True, "Success"


@bot.tree.command(
    name="purgatory",
    description="Send a member to Purgatory"
)
async def purgatory(
    interaction: discord.Interaction,
    member: discord.Member
):
    if not allowed(interaction.user):
        return await respond(
            interaction, "You cannot use this command."
        )

    await interaction.response.defer(ephemeral=True)

    success, result = await put_in_purgatory(
        member, interaction.user
    )

    if not success:
        return await respond(interaction, result)

    message = (
        f"🔥 {member.mention} has been sent to "
        "the deepest pits of hell — Purgatory."
    )

    await send_public(interaction, message)
    await announce(
        interaction.guild, message,
        interaction.user, member, "Purgatory"
    )
    await respond(interaction, "Member sent to Purgatory.")


@bot.tree.command(
    name="unpurg",
    description="Release a member from Purgatory"
)
async def unpurg(
    interaction: discord.Interaction,
    member: discord.Member
):
    if not allowed(interaction.user):
        return await respond(
            interaction, "You cannot use this command."
        )

    await interaction.response.defer(ephemeral=True)

    if not can_manage(
        interaction.user, member, interaction.guild.me
    ):
        return await respond(
            interaction, "Role hierarchy prevents this."
        )

    success, result = await release_member(
        member, interaction.user
    )

    if not success:
        return await respond(interaction, result)

    message = (
        f"🔓 {member.mention} has escaped "
        "the deepest pits of hell — Purgatory."
    )

    await send_public(interaction, message)
    await announce(
        interaction.guild, message,
        interaction.user, member, "Unpurgatory"
    )
    await respond(interaction, "Member released.")


@bot.tree.command(
    name="deepestpits",
    description="Override immunity and punish for 2 minutes"
)
async def deepestpits(
    interaction: discord.Interaction,
    member: discord.Member
):
    if not allowed(interaction.user, "deep"):
        return await respond(
            interaction, "Only authorized senior staff can use this."
        )

    await interaction.response.defer(ephemeral=True)

    success, result = await put_in_purgatory(
        member, interaction.user, deep=True
    )

    if not success:
        return await respond(interaction, result)

    message = (
        f"🔥 **DEEPEST PITS OF HELL**\n"
        f"{member.mention} has been condemned "
        "to Purgatory for **2 minutes**."
    )

    await send_public(interaction, message)
    await announce(
        interaction.guild, message,
        interaction.user, member, "Deepest Pits"
    )
    await respond(interaction, "Deepest Pits activated.")


@bot.tree.command(
    name="purgatoryrole",
    description="Purgatory everyone with a selected role"
)
async def purgatoryrole(
    interaction: discord.Interaction,
    role: discord.Role
):
    if not allowed(interaction.user, "bulk"):
        return await respond(
            interaction, "Only Head Purgatory Mods and Server Managers."
        )

    await interaction.response.defer(ephemeral=True)

    success_count = 0
    skipped = 0

    for member in list(role.members):
        success, _ = await put_in_purgatory(
            member, interaction.user
        )

        if success:
            success_count += 1
            message = (
                f"🔥 {member.mention} has been sent to "
                "the deepest pits of hell — Purgatory."
            )
            await send_public(interaction, message)
            await announce(
                interaction.guild, message,
                interaction.user, member, "Role Purgatory"
            )
        else:
            skipped += 1

    await respond(
        interaction,
        f"Sent {success_count} members to Purgatory. "
        f"Skipped {skipped}."
    )


@bot.tree.command(
    name="unpurgatoryrole",
    description="Release members who originally had a role"
)
async def unpurgatoryrole(
    interaction: discord.Interaction,
    role: discord.Role
):
    if not allowed(interaction.user, "bulk"):
        return await respond(
            interaction, "Only Head Purgatory Mods and Server Managers."
        )

    await interaction.response.defer(ephemeral=True)

    data = load_data()
    guild = interaction.guild
    released = 0

    for member in list(guild.members):
        record = data.get(member_key(member))

        if not record:
            continue

        if role.id not in record["roles"]:
            continue

        if not can_manage(
            interaction.user, member, guild.me
        ):
            continue

        success, _ = await release_member(
            member, interaction.user
        )

        if success:
            released += 1
            message = (
                f"🔓 {member.mention} has escaped "
                "the deepest pits of hell — Purgatory."
            )
            await send_public(interaction, message)
            await announce(
                guild, message, interaction.user,
                member, "Role Unpurgatory"
            )

    await respond(
        interaction,
        f"Released {released} members."
    )


class PageSelect(discord.ui.Select):
    def __init__(self, members, page=0):
        self.members = members
        self.page = page
        pages = max(1, (len(members) + 9) // 10)

        options = [
            discord.SelectOption(
                label=f"Page {i + 1}",
                value=str(i),
                default=(i == page)
            )
            for i in range(pages)
        ]

        super().__init__(
            placeholder=f"Page {page + 1}",
            options=options[:25]
        )

    async def callback(self, interaction):
        page = int(self.values[0])
        self.view.page = page
        embed = self.view.make_embed()
        self.view.refresh()
        await interaction.response.edit_message(
            embed=embed, view=self.view
        )


class PurgatoryPages(discord.ui.View):
    def __init__(self, members, owner_id):
        super().__init__(timeout=180)
        self.members = members
        self.owner_id = owner_id
        self.page = 0
        self.refresh()

    def refresh(self):
        self.clear_items()
        if len(self.members) > 10:
            self.add_item(PageSelect(
                self.members, self.page
            ))

    def make_embed(self):
        start = self.page * 10
        selected = self.members[start:start + 10]

        description = "\n".join(
            f"**{start + i + 1}.** {member.mention}"
            for i, member in enumerate(selected)
        ) or "Nobody is in Purgatory."

        embed = discord.Embed(
            title="🔥 Purgatory Members",
            description=description,
            color=discord.Color.red()
        )

        total_pages = max(
            1, (len(self.members) + 9) // 10
        )

        embed.set_footer(
            text=f"Page {self.page + 1}/{total_pages} "
                 f"• {len(self.members)} members"
        )
        return embed

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the command user can change pages.",
                ephemeral=True
            )
            return False
        return True


@bot.tree.command(
    name="viewpurgatory",
    description="View Purgatory members with pages"
)
async def viewpurgatory(interaction: discord.Interaction):
    if not allowed(interaction.user):
        return await respond(
            interaction, "You cannot use this command."
        )

    role = interaction.guild.get_role(PURGATORY_ROLE_ID)

    if role is None:
        return await respond(
            interaction, "Purgatory role not found."
        )

    members = list(role.members)
    view = PurgatoryPages(
        members, interaction.user.id
    )

    await interaction.response.send_message(
        embed=view.make_embed(),
        view=view,
        ephemeral=True
    )


@tasks.loop(seconds=15)
async def automatic_release():
    data = load_data()
    now = datetime.now(timezone.utc)

    for key, record in list(data.items()):
        expires = record.get("expires")

        if not expires:
            continue

        if datetime.fromisoformat(expires) > now:
            continue

        guild_id, user_id = map(int, key.split(":"))
        guild = bot.get_guild(guild_id)

        if not guild:
            continue

        member = guild.get_member(user_id)

        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                continue

        success, _ = await release_member(member)

        if success:
            channel = guild.get_channel(LOG_CHANNEL_ID)
            if channel:
                await channel.send(
                    f"🔓 {member.mention} has automatically "
                    "escaped the deepest pits of hell."
                )


@automatic_release.before_loop
async def before_release():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"✅ Online as {bot.user}")

    if not automatic_release.is_running():
        automatic_release.start()

    if not getattr(bot, "synced", False):
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        bot.synced = True
        print("Slash commands synced.")


token = os.environ.get("DISCORD_TOKEN")

if not token:
    raise RuntimeError(
        "Set DISCORD_TOKEN in your hosting environment."
    )

bot.run(token)
