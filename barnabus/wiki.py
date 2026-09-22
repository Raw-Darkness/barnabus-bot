"""/wiki — MediaWiki search, no model involved."""
import logging
import re
import urllib.parse

import aiohttp
import discord
from discord import app_commands

from . import core


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("&quot;", '"').replace("&amp;", "&").replace("&#039;", "'")


@core.tree.command(name="wiki", description="Search the game wiki")
@app_commands.describe(term="What to search for", share="Post the result publicly instead of only to you")
async def wiki_command(interaction: discord.Interaction, term: str, share: bool = False):
    try:
        base = (core.config.get("WikiBaseURL") or "").rstrip("/")
        if not base:
            await interaction.response.send_message("No wiki is configured.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=not share, thinking=True)
        url = f"{base}/w/api.php?action=query&list=search&format=json&srlimit=5&srprop=snippet&srsearch=" + urllib.parse.quote(term)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(url, headers={"User-Agent": core.bot_name() + "Bot"}) as r:
                    r.raise_for_status()
                    results = (await r.json()).get("query", {}).get("search", [])
        except Exception:
            logging.exception("Wiki search failed")
            await interaction.followup.send("The wiki isn't answering. Try again later.", ephemeral=not share)
            return
        if not results:
            await interaction.followup.send(f"No wiki page found for **{term[:100]}**.", ephemeral=not share)
            return
        link = lambda t: f"{base}/wiki/" + urllib.parse.quote(t.replace(" ", "_"))
        top = results[0]
        embed = discord.Embed(title=top["title"], url=link(top["title"]),
                              description=_strip_html(top.get("snippet", ""))[:400] + "…", color=0x3498DB)
        if len(results) > 1:
            embed.add_field(name="More results", value="\n".join(f"[{r['title']}]({link(r['title'])})" for r in results[1:5]), inline=False)
        embed.set_footer(text=core.text("WikiFooter", "Wicked Island wiki"))
        await interaction.followup.send(embed=embed, ephemeral=not share)
    except Exception:
        logging.exception("/wiki failed")
