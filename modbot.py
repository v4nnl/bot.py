"""Mention-prefix Discord bot: real moderation + utilities + fun.
Use it by tagging the bot:  @Bot ban @user spamming
"""
import asyncio
import datetime as dt
import json
import os
import random
import re
import time
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

import fakegen as fg

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
WARN_FILE = Path(__file__).with_name("warnings.json")
START = time.time()

intents = discord.Intents.default()
intents.message_content = True   # enable in the Developer Portal too
intents.members = True           # enable in the Developer Portal too
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents,
                   help_command=None, case_insensitive=True,
                   allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))

RED, GREEN, GOLD, BLURPLE = 0xED4245, 0x57F287, 0xFEE75C, 0x5865F2


# ---------------- helpers ----------------
def emb(title, desc=None, color=BLURPLE):
    return discord.Embed(title=title, description=desc, color=color)


def load_warns():
    try:
        return json.loads(WARN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_warns(d):
    tmp = WARN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, WARN_FILE)


def parse_duration(s: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)\s*([smhdw])", s.strip().lower())
    if not m:
        raise commands.BadArgument("Duration looks like `30s`, `10m`, `2h`, `1d`, `1w`.")
    n, unit = int(m.group(1)), m.group(2)
    secs = n * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    if secs < 1 or secs > 28 * 86400:
        raise commands.BadArgument("Duration must be between 1 second and 28 days.")
    return dt.timedelta(seconds=secs)


def can_act(ctx: commands.Context, target: discord.Member):
    """Return an error string if the author/bot may not act on target, else None."""
    if target == ctx.author:
        return "You can't do that to yourself."
    if target.id == ctx.guild.owner_id:
        return "You can't do that to the server owner."
    if target == ctx.guild.me:
        return "Nice try."
    if ctx.author.id != ctx.guild.owner_id and target.top_role >= ctx.author.top_role:
        return "That member's top role is equal to or higher than yours."
    if target.top_role >= ctx.guild.me.top_role:
        return "That member's top role is equal to or higher than mine. Move my role higher."
    return None


async def guard(ctx, target):
    err = can_act(ctx, target)
    if err:
        await ctx.send(f"❌ {err}")
        return False
    return True


def reason_str(ctx, reason):
    return f"{ctx.author} ({ctx.author.id}): {reason or 'No reason given'}"[:512]


async def try_dm(member, text):
    try:
        await member.send(text)
    except (discord.Forbidden, discord.HTTPException):
        pass


def fmt_uptime(sec):
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h}h {m}m {s}s"


# ---------------- moderation ----------------
@bot.command(help="ban <@user|id> [reason]")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, target: discord.User, *, reason: str = None):
    member = ctx.guild.get_member(target.id)
    if member and not await guard(ctx, member):
        return
    if member:
        await try_dm(member, f"🔨 You were banned from **{ctx.guild.name}**. Reason: {reason or 'none'}")
    await ctx.guild.ban(target, reason=reason_str(ctx, reason), delete_message_days=0)
    await ctx.send(embed=emb("🔨 Banned", f"**{target}** was banned.\n**Reason:** {reason or 'none'}", RED))


@bot.command(help="unban <user id> [reason]")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx, target: discord.User, *, reason: str = None):
    try:
        await ctx.guild.unban(target, reason=reason_str(ctx, reason))
    except discord.NotFound:
        return await ctx.send("❌ That user isn't banned.")
    await ctx.send(embed=emb("✅ Unbanned", f"**{target}** was unbanned.", GREEN))


@bot.command(help="softban <@user> [reason]  (ban+unban to delete 1 day of messages)")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def softban(ctx, member: discord.Member, *, reason: str = None):
    if not await guard(ctx, member):
        return
    await ctx.guild.ban(member, reason=reason_str(ctx, "softban: " + (reason or "")), delete_message_days=1)
    await ctx.guild.unban(member, reason="softban release")
    await ctx.send(embed=emb("🧹 Softbanned", f"**{member}**'s recent messages were cleared.", GOLD))


@bot.command(help="kick <@user> [reason]")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = None):
    if not await guard(ctx, member):
        return
    await try_dm(member, f"👢 You were kicked from **{ctx.guild.name}**. Reason: {reason or 'none'}")
    await member.kick(reason=reason_str(ctx, reason))
    await ctx.send(embed=emb("👢 Kicked", f"**{member}** was kicked.\n**Reason:** {reason or 'none'}", RED))


