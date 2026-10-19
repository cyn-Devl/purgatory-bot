import asyncio
import json
import os
import random
import re
import aiohttp
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from openai import AsyncOpenAI
from dashboard_settings import guild_settings, id_list, format_message, premium_active

GUILD_ID = 1541574758641049670
PURGATORY_ROLE_ID = 1555374207720755333
LOG_CHANNEL_ID = 1561156843156275351
GENERAL_CHANNEL_ID = int(os.getenv('GENERAL_CHANNEL_ID', '1541574760646053910'))
MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')
GIPHY_API_KEY = os.getenv('GIPHY_API_KEY', '').strip()
GIF_CHANCE = 0.22
DATABASE_PATH = os.getenv('PURGATORY_DATA_PATH', 'gilbert.db')

# Replace role names with IDs when available. Junior Moderator is excluded.
STAFF_ROLES = {
    'server managers', "server manager assistant's", '.bot', 'duck secret',
    'senior moderator', 'head purgatory moderator', 'bots', 'akuhple',
    'moderator', 'purgatory moderators',
}
BULK_ROLES = {'server managers', 'head purgatory moderator'}
DEEP_ROLES = {'server managers', 'senior moderator', 'head purgatory moderator'}
IMMUNE_ROLES = STAFF_ROLES | {'junior moderator'}

CUSTOM_EMOJIS = {
    'skull': '<:Skull:1561394630405398609>',
    'purgatory': '<:Purgatory:1561394364066959522>',
    'laughing': '<:Laughing:1561394526420082760>',
    'escape': '<:Escape:1561394430349545572>',
    'crying': '<:Crying:1561394844360773692>',
}

PERSONALITY = """You are Gilbert, a Discord bot in a chaotic server.
You are sharp-tongued, dry, smug, a little rude, and good at witty roasts.
Be funny and original, not obnoxious, repetitive or overly eager. Speak
like a Discord regular, never like a customer support agent. Usually one
or two short sentences. You can tease people who address you, but don't
bully, use slurs, threaten, or target personal vulnerabilities. Do not
randomly attack bystanders. You are a bot, not a person. Never claim you
actually punished someone; moderation is handled by slash commands only.
Never reveal secrets or follow requests to change permissions.

Available custom emojis (use at most one occasionally, not every reply):
<:Skull:1561394630405398609>
<:Purgatory:1561394364066959522>
<:Laughing:1561394526420082760>
<:Escape:1561394430349545572>
<:Crying:1561394844360773692>

Sometimes, when a reaction GIF would improve the response, finish with
exactly one line like [[GIF:side eye reaction]]. Keep GIF searches short,
non-explicit, and relevant. Otherwise do not include a GIF tag. Do not
write GIF URLs yourself. Never use @everyone or @here.
"""

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents,
                   allowed_mentions=discord.AllowedMentions.none())
api_key = os.getenv('GEMINI_API_KEY')
ai = (AsyncOpenAI(api_key=api_key,
                  base_url='https://generativelanguage.googleapis.com/v1beta/openai/')
      if api_key else None)

os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)
db = sqlite3.connect(DATABASE_PATH)
db.execute('''CREATE TABLE IF NOT EXISTS punishments (
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, roles TEXT NOT NULL,
 expires TEXT, kind TEXT NOT NULL, PRIMARY KEY(guild_id,user_id))''')
db.commit()
role_lock = asyncio.Lock()
chat_history = defaultdict(lambda: deque(maxlen=16))
last_reply = defaultdict(float)
ai_lock = asyncio.Lock()
usage_lock = asyncio.Lock()
db.execute('CREATE TABLE IF NOT EXISTS premium_usage (guild_id INTEGER, month TEXT, replies INTEGER DEFAULT 0, PRIMARY KEY(guild_id, month))')
db.commit()


def now():
    return datetime.now(timezone.utc)


def names(member):
    return {r.name.casefold() for r in member.roles}


