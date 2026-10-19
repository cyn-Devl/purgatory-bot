"""Read per-guild dashboard settings from Supabase REST API.

Uses the service-role key only on the bot host; never expose it to Discord.
Short cache limits requests, while failures leave existing bot features intact.
"""
import asyncio
import os
import time

import aiohttp

URL = os.getenv("SUPABASE_URL", "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
_cache = {}
_cache_lock = asyncio.Lock()
CACHE_SECONDS = 20


async def guild_settings(guild_id, module):
    if not URL or not KEY:
        return {}
    cache_key = (str(guild_id), module)
    entry = _cache.get(cache_key)
    if entry and time.monotonic() - entry[0] < CACHE_SECONDS:
        return entry[1]
    async with _cache_lock:
        entry = _cache.get(cache_key)
        if entry and time.monotonic() - entry[0] < CACHE_SECONDS:
            return entry[1]
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{URL}/rest/v1/guild_modules",
                    params={"guild_id": f"eq.{guild_id}", "module": f"eq.{module}", "select": "settings", "limit": "1"},
                    headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
                ) as response:
                    response.raise_for_status()
                    rows = await response.json()
            settings = rows[0].get("settings", {}) if rows else {}
            if not isinstance(settings, dict):
                settings = {}
            _cache[cache_key] = (time.monotonic(), settings)
            return settings
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as exc:
            print(f"Dashboard settings fetch failed for {module}: {type(exc).__name__}")
            return entry[1] if entry else {}


def id_list(value):
    if not isinstance(value, str):
        return []
    return [int(x) for x in value.replace(",", " ").split() if x.isdigit()]


def format_message(template, member):
    return (template.replace("{user}", member.mention)
                    .replace("{server}", member.guild.name)
                    .replace("{username}", member.display_name))[:1900]


async def premium_active(guild_id):
    """Fail closed when Stripe-backed entitlement cannot be verified."""
    if not URL or not KEY:
        return False
    cache_key = (str(guild_id), "__premium_entitlement__")
    entry = _cache.get(cache_key)
    if entry and time.monotonic() - entry[0] < CACHE_SECONDS:
        return entry[1]
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{URL}/rest/v1/guild_subscriptions",
                params={"guild_id": f"eq.{guild_id}", "select": "status", "limit": "1"},
                headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"},
            ) as response:
                response.raise_for_status()
                rows = await response.json()
        active = bool(rows and rows[0].get("status") in ("active", "trialing"))
        _cache[cache_key] = (time.monotonic(), active)
        return active
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
        print(f"Premium entitlement verification failed: {type(exc).__name__}")
        return False