@bot.command(aliases=["timeout"], help="mute <@user> <duration e.g. 10m> [reason]")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, duration: str, *, reason: str = None):
    if not await guard(ctx, member):
        return
    delta = parse_duration(duration)
    await member.timeout(delta, reason=reason_str(ctx, reason))
    await ctx.send(embed=emb("🔇 Muted", f"**{member}** muted for **{duration}**.\n**Reason:** {reason or 'none'}", GOLD))


@bot.command(aliases=["untimeout"], help="unmute <@user>")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    await member.timeout(None, reason=reason_str(ctx, "unmute"))
    await ctx.send(embed=emb("🔊 Unmuted", f"**{member}** can talk again.", GREEN))


@bot.command(help="warn <@user> [reason]")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason: str = "No reason given"):
    if not await guard(ctx, member):
        return
    d = load_warns()
    key = f"{ctx.guild.id}:{member.id}"
    d.setdefault(key, []).append({"by": ctx.author.id, "reason": reason[:300], "ts": int(time.time())})
    save_warns(d)
    n = len(d[key])
    await try_dm(member, f"⚠️ You were warned in **{ctx.guild.name}**: {reason}")
    await ctx.send(embed=emb("⚠️ Warned", f"**{member}** now has **{n}** warning(s).\n**Reason:** {reason}", GOLD))


@bot.command(aliases=["warns"], help="warnings <@user>")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def warnings(ctx, member: discord.Member):
    w = load_warns().get(f"{ctx.guild.id}:{member.id}", [])
    if not w:
        return await ctx.send(f"✅ **{member}** has no warnings.")
    lines = [f"`{i+1}.` <t:{x['ts']}:d> by <@{x['by']}>: {x['reason']}" for i, x in enumerate(w[-15:])]
    await ctx.send(embed=emb(f"⚠️ Warnings for {member}", "\n".join(lines), GOLD))


@bot.command(aliases=["clearwarns"], help="clearwarnings <@user>")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def clearwarnings(ctx, member: discord.Member):
    d = load_warns()
    d.pop(f"{ctx.guild.id}:{member.id}", None)
    save_warns(d)
    await ctx.send(f"🧽 Cleared warnings for **{member}**.")


@bot.command(aliases=["clear"], help="purge <1-200> [@user]")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(ctx, amount: int, member: discord.Member = None):
    if not 1 <= amount <= 200:
        return await ctx.send("❌ Amount must be 1 to 200.")
    check = (lambda m: m.author == member) if member else None
    deleted = await ctx.channel.purge(limit=amount + 1, check=check)
    msg = await ctx.send(f"🧹 Deleted **{max(len(deleted) - 1, 0)}** messages.")
    await asyncio.sleep(4)
    try:
        await msg.delete()
    except discord.HTTPException:
        pass


@bot.command(help="slowmode <seconds 0-21600>")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int):
    if not 0 <= seconds <= 21600:
        return await ctx.send("❌ Use 0 to 21600 seconds.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=reason_str(ctx, "slowmode"))
    await ctx.send("🐢 Slowmode off." if seconds == 0 else f"🐢 Slowmode set to **{seconds}s**.")


@bot.command(help="lock  (stop @everyone from sending here)")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def lock(ctx):
    ow = ctx.channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=reason_str(ctx, "lock"))
    await ctx.send("🔒 Channel locked.")


@bot.command(help="unlock")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def unlock(ctx):
    ow = ctx.channel.overwrites_for(ctx.guild.default_role)
    ow.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=ow, reason=reason_str(ctx, "unlock"))
    await ctx.send("🔓 Channel unlocked.")


@bot.command(aliases=["nickname"], help="nick <@user> [new nickname]  (blank resets)")
@commands.guild_only()
@commands.has_permissions(manage_nicknames=True)
@commands.bot_has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, name: str = None):
    if not await guard(ctx, member):
        return
    await member.edit(nick=name[:32] if name else None, reason=reason_str(ctx, "nick"))
    await ctx.send(f"✏️ Nickname {'set to **' + name[:32] + '**' if name else 'reset'} for **{member}**.")


def _role_check(ctx, role):
    if role >= ctx.guild.me.top_role:
        return "That role is higher than or equal to my top role."
    if ctx.author.id != ctx.guild.owner_id and role >= ctx.author.top_role:
        return "That role is higher than or equal to your top role."
    if role.is_default() or role.managed:
        return "I can't assign that role."
    return None


@bot.command(help="addrole <@user> <@role|name>")
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def addrole(ctx, member: discord.Member, *, role: discord.Role):
    err = _role_check(ctx, role)
    if err:
        return await ctx.send(f"❌ {err}")
    await member.add_roles(role, reason=reason_str(ctx, "addrole"))
    await ctx.send(f"✅ Gave **{role.name}** to **{member}**.")


