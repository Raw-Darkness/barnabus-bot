"""Stats channels: member count and Steam players online, in channel names."""
import asyncio
import logging

import aiohttp

from . import core


async def _rename(channel_id: int, name: str) -> None:
    ch = await core.get_channel(channel_id)
    if ch is not None and ch.name != name:
        await ch.edit(name=name, reason="Stats update")
        logging.info("Stats: renamed %s -> %r", channel_id, name)


async def update() -> None:
    if not core.config.get("StatsEnabled"):
        return
    mid = core.cfg_int("StatsMemberChannelID")
    if mid:
        ch = core.bot.get_channel(mid)
        count = getattr(getattr(ch, "guild", None), "member_count", None)
        if count:
            await _rename(mid, str(core.config.get("StatsMemberTemplate", "members-{count}")).format(count=count))
    pid, appid = core.cfg_int("StatsPlayersChannelID"), core.cfg_int("SteamAppID")
    if pid and appid:
        url = f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={appid}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get(url) as r:
                r.raise_for_status()
                players = (await r.json()).get("response", {}).get("player_count")
        if players is not None:
            await _rename(pid, str(core.config.get("StatsPlayersTemplate", "in-game-now-{count}")).format(count=players))


async def loop() -> None:
    await core.bot.wait_until_ready()
    while not core.bot.is_closed():
        try:
            await update()
        except Exception:
            logging.exception("Stats update failed")
        # Discord allows 2 channel-name edits per 10 minutes — never poll faster.
        await asyncio.sleep(max(600, core.cfg_int("StatsUpdateMin", 10) * 60))