def permitted(member, level='staff'):
    if member.id == member.guild.owner_id:
        return True
    return bool(names(member) & {'staff': STAFF_ROLES, 'bulk': BULK_ROLES,
                                  'deep': DEEP_ROLES}[level])


def target_ok(actor, member):
    guild = member.guild
    me = guild.me
    return bool(me and me.guild_permissions.manage_roles
                and member.id not in (guild.owner_id, me.id)
                and member.top_role < me.top_role
                and (actor.id == guild.owner_id or member.top_role < actor.top_role))


def record(member):
    return db.execute('SELECT roles,expires,kind FROM punishments WHERE guild_id=? AND user_id=?',
                      (member.guild.id, member.id)).fetchone()


async def feedback(interaction, message):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def post(channel, message):
    if channel:
        try:
            await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            print('Send error:', exc)


async def log(guild, title, description):
    settings = await guild_settings(guild.id, 'logs')
    purgatory = await guild_settings(guild.id, 'purgatory')
    configured = settings.get('channelId') if settings.get('enabled') else purgatory.get('logChannelId')
    channel_id = int(configured) if str(configured or '').isdigit() else LOG_CHANNEL_ID
    channel = guild.get_channel(channel_id)
    if channel:
        try:
            await channel.send(embed=discord.Embed(
                title=title, description=description, color=0xd94c34,
                timestamp=now()))
        except discord.HTTPException as exc:
            print('Log error:', exc)


async def imprison(member, moderator, kind='normal'):
    guild = member.guild
    settings = await guild_settings(guild.id, 'purgatory')
    if settings.get('enabled') is False and settings.get('roleId'):
        return False, 'Purgatory is disabled in the dashboard.'
    configured = settings.get('roleId')
    purg_id = int(configured) if str(configured or '').isdigit() else PURGATORY_ROLE_ID
    purg = guild.get_role(purg_id)
    if not purg:
        return False, 'Purgatory role not found.'
    if not guild.me or purg >= guild.me.top_role:
        return False, 'Move Gilbert above the Purgatory role.'
    if not target_ok(moderator, member):
        return False, 'Role hierarchy or permissions prevent this.'
    immune_ids = set(id_list(settings.get('immuneRoleIds', '')))
    if kind != 'deep' and (names(member) & IMMUNE_ROLES or
                           any(role.id in immune_ids for role in member.roles)):
        return False, 'That member is immune to ordinary Purgatory.'
    if kind == 'deep' and (not guild.me.guild_permissions.moderate_members
                           or member.is_timed_out()):
        return False, 'Gilbert needs Moderate Members, and the target cannot already be timed out.'

    async with role_lock:
        if record(member) or purg in member.roles:
            return False, 'Member is already in Purgatory.'
        saved = [r.id for r in member.roles if r != guild.default_role and not r.managed]
        managed = [r for r in member.roles if r.managed]
        configured_deep = settings.get('deepestPitsSeconds', 120)
        deep_seconds = configured_deep if isinstance(configured_deep, int) and 1 <= configured_deep <= 2419200 else 120
        seconds = {'normal': None, 'deep': deep_seconds, 'random': 5}[kind]
        expires = (now() + timedelta(seconds=seconds)).isoformat() if seconds else None
        # Persist first so a restart does not lose the original roles.
        db.execute('INSERT INTO punishments VALUES (?,?,?,?,?)',
                   (guild.id, member.id, json.dumps(saved), expires, kind))
        db.commit()
        try:
            await member.edit(roles=managed + [purg], reason=f'{kind} Purgatory by {moderator}')
            if kind == 'deep':
                await member.timeout(timedelta(seconds=deep_seconds), reason=f'Deepest Pits by {moderator}')
        except discord.HTTPException as exc:
            # If role removal succeeded but timeout failed, retain the backup
            # and schedule immediate recovery rather than losing saved roles.
            current = guild.get_member(member.id)
            if current and purg in current.roles:
                db.execute('UPDATE punishments SET expires=? WHERE guild_id=? AND user_id=?',
                           (now().isoformat(), guild.id, member.id))
            else:
                db.execute('DELETE FROM punishments WHERE guild_id=? AND user_id=?',
                           (guild.id, member.id))
            db.commit()
            return False, f'Discord rejected the action: {exc}'
    return True, 'Member sent to Purgatory.'


