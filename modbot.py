"""Mention-prefix Discord bot: real moderation + utilities + fun.
Use it by tagging the bot (@Bot ban @user spamming) or with the prefix (-ban @user spamming).
"""
import ast
import asyncio
import collections
import copy
import datetime as dt
import json
import operator
import os
import random
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Union

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import fakegen as fg

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATA_DIR = Path(os.getenv("DATA_DIR") or Path(__file__).parent)   # set DATA_DIR=/data if you attach a Railway Volume
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    DATA_DIR = Path(__file__).parent
WARN_FILE = DATA_DIR / "warnings.json"
TEMPBAN_FILE = DATA_DIR / "tempbans.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
START = time.time()
INSTANCE_ID = uuid.uuid4().hex[:6]   # differs per running copy of the bot

# --- duplicate-message guard (stops the same message being handled twice in one process) ---
_SEEN_IDS = collections.deque(maxlen=1000)
_SEEN_SET = set()


def already_seen(message_id: int) -> bool:
    if message_id in _SEEN_SET:
        return True
    if len(_SEEN_IDS) == _SEEN_IDS.maxlen:
        _SEEN_SET.discard(_SEEN_IDS[0])
    _SEEN_IDS.append(message_id)
    _SEEN_SET.add(message_id)
    return False

DEFAULT_PREFIX = "-"


def get_prefix(bot_, message):
    # Mention always works; the text prefix (default "-") is per server and changeable with the prefix command.
    base = commands.when_mentioned(bot_, message)
    p = get_cfg(message.guild.id)["prefix"] if message.guild else DEFAULT_PREFIX
    return base + [p]