@bot.command(help="removerole <@user> <@role|name>")
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def removerole(ctx, member: discord.Member, *, role: discord.Role):
    err = _role_check(ctx, role)
    if err:
        return await ctx.send(f"❌ {err}")
    await member.remove_roles(role, reason=reason_str(ctx, "removerole"))
    await ctx.send(f"✅ Removed **{role.name}** from **{member}**.")


# ---------------- info / utility ----------------
@bot.command(aliases=["ui"], help="userinfo [@user]")
@commands.guild_only()
async def userinfo(ctx, member: discord.Member = None):
    m = member or ctx.author
    e = emb(f"👤 {m}", color=m.color.value or BLURPLE)
    e.set_thumbnail(url=m.display_avatar.url)
    e.add_field(name="ID", value=m.id)
    e.add_field(name="Created", value=discord.utils.format_dt(m.created_at, "R"))
    e.add_field(name="Joined", value=discord.utils.format_dt(m.joined_at, "R") if m.joined_at else "?")
    e.add_field(name="Top role", value=m.top_role.mention)
    e.add_field(name="Bot", value="Yes" if m.bot else "No")
    await ctx.send(embed=e)


@bot.command(aliases=["si"], help="serverinfo")
@commands.guild_only()
async def serverinfo(ctx):
    g = ctx.guild
    e = emb(f"🏠 {g.name}")
    if g.icon:
        e.set_thumbnail(url=g.icon.url)
    e.add_field(name="Owner", value=f"<@{g.owner_id}>")
    e.add_field(name="Members", value=g.member_count)
    e.add_field(name="Channels", value=len(g.channels))
    e.add_field(name="Roles", value=len(g.roles))
    e.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"))
    await ctx.send(embed=e)


@bot.command(aliases=["av"], help="avatar [@user]")
async def avatar(ctx, member: discord.Member = None):
    m = member or ctx.author
    e = emb(f"🖼️ {m.display_name}")
    e.set_image(url=m.display_avatar.url)
    await ctx.send(embed=e)


@bot.command(help="ping")
async def ping(ctx):
    await ctx.send(f"🏓 {round(bot.latency * 1000)} ms | up {fmt_uptime(time.time() - START)}")


@bot.command(help="say <text>  (needs Manage Messages)")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def say(ctx, *, text: str):
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass
    await ctx.send(text[:1900])


@bot.command(help="poll <question>")
@commands.guild_only()
async def poll(ctx, *, question: str):
    m = await ctx.send(embed=emb("📊 Poll", question[:1000]))
    for r in ("👍", "👎"):
        await m.add_reaction(r)


# ---------------- fun ----------------
@bot.command(name="8ball", help="8ball <question>")
async def eightball(ctx, *, question: str):
    await ctx.send("🎱 " + random.choice(["Yes.", "No.", "Maybe.", "Ask again later.", "Absolutely not.", "100%.", "Bro what."]))


@bot.command(aliases=["flip"], help="coinflip")
async def coinflip(ctx):
    await ctx.send("🪙 " + random.choice(["Heads", "Tails"]))


@bot.command(help="roll [sides=6]")
async def roll(ctx, sides: int = 6):
    if not 2 <= sides <= 1000:
        return await ctx.send("❌ Sides must be 2 to 1000.")
    await ctx.send(f"🎲 {random.randint(1, sides)} (d{sides})")


@bot.command(help="ship <@a> <@b>")
async def ship(ctx, a: discord.Member, b: discord.Member):
    pct = random.Random(f"{min(a.id, b.id)}{max(a.id, b.id)}").randint(0, 100)
    await ctx.send(f"💘 {a.display_name} + {b.display_name}: `{'█' * (pct // 10)}{'░' * (10 - pct // 10)}` **{pct}%**")


@bot.command(help="iq [@user]")
async def iq(ctx, member: discord.Member = None):
    m = member or ctx.author
    await ctx.send(f"🧠 {m.display_name}'s IQ: **{fg.rng_for(m.id, 'iq').randint(1, 220)}**")