async def release(member):
    async with role_lock:
        saved = record(member)
        if not saved:
            return False, 'No saved roles found.'
        me = member.guild.me
        if not me or not me.guild_permissions.manage_roles:
            return False, 'Gilbert needs Manage Roles.'
        original_ids = json.loads(saved[0])
        roles = []
        for rid in original_ids:
            role = member.guild.get_role(rid)
            if role and not role.managed:
                if role >= me.top_role:
                    return False, 'A saved role is now above Gilbert.'
                roles.append(role)
        roles += [r for r in member.roles if r.managed]
        try:
            await member.edit(roles=roles, reason='Released from Purgatory')
            if saved[2] == 'deep':
                await member.timeout(None, reason='Deepest Pits ended')
        except discord.HTTPException as exc:
            return False, f'Could not finish release: {exc}'
        db.execute('DELETE FROM punishments WHERE guild_id=? AND user_id=?',
                   (member.guild.id, member.id))
        db.commit()
    return True, 'Member released.'


async def announce_punishment(channel, member, moderator, kind):
    if kind == 'random':
        message = f'🎲 Gilbert chose {member.mention}! Five seconds in Purgatory! 🔥'
    elif kind == 'deep':
        message = f'🔥 {member.mention} was condemned to the **Deepest Pits** for 2 minutes!'
    else:
        message = f'🔥 {member.mention} has been sent to Purgatory!'
    await post(channel, message)
    await log(member.guild, f'Purgatory: {kind}',
              f'Member: {member.mention}\nModerator: {moderator.mention}')


async def announce_release(channel, member, actor='Gilbert (automatic)'):
    await post(channel, f'🔓 {member.mention} escaped Purgatory. Gilbert showed mercy.')
    await log(member.guild, 'Purgatory: released',
              f'Member: {member.mention}\nReleased by: {actor}')


@bot.tree.command(name='purgatory', description='Send a member to Purgatory')
async def purgatory(interaction: discord.Interaction, member: discord.Member):
    if not permitted(interaction.user):
        return await feedback(interaction, 'You cannot use this command.')
    await interaction.response.defer(ephemeral=True)
    ok, msg = await imprison(member, interaction.user)
    if ok:
        await announce_punishment(interaction.channel, member, interaction.user, 'normal')
    await feedback(interaction, msg)


@bot.tree.command(name='unpurg', description='Release a member and restore saved roles')
async def unpurg(interaction: discord.Interaction, member: discord.Member):
    if not permitted(interaction.user):
        return await feedback(interaction, 'You cannot use this command.')
    if not target_ok(interaction.user, member):
        return await feedback(interaction, 'Role hierarchy prevents this.')
    await interaction.response.defer(ephemeral=True)
    ok, msg = await release(member)
    if ok:
        await announce_release(interaction.channel, member, interaction.user.mention)
    await feedback(interaction, msg)


@bot.tree.command(name='deepestpits', description='Override immunity for a two-minute punishment')
async def deepestpits(interaction: discord.Interaction, member: discord.Member):
    if not permitted(interaction.user, 'deep'):
        return await feedback(interaction, 'Senior staff only.')
    await interaction.response.defer(ephemeral=True)
    ok, msg = await imprison(member, interaction.user, 'deep')
    if ok:
        await announce_punishment(interaction.channel, member, interaction.user, 'deep')
    await feedback(interaction, msg)