intents = discord.Intents.default()
intents.message_content = True   # enable in the Developer Portal too
intents.members = True           # enable in the Developer Portal too
bot = commands.Bot(command_prefix=get_prefix, intents=intents,
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


# ---------------- security: settings, mod log, warnings, escalation ----------------
DEFAULT_CFG = {"prefix": DEFAULT_PREFIX, "log_channel": None, "automod": True, "antiraid": True, "antinuke": True,
               "whitelist": [], "badwords": [], "welcome_channel": None, "autorole": None}

WARN_MUTE_AT = 3      # 3 warnings -> 1 hour mute
WARN_KICK_AT = 5      # 5 warnings -> automatic kick
RAID_JOINS, RAID_WINDOW, RAID_DURATION = 8, 10, 600     # 8 joins in 10s -> raid mode for 10 min
NUKE_LIMIT, NUKE_WINDOW = 4, 10                         # 4 destructive actions in 10s -> punished
SPAM_LIMIT, SPAM_WINDOW = 6, 5                          # 6 messages in 5s -> spam
MASS_MENTIONS = 5
INVITE_RE = re.compile(r"(?:discord\.gg|discordapp\.com/invite|discord\.com/invite|dsc\.gg)/[\w-]+", re.I)


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


_SETTINGS = _load_json(SETTINGS_FILE)


def get_cfg(guild_id):
    cfg = copy.deepcopy(DEFAULT_CFG)
    cfg.update(_SETTINGS.get(str(guild_id), {}))
    return cfg


def update_cfg(guild_id, **changes):
    _SETTINGS.setdefault(str(guild_id), {}).update(changes)
    _save_json(SETTINGS_FILE, _SETTINGS)


async def modlog(guild, embed):
    cid = get_cfg(guild.id).get("log_channel")
    ch = guild.get_channel(cid) if cid else None
    if ch is None:
        return
    try:
        await ch.send(embed=embed)
    except discord.HTTPException:
        pass


def add_warning(guild_id, user_id, by_id, reason):
    d = load_warns()
    key = f"{guild_id}:{user_id}"
    d.setdefault(key, []).append({"by": by_id, "reason": reason[:300], "ts": int(time.time())})
    save_warns(d)
    return len(d[key])


async def escalate(guild, member, n):
    # Automatic punishment when warnings pile up. Returns a short note or None.
    note = None
    if n >= WARN_KICK_AT:
        await try_dm(member, f"👢 You were kicked from **{guild.name}** for reaching {n} warnings.")
        try:
            await member.kick(reason=f"Auto: reached {n} warnings")
            d = load_warns()
            d.pop(f"{guild.id}:{member.id}", None)   # fresh start if they rejoin
            save_warns(d)
            note = f"👢 Auto-kicked at {n} warnings."
        except discord.HTTPException:
            note = "⚠️ Hit the kick limit but I couldn't kick (my role is too low)."
    elif n >= WARN_MUTE_AT:
        try:
            await member.timeout(dt.timedelta(hours=1), reason=f"Auto: reached {n} warnings")
            note = f"🔇 Auto-muted for 1 hour at {n} warnings."
        except discord.HTTPException:
            note = "⚠️ Hit the mute limit but I couldn't mute (my role is too low)."
    if note:
        await modlog(guild, emb("🤖 Auto-escalation", f"**{member}** ({member.id})\n{note}", RED))
    return note


# in-memory state (resets on restart)
RAID_UNTIL = {}
JOINS = collections.defaultdict(collections.deque)
NUKE_LOG = collections.defaultdict(collections.deque)
NUKE_PUNISHED = {}
SPAM_LOG = collections.defaultdict(collections.deque)


async def automod(message):
    # Returns True if the message was removed and should not be processed further.
    if not message.guild or not isinstance(message.author, discord.Member):
        return False
    cfg = get_cfg(message.guild.id)
    m = message.author
    if not cfg["automod"] or m.guild_permissions.manage_messages or m.id in cfg["whitelist"]:
        return False
    text = message.content or ""
    reason = None
    if INVITE_RE.search(text):
        reason = "invite links"
    elif len({u.id for u in message.mentions if u.id != bot.user.id}) + len(message.role_mentions) >= MASS_MENTIONS:
        reason = "mass mentions"
    elif any(re.search(r"\b" + re.escape(w) + r"\b", text, re.I) for w in cfg["badwords"]):
        reason = "banned words"
    else:
        key, now = (message.guild.id, m.id), time.time()
        dq = SPAM_LOG[key]
        dq.append(now)
        while dq and now - dq[0] > SPAM_WINDOW:
            dq.popleft()
        if len(dq) >= SPAM_LIMIT:
            dq.clear()
            reason = "spam"
    if not reason:
        return False
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    if reason == "spam":
        try:
            await message.channel.purge(limit=15, check=lambda x: x.author == m)
        except discord.HTTPException:
            pass
        try:
            await m.timeout(dt.timedelta(minutes=5), reason="Automod: spam")
        except discord.HTTPException:
            pass
    n = add_warning(message.guild.id, m.id, bot.user.id, f"Automod: {reason}")
    note = await escalate(message.guild, m, n)
    try:
        await message.channel.send(f"⚠️ {m.mention} automod: **{reason}**. Warning **{n}/{WARN_KICK_AT}**." + (f"\n{note}" if note else ""),
                                   delete_after=8)
    except discord.HTTPException:
        pass
    await modlog(message.guild, emb("🛡️ Automod", f"**{m}** ({m.id}) in {message.channel.mention}\n**Reason:** {reason}\n**Warnings:** {n}", GOLD))
    return True


async def raid_kick(member, reason):
    if member.top_role >= member.guild.me.top_role:
        return False
    await try_dm(member, f"🚫 **{member.guild.name}** is under raid protection right now. Try joining again later.")
    try:
        await member.kick(reason=reason)
        return True
    except discord.HTTPException:
        return False


NUKE_ACTIONS = {discord.AuditLogAction.ban, discord.AuditLogAction.kick, discord.AuditLogAction.channel_delete,
                discord.AuditLogAction.role_delete, discord.AuditLogAction.member_prune}


async def nuke_punish(guild, user_id, why):
    result = "FAILED (move my role to the very top and give me Ban Members)"
    try:
        await guild.ban(discord.Object(id=user_id), reason=f"Anti-nuke: {why}", delete_message_days=0)
        result = "banned"
    except discord.HTTPException:
        member = guild.get_member(user_id)
        if member:
            try:
                await member.edit(roles=[], reason=f"Anti-nuke: {why}")
                result = "stripped of all roles"
            except discord.HTTPException:
                pass
    await modlog(guild, emb("🚨 ANTI-NUKE TRIGGERED", f"<@{user_id}> ({user_id}) did **{NUKE_LIMIT}+ destructive actions in {NUKE_WINDOW}s**.\n"
                            f"**Latest:** {why}\n**Action taken:** {result}\nDeleted channels/roles are not restored automatically.", RED))


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
    n = add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
    await try_dm(member, f"⚠️ You were warned in **{ctx.guild.name}**: {reason}")
    note = await escalate(ctx.guild, member, n)
    await ctx.send(embed=emb("⚠️ Warned", f"**{member}** now has **{n}/{WARN_KICK_AT}** warning(s).\n**Reason:** {reason}" + (f"\n{note}" if note else ""), GOLD))


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



# ---------------- more moderation ----------------
def load_tempbans():
    try:
        return json.loads(TEMPBAN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_tempbans(d):
    tmp = TEMPBAN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    os.replace(tmp, TEMPBAN_FILE)


@bot.command(help="tempban <@user> <duration e.g. 1d> [reason]")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def tempban(ctx, target: discord.User, duration: str, *, reason: str = None):
    delta = parse_duration(duration)
    member = ctx.guild.get_member(target.id)
    if member and not await guard(ctx, member):
        return
    if member:
        await try_dm(member, f"🔨 You were temp-banned from **{ctx.guild.name}** for {duration}. Reason: {reason or 'none'}")
    await ctx.guild.ban(target, reason=reason_str(ctx, f"tempban {duration}: {reason or ''}"), delete_message_days=0)
    d = load_tempbans()
    d[f"{ctx.guild.id}:{target.id}"] = int(time.time() + delta.total_seconds())
    save_tempbans(d)
    await ctx.send(embed=emb("⏳ Temp-banned", f"**{target}** banned for **{duration}**.\n**Reason:** {reason or 'none'}", RED))


@tasks.loop(seconds=30)
async def tempban_loop():
    d = load_tempbans()
    now, changed = time.time(), False
    for key, exp in list(d.items()):
        if exp > now:
            continue
        gid, uid = (int(x) for x in key.split(":"))
        guild = bot.get_guild(gid)
        if guild:
            try:
                await guild.unban(discord.Object(id=uid), reason="tempban expired")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        d.pop(key)
        changed = True
    if changed:
        save_tempbans(d)


@bot.command(help="unwarn <@user> <number>  (remove one warning)")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def unwarn(ctx, member: discord.Member, number: int):
    d = load_warns()
    key = f"{ctx.guild.id}:{member.id}"
    w = d.get(key, [])
    if not 1 <= number <= len(w):
        return await ctx.send(f"❌ {member} has {len(w)} warning(s). Pick 1 to {len(w) or 1}.")
    w.pop(number - 1)
    d[key] = w
    save_warns(d)
    await ctx.send(f"✅ Removed warning #{number} from **{member}**.")


@bot.command(help="banlist  (shows up to 25 bans)")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def banlist(ctx):
    lines = []
    async for entry in ctx.guild.bans(limit=25):
        lines.append(f"`{entry.user.id}` **{entry.user}**: {(entry.reason or 'no reason')[:60]}")
    await ctx.send(embed=emb("🔨 Bans", "\n".join(lines) if lines else "Nobody is banned.", RED))


@bot.command(help="lockdown  (lock every text channel)")
@commands.guild_only()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(manage_channels=True)
async def lockdown(ctx):
    n = 0
    for ch in ctx.guild.text_channels:
        ow = ch.overwrites_for(ctx.guild.default_role)
        ow.send_messages = False
        try:
            await ch.set_permissions(ctx.guild.default_role, overwrite=ow, reason=reason_str(ctx, "lockdown"))
            n += 1
        except discord.HTTPException:
            pass
    await ctx.send(f"🔒 Locked **{n}** channels. Use `unlockdown` to undo.")


@bot.command(help="unlockdown  (unlock every text channel)")
@commands.guild_only()
@commands.has_permissions(administrator=True)
@commands.bot_has_permissions(manage_channels=True)
async def unlockdown(ctx):
    n = 0
    for ch in ctx.guild.text_channels:
        ow = ch.overwrites_for(ctx.guild.default_role)
        ow.send_messages = None
        try:
            await ch.set_permissions(ctx.guild.default_role, overwrite=ow, reason=reason_str(ctx, "unlockdown"))
            n += 1
        except discord.HTTPException:
            pass
    await ctx.send(f"🔓 Unlocked **{n}** channels.")


# ---------------- more utility ----------------
@bot.command(help="roleinfo <@role|name>")
@commands.guild_only()
async def roleinfo(ctx, *, role: discord.Role):
    e = emb(f"🎭 {role.name}", color=role.color.value or BLURPLE)
    e.add_field(name="ID", value=role.id)
    e.add_field(name="Members", value=len(role.members))
    e.add_field(name="Color", value=str(role.color))
    e.add_field(name="Mentionable", value="Yes" if role.mentionable else "No")
    e.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
    await ctx.send(embed=e)


@bot.command(help="channelinfo [#channel]")
@commands.guild_only()
async def channelinfo(ctx, channel: discord.TextChannel = None):
    c = channel or ctx.channel
    e = emb(f"#{c.name}")
    e.add_field(name="ID", value=c.id)
    e.add_field(name="Category", value=c.category.name if c.category else "None")
    e.add_field(name="Slowmode", value=f"{c.slowmode_delay}s")
    e.add_field(name="NSFW", value="Yes" if c.is_nsfw() else "No")
    e.add_field(name="Created", value=discord.utils.format_dt(c.created_at, "R"))
    if c.topic:
        e.add_field(name="Topic", value=c.topic[:300], inline=False)
    await ctx.send(embed=e)


@bot.command(help="remind <duration e.g. 10m> <text>")
async def remind(ctx, duration: str, *, text: str):
    delta = parse_duration(duration)
    await ctx.send(f"⏰ Okay, I'll remind you in **{duration}**.")

    async def _later():
        await asyncio.sleep(delta.total_seconds())
        try:
            await ctx.send(f"⏰ {ctx.author.mention} reminder: {text[:500]}")
        except discord.HTTPException:
            pass
    asyncio.create_task(_later())


@bot.command(help="choose <option1, option2, ...>")
async def choose(ctx, *, options: str):
    opts = [o.strip() for o in options.split(",") if o.strip()]
    if len(opts) < 2:
        return await ctx.send("❌ Give at least 2 options separated by commas.")
    await ctx.send(f"🤔 I pick: **{random.choice(opts)[:200]}**")


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}


def _safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _safe_eval(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(b) > 100 or abs(a) > 1e6):
            raise ValueError("Exponent too big.")
        return _OPS[type(node.op)](a, b)
    raise ValueError("Only numbers and + - * / // % ** ( ) are allowed.")


@bot.command(aliases=["math"], help="calc <expression e.g. 2*(3+4)>")
async def calc(ctx, *, expression: str):
    try:
        result = _safe_eval(ast.parse(expression.strip(), mode="eval").body)
    except ZeroDivisionError:
        return await ctx.send("❌ Can't divide by zero.")
    except (ValueError, SyntaxError, OverflowError) as e:
        return await ctx.send(f"❌ {e}")
    await ctx.send(f"🧮 `{expression[:100]}` = **{result}**")


@bot.command(help="invite  (get the bot's invite link)")
async def invite(ctx):
    perms = discord.Permissions(ban_members=True, kick_members=True, moderate_members=True, manage_messages=True,
                                manage_channels=True, manage_roles=True, manage_nicknames=True, send_messages=True,
                                embed_links=True, read_message_history=True, add_reactions=True, view_channel=True,
                                view_audit_log=True)
    url = discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot", "applications.commands"))
    await ctx.send(f"🔗 {url}")


@bot.command(help="instance  (debug: shows which running copy answered)")
async def instance(ctx):
    await ctx.send(f"🧩 instance `{INSTANCE_ID}` | host `{socket.gethostname()[:20]}` | up {fmt_uptime(time.time() - START)}\n"
                   "If you see two replies with different ids, two copies of the bot are running.")


# ---------------- more fun ----------------
@bot.command(help="rps <rock|paper|scissors>")
async def rps(ctx, choice: str):
    choice = choice.lower()
    if choice not in ("rock", "paper", "scissors"):
        return await ctx.send("❌ Pick rock, paper, or scissors.")
    bot_choice = random.choice(["rock", "paper", "scissors"])
    wins = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
    out = "🤝 Tie!" if choice == bot_choice else ("🎉 You win!" if (choice, bot_choice) in wins else "😈 I win!")
    await ctx.send(f"You: **{choice}** | Me: **{bot_choice}**\n{out}")


@bot.command(help="slots")
async def slots(ctx):
    sym = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
    r = [random.choice(sym) for _ in range(3)]
    out = "🎉 JACKPOT!" if len(set(r)) == 1 else ("✨ Two match!" if len(set(r)) == 2 else "😭 No luck.")
    await ctx.send(f"🎰 | {' | '.join(r)} |\n{out}")


@bot.command(help="roast [@user]")
async def roast(ctx, member: discord.Member = None):
    m = member or ctx.author
    lines = ["has a ping so high NASA is jealous.", "plays with the monitor off for 'extra challenge'.",
             "once lost a 1v0.", "has more tabs open than friends.", "types 'brb' and returns next Tuesday.",
             "thinks 'lag' is a personality."]
    await ctx.send(f"🔥 {m.display_name} {random.choice(lines)}")


# ---------------- security commands ----------------
def _parse_state(text):
    t = text.lower()
    if t in ("on", "enable", "enabled", "true", "yes"):
        return True
    if t in ("off", "disable", "disabled", "false", "no"):
        return False
    return None


def _make_toggle(key, label, owner_only=False):
    @bot.command(name=key, help=f"{key} <on|off>  ({label})")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def _toggle(ctx, state: str):
        val = _parse_state(state)
        if val is None:
            return await ctx.send("❌ Say `on` or `off`.")
        if owner_only and ctx.author.id != ctx.guild.owner_id:
            return await ctx.send("❌ Only the server owner can change this.")
        update_cfg(ctx.guild.id, **{key: val})
        await ctx.send(f"{'✅' if val else '❌'} **{key}** is now **{'ON' if val else 'OFF'}**.")
    return _toggle


_make_toggle("automod", "delete spam, invite links, mass pings, banned words + auto-warn")
_make_toggle("antiraid", "auto-detect mass joins and kick raiders")
_make_toggle("antinuke", "ban anyone who mass-bans/kicks/deletes channels or roles; owner only", owner_only=True)


@bot.command(help="setlog <#channel|off>  (mod-log channel)")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def setlog(ctx, channel: Union[discord.TextChannel, str]):
    if isinstance(channel, str):
        if channel.lower() != "off":
            return await ctx.send("❌ Mention a channel, or say `off`.")
        update_cfg(ctx.guild.id, log_channel=None)
        return await ctx.send("📝 Mod log turned off.")
    try:
        await channel.send(embed=emb("📝 Mod log enabled", "Security and moderation events will appear here.", GREEN))
    except discord.HTTPException:
        return await ctx.send("❌ I can't send messages in that channel.")
    update_cfg(ctx.guild.id, log_channel=channel.id)
    await ctx.send(f"📝 Mod log set to {channel.mention}.")


@bot.command(help="whitelist <add|remove|list> [@user]  (trusted: skips automod + anti-nuke; owner only)")
@commands.guild_only()
async def whitelist(ctx, action: str, user: discord.User = None):
    if ctx.author.id != ctx.guild.owner_id:
        return await ctx.send("❌ Only the server owner can change the whitelist.")
    wl = get_cfg(ctx.guild.id)["whitelist"]
    action = action.lower()
    if action == "list":
        return await ctx.send(embed=emb("🤝 Whitelist", "\n".join(f"<@{u}>" for u in wl) or "Nobody."))
    if action not in ("add", "remove") or user is None:
        return await ctx.send("❌ Usage: `whitelist add @user`, `whitelist remove @user`, or `whitelist list`.")
    if action == "add" and user.id not in wl:
        wl.append(user.id)
    if action == "remove" and user.id in wl:
        wl.remove(user.id)
    update_cfg(ctx.guild.id, whitelist=wl)
    await ctx.send(f"✅ Whitelist updated ({len(wl)} trusted).")


@bot.command(help="raidmode <on|off>  (on = kick everyone who joins; ends by itself in 10 min)")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
@commands.bot_has_permissions(kick_members=True)
async def raidmode(ctx, state: str):
    val = _parse_state(state)
    if val is None:
        return await ctx.send("❌ Say `on` or `off`.")
    if val:
        RAID_UNTIL[ctx.guild.id] = time.time() + RAID_DURATION
        await ctx.send("🚨 Raid mode **ON** for 10 minutes. New joins are kicked. Use `raidmode off` to end it.")
    else:
        RAID_UNTIL.pop(ctx.guild.id, None)
        await ctx.send("✅ Raid mode **OFF**.")


@bot.command(help="welcome <#channel|off>  (greet new members)")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def welcome(ctx, channel: Union[discord.TextChannel, str]):
    if isinstance(channel, str):
        if channel.lower() != "off":
            return await ctx.send("❌ Mention a channel, or say `off`.")
        update_cfg(ctx.guild.id, welcome_channel=None)
        return await ctx.send("👋 Welcome messages off.")
    update_cfg(ctx.guild.id, welcome_channel=channel.id)
    await ctx.send(f"👋 Welcome messages will go to {channel.mention}.")


@bot.command(help="autorole <@role|off>  (role given to every new member)")
@commands.guild_only()
@commands.has_permissions(manage_roles=True)
@commands.bot_has_permissions(manage_roles=True)
async def autorole(ctx, role: Union[discord.Role, str]):
    if isinstance(role, str):
        if role.lower() != "off":
            return await ctx.send("❌ Mention a role, or say `off`.")
        update_cfg(ctx.guild.id, autorole=None)
        return await ctx.send("🏷️ Autorole off.")
    err = _role_check(ctx, role)
    if err:
        return await ctx.send(f"❌ {err}")
    update_cfg(ctx.guild.id, autorole=role.id)
    await ctx.send(f"🏷️ New members will get **{role.name}**.")


@bot.command(help="badword <add|remove|list> [word]  (automod deletes + warns)")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def badword(ctx, action: str, *, word: str = None):
    words = get_cfg(ctx.guild.id)["badwords"]
    action = action.lower()
    if action == "list":
        return await ctx.send(embed=emb("🤬 Banned words", ", ".join(f"||{w}||" for w in words) or "None."))
    if action not in ("add", "remove") or not word:
        return await ctx.send("❌ Usage: `badword add <word>`, `badword remove <word>`, or `badword list`.")
    w = word.strip().lower()[:50]
    if action == "add" and w not in words:
        words.append(w)
    if action == "remove" and w in words:
        words.remove(w)
    update_cfg(ctx.guild.id, badwords=words)
    try:
        await ctx.message.delete()   # hide the word
    except discord.HTTPException:
        pass
    await ctx.send(f"✅ Banned words: **{len(words)}**.")


@bot.command(help="config  (show security settings)")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def config(ctx):
    c = get_cfg(ctx.guild.id)
    on = lambda v: "✅ ON" if v else "❌ OFF"
    e = emb("🛡️ Security settings")
    e.add_field(name="Mod log", value=f"<#{c['log_channel']}>" if c["log_channel"] else "not set")
    e.add_field(name="Automod", value=on(c["automod"]))
    e.add_field(name="Anti-raid", value=on(c["antiraid"]))
    e.add_field(name="Anti-nuke", value=on(c["antinuke"]))
    e.add_field(name="Raid mode now", value="🚨 ACTIVE" if RAID_UNTIL.get(ctx.guild.id, 0) > time.time() else "off")
    e.add_field(name="Welcome", value=f"<#{c['welcome_channel']}>" if c["welcome_channel"] else "off")
    e.add_field(name="Autorole", value=f"<@&{c['autorole']}>" if c["autorole"] else "off")
    e.add_field(name="Prefix", value=f"`{c['prefix']}` or tag me")
    e.add_field(name="Whitelisted", value=len(c["whitelist"]))
    e.add_field(name="Banned words", value=len(c["badwords"]))
    e.add_field(name="Warn limits", value=f"mute at {WARN_MUTE_AT}, kick at {WARN_KICK_AT}")
    await ctx.send(embed=e)


LOGGED_COMMANDS = {"ban", "unban", "softban", "kick", "mute", "unmute", "warn", "unwarn", "clearwarnings", "purge",
                   "tempban", "lock", "unlock", "lockdown", "unlockdown", "addrole", "removerole", "nick",
                   "slowmode", "raidmode", "automod", "antiraid", "antinuke", "whitelist", "prefix"}


@bot.event
async def on_command_completion(ctx):
    if ctx.guild and ctx.command and ctx.command.name in LOGGED_COMMANDS:
        await modlog(ctx.guild, emb("📝 Mod action", f"**{ctx.author}** in {ctx.channel.mention}\n`{ctx.message.content[:400]}`", BLURPLE))


@bot.event
async def on_member_join(member):
    guild, now = member.guild, time.time()
    cfg = get_cfg(guild.id)
    if member.bot:
        await modlog(guild, emb("🤖 Bot added", f"**{member}** ({member.id}) joined the server.", GOLD))
        return
    if RAID_UNTIL.get(guild.id, 0) > now:
        await raid_kick(member, "Raid mode active")
        return
    if cfg["antiraid"]:
        dq = JOINS[guild.id]
        dq.append((now, member))
        while dq and now - dq[0][0] > RAID_WINDOW:
            dq.popleft()
        if len(dq) >= RAID_JOINS:
            RAID_UNTIL[guild.id] = now + RAID_DURATION
            victims = [m for _, m in dq]
            dq.clear()
            kicked = 0
            for v in victims:
                if await raid_kick(v, "Anti-raid: mass join"):
                    kicked += 1
            await modlog(guild, emb("🚨 RAID DETECTED", f"{len(victims)} accounts joined within {RAID_WINDOW}s.\nKicked **{kicked}**. "
                                    f"Raid mode is ON for {RAID_DURATION // 60} minutes (new joins are kicked). Use `raidmode off` to end it.", RED))
            return
    if cfg["autorole"]:
        role = guild.get_role(cfg["autorole"])
        if role:
            try:
                await member.add_roles(role, reason="Autorole")
            except discord.HTTPException:
                pass
    ch = guild.get_channel(cfg["welcome_channel"]) if cfg["welcome_channel"] else None
    if ch:
        try:
            await ch.send(f"👋 Welcome {member.mention} to **{guild.name}**! You're member #{guild.member_count}.")
        except discord.HTTPException:
            pass


@bot.event
async def on_audit_log_entry_create(entry):
    # Anti-nuke: watches the audit log for mass destructive actions.
    if entry.action not in NUKE_ACTIONS or entry.user is None:
        return
    guild = entry.guild
    cfg = get_cfg(guild.id)
    uid = entry.user.id
    if not cfg["antinuke"] or uid == bot.user.id or uid == guild.owner_id or uid in cfg["whitelist"]:
        return
    key, now = (guild.id, uid), time.time()
    dq = NUKE_LOG[key]
    dq.append(now)
    while dq and now - dq[0] > NUKE_WINDOW:
        dq.popleft()
    if len(dq) >= NUKE_LIMIT and now - NUKE_PUNISHED.get(key, 0) > 60:
        NUKE_PUNISHED[key] = now
        dq.clear()
        await nuke_punish(guild, uid, str(entry.action).split(".")[-1])


@bot.command(help="prefix [new]  (show or change the text prefix; tagging me always works)")
@commands.guild_only()
async def prefix(ctx, new: str = None):
    cur = get_cfg(ctx.guild.id)["prefix"]
    if new is None:
        return await ctx.send(f"🔧 My prefix here is `{cur}`  (you can also tag me). Change it with `{cur}prefix <new>`.")
    if not ctx.author.guild_permissions.manage_guild:
        return await ctx.send("❌ You need **Manage Server** to change the prefix.")
    if len(new) > 5 or any(ch.isspace() for ch in new) or "<@" in new:
        return await ctx.send("❌ Prefix must be 1 to 5 characters, no spaces, and no mentions.")
    update_cfg(ctx.guild.id, prefix=new)
    await ctx.send(f"✅ Prefix is now `{new}`. Try `{new}ping`. Tagging me still works too.")


@bot.command(help="help [command]")
async def help(ctx, name: str = None):
    if name:
        c = bot.get_command(name.lower())
        if not c:
            return await ctx.send("❌ No such command.")
        alias = f"  (aliases: {', '.join(c.aliases)})" if c.aliases else ""
        return await ctx.send(f"`{c.help}`{alias}")
    cats = {"🔨 Moderation": ["ban", "unban", "softban", "kick", "mute", "unmute", "warn", "warnings", "unwarn", "clearwarnings",
                              "purge", "slowmode", "lock", "unlock", "nick", "addrole", "removerole",
                              "tempban", "banlist", "lockdown", "unlockdown"],
            "🛡️ Security": ["setlog", "automod", "antiraid", "antinuke", "whitelist", "raidmode", "welcome", "autorole", "badword", "config", "prefix"],
            "ℹ️ Utility": ["userinfo", "serverinfo", "avatar", "ping", "say", "poll",
                          "roleinfo", "channelinfo", "remind", "choose", "calc", "invite", "instance"],
            "🎉 Fun": ["8ball", "coinflip", "roll", "ship", "iq", "ipleak", "bank", "phone", "fakeban", "hack", "rps", "slots", "roast"]}
    p = get_cfg(ctx.guild.id)["prefix"] if ctx.guild else DEFAULT_PREFIX
    e = emb("Commands", f"Use `{p}command` or tag me. Use `{p}help <command>` for usage, e.g. `{p}help ban`.")
    for cat, cmds in cats.items():
        e.add_field(name=cat, value=" ".join(f"`{c}`" for c in cmds), inline=False)
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
async def on_message(message):
    if message.author.bot or already_seen(message.id):
        return
    if await automod(message):
        return
    await bot.process_commands(message)


@bot.event
async def setup_hook():
    tempban_loop.start()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | {len(bot.commands)} commands | instance {INSTANCE_ID}", flush=True)


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