@bot.command(help="ipleak <@user>  (FAKE)")
async def ipleak(ctx, member: discord.Member):
    r = fg.rng_for(member.id, "ip")
    msg = await ctx.send(f"💻 Tracing {member.display_name}...")
    await asyncio.sleep(1.5)
    e = emb(f"🚨 {member.display_name} 'leaked' 🚨", color=RED)
    e.add_field(name="IPv4", value=f"`{fg.fake_ip(r)}`")
    e.add_field(name="IPv6", value=f"`{fg.fake_ipv6(r)}`")
    e.add_field(name="Location", value=r.choice(fg.CITIES))
    e.add_field(name="ISP", value=r.choice(fg.ISPS))
    e.set_footer(text="😂 100% fake, randomly generated")
    await msg.edit(content=None, embed=e)


@bot.command(help="bank <@user>  (FAKE)")
async def bank(ctx, member: discord.Member):
    b = fg.fake_bank(fg.rng_for(member.id, "bank"))
    e = emb(f"🏦 {member.display_name}'s 'bank info'", color=GOLD)
    e.add_field(name="Bank", value=b["bank"], inline=False)
    e.add_field(name="Card", value=f"`{b['card']}`", inline=False)
    e.add_field(name="Balance", value=b["balance"])
    e.set_footer(text="😂 fake card, can never validate")
    await ctx.send(embed=e)


@bot.command(help="phone <@user>  (FAKE)")
async def phone(ctx, member: discord.Member):
    await ctx.send(f"📞 {member.display_name}: `{fg.fake_phone(fg.rng_for(member.id, 'phone'))}` *(fake 555 number)*")


@bot.command(help="fakeban <@user>  (does NOT ban)")
async def fakeban(ctx, member: discord.Member, *, reason: str = "Being too awesome"):
    await ctx.send(embed=emb("🔨 BAN HAMMER", f"**{member.display_name}** banned. Reason: {reason[:200]}", RED))
    await asyncio.sleep(3)
    await ctx.send("😂 jk, fake ban.")


@bot.command(help="hack <@user>  (FAKE)")
async def hack(ctx, member: discord.Member):
    steps = [f"Target: {member.display_name}", "Bypassing firewall... OK", "Guessing password: hunter2... FAIL",
             "Guessing password: password1... OK", "Hack complete (none of this was real)."]
    lines, msg = [], await ctx.send("```\nInitializing...\n```")
    for s in steps:
        await asyncio.sleep(1)
        lines.append("> " + s)
        await msg.edit(content="```\n" + "\n".join(lines) + "\n```")


@bot.command(help="help")
async def help(ctx):
    cats = {"🔨 Moderation": ["ban", "unban", "softban", "kick", "mute", "unmute", "warn", "warnings", "clearwarnings",
                              "purge", "slowmode", "lock", "unlock", "nick", "addrole", "removerole"],
            "ℹ️ Utility": ["userinfo", "serverinfo", "avatar", "ping", "say", "poll"],
            "🎉 Fun": ["8ball", "coinflip", "roll", "ship", "iq", "ipleak", "bank", "phone", "fakeban", "hack"]}
    e = emb("Commands", f"Tag me first: `@{ctx.guild.me.display_name if ctx.guild else 'Bot'} <command> ...`")
    for name, cmds in cats.items():
        e.add_field(name=name, value="\n".join(f"`{bot.get_command(c).help}`" for c in cmds), inline=False)
    await ctx.send(embed=e)


# ---------------- errors ----------------
@bot.event
async def on_command_error(ctx, error):
    error = getattr(error, "original", error)
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        msg = "You need: " + ", ".join(p.replace("_", " ") for p in error.missing_permissions)
    elif isinstance(error, commands.BotMissingPermissions):
        msg = "I'm missing: " + ", ".join(p.replace("_", " ") for p in error.missing_permissions)
    elif isinstance(error, commands.MissingRequiredArgument):
        msg = f"Missing `{error.param.name}`. Usage: `{ctx.command.help}`"
    elif isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        msg = "I couldn't find that user."
    elif isinstance(error, commands.RoleNotFound):
        msg = "I couldn't find that role."
    elif isinstance(error, (commands.BadArgument, commands.NoPrivateMessage)):
        msg = str(error) or "Bad argument."
    elif isinstance(error, discord.Forbidden):
        msg = "Discord says I'm not allowed to do that (check my role position/permissions)."
    else:
        print("Unhandled:", repr(error))
        msg = "Something broke. Try again."
    await ctx.send(f"❌ {msg}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | {len(bot.commands)} commands")


if __name__ == "__main__":
    import sys
    import traceback
    print("Starting bot...", flush=True)
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN is missing in Railway Variables", flush=True)
        time.sleep(120)
        raise SystemExit(1)
    try:
        bot.run(TOKEN.strip())
    except Exception:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(120)
        raise