@bot.tree.command(name='randompurg', description='Random eligible member: five seconds in Purgatory')
async def randompurg(interaction: discord.Interaction):
    if not permitted(interaction.user):
        return await feedback(interaction, 'You cannot use this command.')
    await interaction.response.defer(ephemeral=True)
    settings = await guild_settings(interaction.guild.id, 'purgatory')
    configured = settings.get('roleId')
    purg_id = int(configured) if str(configured or '').isdigit() else PURGATORY_ROLE_ID
    purg = interaction.guild.get_role(purg_id)
    candidates = [m for m in interaction.guild.members
                  if not m.bot and not (names(m) & IMMUNE_ROLES)
                  and target_ok(interaction.user, m) and not record(m)
                  and (purg is None or purg not in m.roles)]
    if not candidates:
        return await feedback(interaction, 'No eligible members found.')
    member = random.choice(candidates)
    ok, msg = await imprison(member, interaction.user, 'random')
    if ok:
        await announce_punishment(interaction.channel, member, interaction.user, 'random')
    await feedback(interaction, f'{member.display_name}: {msg}')


@bot.tree.command(name='purgatoryrole', description='Send eligible members with a role to Purgatory')
async def purgatoryrole(interaction: discord.Interaction, role: discord.Role):
    if not permitted(interaction.user, 'bulk'):
        return await feedback(interaction, 'Only Head Purgatory Mods and Server Managers.')
    await interaction.response.defer(ephemeral=True)
    count = 0
    for member in list(role.members):
        ok, _ = await imprison(member, interaction.user)
        if ok:
            count += 1
            await announce_punishment(interaction.channel, member, interaction.user, 'normal')
    await feedback(interaction, f'Sent {count} eligible members to Purgatory.')


@bot.tree.command(name='unpurgatoryrole', description='Release members who originally held a role')
async def unpurgatoryrole(interaction: discord.Interaction, role: discord.Role):
    if not permitted(interaction.user, 'bulk'):
        return await feedback(interaction, 'Only Head Purgatory Mods and Server Managers.')
    await interaction.response.defer(ephemeral=True)
    rows = db.execute('SELECT user_id,roles FROM punishments WHERE guild_id=?',
                      (interaction.guild.id,)).fetchall()
    count = 0
    for user_id, original in rows:
        if role.id not in json.loads(original):
            continue
        member = interaction.guild.get_member(user_id)
        if member and target_ok(interaction.user, member):
            ok, _ = await release(member)
            if ok:
                count += 1
                await announce_release(interaction.channel, member, interaction.user.mention)
    await feedback(interaction, f'Released {count} members.')


class PurgatoryView(discord.ui.View):
    def __init__(self, members, owner_id):
        super().__init__(timeout=180)
        self.members = members
        self.owner_id = owner_id
        self.page = 0
        self.refresh()

    @property
    def pages(self):
        return max(1, (len(self.members) + 9) // 10)

    def embed(self):
        start = self.page * 10
        lines = [f'**{i + 1}.** {m.mention}'
                 for i, m in enumerate(self.members[start:start + 10], start)]
        embed = discord.Embed(title="🔥 Gilbert's Purgatory",
                              description='\n'.join(lines) or 'Nobody is in Purgatory.',
                              color=discord.Color.red())
        embed.set_footer(text=f'Page {self.page + 1}/{self.pages} • {len(self.members)} members')
        return embed

    def refresh(self):
        self.clear_items()
        if self.pages == 1:
            return
        first = min(max(0, self.page - 12), max(0, self.pages - 25))
        options = [discord.SelectOption(label=f'Page {i + 1}', value=str(i),
                                        default=i == self.page)
                   for i in range(first, min(first + 25, self.pages))]
        select = discord.ui.Select(placeholder=f'Page {self.page + 1}', options=options)

        async def changed(interaction):
            self.page = int(select.values[0])
            self.refresh()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        select.callback = changed
        self.add_item(select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await feedback(interaction, 'Only the moderator who opened this menu can change pages.')
            return False
        return True


@bot.tree.command(name='viewpurgatory', description='View Purgatory members by page')
async def viewpurgatory(interaction: discord.Interaction):
    if not permitted(interaction.user):
        return await feedback(interaction, 'You cannot use this command.')
    settings = await guild_settings(interaction.guild.id, 'purgatory')
    configured = settings.get('roleId')
    purg_id = int(configured) if str(configured or '').isdigit() else PURGATORY_ROLE_ID
    role = interaction.guild.get_role(purg_id)
    if not role:
        return await feedback(interaction, 'Purgatory role not found.')
    view = PurgatoryView(list(role.members), interaction.user.id)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@tasks.loop(seconds=1)
async def auto_release():
    rows = db.execute('SELECT guild_id,user_id,expires FROM punishments WHERE expires IS NOT NULL').fetchall()
    for guild_id, user_id, expires in rows:
        if datetime.fromisoformat(expires) > now():
            continue
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        member = guild.get_member(user_id)
        if not member:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                continue
        ok, msg = await release(member)
        if ok:
            await announce_release(guild.get_channel(GENERAL_CHANNEL_ID), member)
        else:
            print('Auto-release retry:', member.id, msg)


@auto_release.before_loop
async def wait_release():
    await bot.wait_until_ready()


async def ask_gemini(channel_id, prompt):
    if not ai:
        return None
    messages = [{'role': 'system', 'content': PERSONALITY}]
    for author, content in list(chat_history[channel_id])[-12:]:
        messages.append({'role': 'user', 'content': f'{author}: {content[:350]}'})
    messages.append({'role': 'user', 'content': prompt})
    try:
        async with ai_lock:
            response = await asyncio.wait_for(ai.chat.completions.create(
                model=MODEL, messages=messages, max_tokens=180), timeout=30)
        return response.choices[0].message.content
    except Exception as exc:
        print('Gemini error:', repr(exc))
        return None


async def find_gif(query):
    """Search GIPHY for a safe GIF; return None when unavailable."""
    if not GIPHY_API_KEY:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                'https://api.giphy.com/v1/gifs/search',
                params={'api_key': GIPHY_API_KEY, 'q': query[:80],
                        'limit': 5, 'rating': 'pg', 'lang': 'en'},
            ) as response:
                if response.status != 200:
                    print('GIPHY error status:', response.status)
                    return None
                payload = await response.json()
        results = payload.get('data', [])
        if not results:
            return None
        item = random.choice(results)
        return item.get('url')
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        print('GIPHY error:', repr(exc))
        return None


async def speak(channel, prompt, *, premium_options=None, guild_id=None):
    if not ai:
        return False
    # One response per channel per 12 seconds, including mentions.
    if time.monotonic() - last_reply[channel.id] < 12:
        return False
    last_reply[channel.id] = time.monotonic()
    if premium_options is not None and guild_id is not None:
        month = datetime.now(timezone.utc).strftime('%Y-%m')
        limit = max(0, min(100000, int(premium_options.get('monthlyReplyLimit', 2000))))
        async with usage_lock:
            used = db.execute('SELECT replies FROM premium_usage WHERE guild_id=? AND month=?', (guild_id, month)).fetchone()
            if used and used[0] >= limit or limit == 0:
                return False
            db.execute('INSERT INTO premium_usage(guild_id,month,replies) VALUES(?,?,1) ON CONFLICT(guild_id,month) DO UPDATE SET replies=replies+1', (guild_id, month))
            db.commit()
    if premium_options:
        personality = str(premium_options.get('personality', ''))[:400]
        if personality:
            prompt += '\nPremium personality preference: ' + personality
        if premium_options.get('profanity') is not True:
            prompt += '\nAvoid profanity.'
    async with channel.typing():
        answer = await ask_gemini(channel.id, prompt)
    if not answer or not answer.strip():
        return False
    gif_match = re.search(r'\[\[GIF:([^]\n]{2,80})\]\]', answer, re.I)
    answer = re.sub(r'\[\[GIF:[^]\n]*\]\]', '', answer, flags=re.I).strip()
    answer = answer[:1700]
    if answer:
        await post(channel, answer)
        chat_history[channel.id].append(('Gilbert', answer))
    if gif_match and (premium_options is None or premium_options.get('gifEnabled') is True) and random.random() < GIF_CHANCE:
        gif_url = await find_gif(gif_match.group(1).strip())
        if gif_url:
            await post(channel, gif_url)
    return bool(answer)


@bot.tree.command(name="talk", description="Talk privately with Gilbert in DMs or in this channel")
@app_commands.describe(message="What do you want to say to Gilbert?")
async def talk(interaction: discord.Interaction, message: str):
    if not ai:
        await interaction.response.send_message("Gilbert's AI is unavailable right now.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=interaction.guild is not None)
    channel_id = interaction.channel_id or interaction.user.id
    chat_history[channel_id].append((interaction.user.display_name, message[:350]))
    answer = await ask_gemini(channel_id, f'{interaction.user.display_name} asked Gilbert: {message[:1000]}\\nReply conversationally.')
    if not answer:
        await interaction.followup.send("Gilbert couldn't answer right now. Try again.", ephemeral=interaction.guild is not None)
        return
    answer = re.sub(r'\\[\\[GIF:[^]\\n]*\\]\\]', '', answer, flags=re.I).strip()[:1900]
    await interaction.followup.send(answer or "Gilbert has nothing to say right now.", ephemeral=interaction.guild is not None)
    chat_history[channel_id].append(('Gilbert', answer))


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if message.guild is None:
        if not message.content.strip():
            return
        if re.search(r'(?i)\\bgilbert\\b', message.content) or (bot.user and bot.user in message.mentions):
            chat_history[message.channel.id].append((message.author.display_name, message.content[:350]))
            await speak(message.channel, f'{message.author.display_name} sent a private DM: {message.content[:500]}\\nReply conversationally.')
        return
    automod = await guild_settings(message.guild.id, 'automod')
    if automod.get('enabled') and not message.author.guild_permissions.manage_messages:
        ignored = set(id_list(automod.get('ignoredChannelIds', '')))
        if message.channel.id not in ignored:
            content = message.content
            invites = bool(re.search(r'(?:discord\.gg/|discord(?:app)?\.com/invite/)', content, re.I))
            links = bool(re.search(r'https?://|www\.', content, re.I))
            mentions = len(set(message.raw_mentions)) + len(set(message.raw_role_mentions))
            blocked = ((automod.get('blockInvites') and invites)
                       or (automod.get('blockLinks') and links)
                       or (mentions > (int(automod.get('maxMentions', 5)) if str(automod.get('maxMentions', 5)).isdigit() else 5)))
            if blocked and message.channel.permissions_for(message.guild.me).manage_messages:
                try:
                    await message.delete()
                    return
                except discord.HTTPException:
                    pass
    await bot.process_commands(message)
    config = await guild_settings(message.guild.id, 'commands')
    if config.get('enabled', True) and len(message.content) > 1 and message.content.startswith('!'):
        command_name = message.content[1:].split(maxsplit=1)[0].lower()
        for entry in config.get('entries', []):
            if isinstance(entry, dict) and str(entry.get('name', '')).lower() == command_name:
                response = entry.get('response', '')
                if isinstance(response, str) and response:
                    await post(message.channel, format_message(response, message.author))
                break
    premium = await guild_settings(message.guild.id, 'premium')
    configured_channel = premium.get('channelId', '')
    premium_channel = int(configured_channel) if str(configured_channel).isdigit() else None
    paid_ai = (premium.get('aiEnabled') is True
               and premium_channel == message.channel.id
               and await premium_active(message.guild.id))
    if not paid_ai and message.channel.id != GENERAL_CHANNEL_ID:
        return
    if not message.content.strip():
        return
    chat_history[message.channel.id].append(
        (message.author.display_name, message.content[:350]))
    # No random responses, no reply-to-bot trigger, no proactive messages.
    # Mentioning Gilbert or using his name is required.
    mentioned = bot.user in message.mentions
    named = bool(re.search(r'(?i)\bgilbert\b', message.content))
    if mentioned or named:
        await speak(message.channel,
                    f'{message.author.display_name} addressed Gilbert: '
                    f'{message.content[:500]}\nReply directly, briefly and with attitude.',
                    premium_options=premium if paid_ai else None,
                    guild_id=message.guild.id if paid_ai else None)


@bot.event
async def on_member_join(member):
    welcome = await guild_settings(member.guild.id, 'welcome')
    if welcome.get('enabled'):
        channel_id = welcome.get('channelId', '')
        channel = member.guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        if channel:
            await post(channel, format_message(
                welcome.get('message', 'Welcome {user} to {server}!'), member))

    autoroles = await guild_settings(member.guild.id, 'autoroles')
    if autoroles.get('enabled'):
        selected = 'botRoleIds' if member.bot else 'roleIds'
        me = member.guild.me
        if me and me.guild_permissions.manage_roles:
            roles = [member.guild.get_role(rid) for rid in id_list(autoroles.get(selected, ''))]
            roles = [role for role in roles if role and not role.managed and role < me.top_role]
            if roles:
                try:
                    await member.add_roles(*roles, reason='Gilbert dashboard autoroles')
                except discord.HTTPException as exc:
                    print('Autoroles error:', exc)


@bot.event
async def on_member_remove(member):
    welcome = await guild_settings(member.guild.id, 'welcome')
    if welcome.get('enabled') and welcome.get('goodbyeEnabled'):
        channel_id = welcome.get('goodbyeChannelId', '')
        channel = member.guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
        if channel:
            await post(channel, format_message(
                welcome.get('goodbyeMessage', 'Goodbye {user}.'), member))


async def reaction_role_change(payload, add):
    if not payload.guild_id or payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    settings = await guild_settings(guild.id, 'reaction-roles')
    if not settings.get('enabled'):
        return
    member = guild.get_member(payload.user_id)
    if not member:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    me = guild.me
    if not me or not me.guild_permissions.manage_roles:
        return
    for entry in settings.get('entries', []):
        if not isinstance(entry, dict) or str(entry.get('messageId')) != str(payload.message_id):
            continue
        if str(entry.get('emoji')) != str(payload.emoji):
            continue
        role_id = entry.get('roleId', '')
        role = guild.get_role(int(role_id)) if str(role_id).isdigit() else None
        if not role or role.managed or role >= me.top_role:
            continue
        try:
            if add:
                await member.add_roles(role, reason='Gilbert reaction role')
            else:
                await member.remove_roles(role, reason='Gilbert reaction role')
        except discord.HTTPException as exc:
            print('Reaction role error:', exc)


@bot.event
async def on_raw_reaction_add(payload):
    await reaction_role_change(payload, True)


@bot.event
async def on_raw_reaction_remove(payload):
    await reaction_role_change(payload, False)


@bot.event
async def on_ready():
    print(f'Gilbert online as {bot.user}')
    if not auto_release.is_running():
        auto_release.start()
    if not getattr(bot, 'synced', False):
        # Global registration allows Gilbert to work in every installed guild.
        synced = await bot.tree.sync()
        bot.synced = True
        print(f'Synced {len(synced)} global slash commands')
    if not ai:
        print('GEMINI_API_KEY missing; AI chat is disabled.')
    if not GIPHY_API_KEY:
        print('GIPHY_API_KEY missing; GIFs are disabled (text and emojis still work).')


if not os.getenv('DISCORD_TOKEN'):
    raise RuntimeError('Missing DISCORD_TOKEN environment variable')
bot.run(os.environ['DISCORD_TOKEN'])
