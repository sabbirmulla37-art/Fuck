"""
Public Discord Bot - everything in one file.
Features : Welcome, Goodbye, Auto Role, Tickets (button / dropdown), Moderation
Commands : work as slash (/) AND prefix (?)
Storage  : data.json (per-server settings)
Run      : pip install -U discord.py  ->  python bot.py
"""
import asyncio
import copy
import io
import json
import logging
import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Literal, Optional

import discord
from discord.ext import commands

# ───────────────────────── ENV ─────────────────────────
BASE = Path(__file__).resolve().parent


def load_env():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "?").strip() or "?"
log = logging.getLogger("bot")

BLURPLE, GREEN, RED = 0x5865F2, 0x57F287, 0xED4245
CLEAR_WORDS = ("none", "off", "clear", "remove", "reset")
PH = "`{user}` `{user.mention}` `{user.name}` `{user.display}` `{user.id}` `{server}` `{count}` `{date}`"
HEX = re.compile(r"^#?[0-9a-fA-F]{6}$")

# ───────────────────────── STORAGE (JSON) ─────────────────────────
DEFAULT_GUILD = {
    "welcome": {
        "enabled": False, "channel": None, "mode": "embed", "dm": False,
        "text": "Welcome {user.mention}! 🎉",
        "title": "Welcome to {server}!",
        "description": "Hey {user.mention}, you are member **#{count}**. Enjoy your stay!",
        "color": "#57F287", "image": None, "thumbnail": True, "footer": "Joined on {date}",
    },
    "goodbye": {
        "enabled": False, "channel": None, "mode": "embed", "dm": False,
        "text": "Goodbye {user.name} 👋",
        "title": "Goodbye!",
        "description": "**{user.name}** has left {server}. We are now **{count}** members.",
        "color": "#ED4245", "image": None, "thumbnail": True, "footer": "Left on {date}",
    },
    "autorole": {"roles": [], "bot_roles": []},
    "ticket": {
        "panel_channel": None, "category": None, "log_channel": None,
        "support_roles": [], "mode": "button", "max_per_user": 1, "counter": 0,
        "dm_transcript": False,
        "panel": {"title": "🎫 Support Tickets",
                  "description": "Need help? Click below to open a private ticket.",
                  "color": "#5865F2", "image": None, "footer": "",
                  "button_label": "Create Ticket", "button_emoji": "🎫"},
        "categories": [{"name": "General Support", "emoji": "❓", "description": "General questions"}],
        "open_message": "Hello {user.mention}! Our team will be with you shortly.\n**Category:** {category}",
        "open": {},
    },
}


def _fill(dst, defaults):
    for k, v in defaults.items():
        if k not in dst:
            dst[k] = copy.deepcopy(v)
        elif isinstance(v, dict) and isinstance(dst[k], dict):
            _fill(dst[k], v)


class Store:
    def __init__(self, path: Path):
        self.path, self.lock, self.data = path, asyncio.Lock(), {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                path.replace(path.with_suffix(".bak"))
                log.warning("data.json was corrupt - backed up to data.bak")

    def get(self, gid) -> dict:
        g = self.data.setdefault(str(gid), {})
        _fill(g, DEFAULT_GUILD)
        return g

    def _write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    async def save(self):
        async with self.lock:
            await asyncio.to_thread(self._write)


store = Store(BASE / "data.json")

# ───────────────────────── HELPERS ─────────────────────────


def render(text, member, guild=None, **extra):
    if not text:
        return ""
    guild = guild or member.guild
    m = {
        "{user}": member.mention, "{user.mention}": member.mention, "{user.name}": member.name,
        "{user.display}": member.display_name, "{user.id}": str(member.id),
        "{server}": guild.name, "{count}": str(guild.member_count),
        "{date}": discord.utils.utcnow().strftime("%d %b %Y"),
    }
    for k, v in extra.items():
        m["{" + k + "}"] = str(v)
    for k, v in m.items():
        text = text.replace(k, v)
    return text


def color_of(v, fallback=BLURPLE):
    try:
        return discord.Color(int(str(v).lstrip("#"), 16))
    except Exception:
        return discord.Color(fallback)


def is_url(s):
    return s.lower().startswith(("http://", "https://"))


def valid_emoji(e):
    return (not e) or bool(re.fullmatch(r"<a?:\w+:\d+>", e)) or (not e.isascii() and len(e) <= 8)


async def say(ctx, text, color=GREEN, ephemeral=False, **kw):
    await ctx.send(embed=discord.Embed(description=text, color=color), ephemeral=ephemeral, **kw)


async def fail(ctx, text):
    await say(ctx, f"❌ {text}", RED, True)


def manage(f):
    """Needs Manage Server, guild only."""
    f = commands.has_permissions(manage_guild=True)(f)
    return commands.guild_only()(f)


async def group_help(ctx):
    g = ctx.command
    rows = [f"`{PREFIX}{g.name} {c.name} {c.signature}`".replace("  ", " ") + f"\n↳ {c.description or c.short_doc}"
            for c in sorted(g.commands, key=lambda x: x.name)]
    await ctx.send(embed=discord.Embed(title=f"/{g.name} commands", description="\n".join(rows)[:4000], color=BLURPLE))


# ───────────────────────── BOT ─────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


# ───────────────────────── TICKET VIEWS ─────────────────────────
def tcfg(guild):
    return store.get(guild.id)["ticket"]


def is_staff(member, c):
    return member.guild_permissions.manage_channels or any(r.id in c["support_roles"] for r in member.roles)


def type_options(c):
    return [discord.SelectOption(label=t["name"][:100], value=t["name"][:100],
                                 description=(t.get("description") or None), emoji=(t.get("emoji") or None))
            for t in c["categories"][:25]]


async def open_ticket(inter: discord.Interaction, cat_name: Optional[str]):
    guild, user = inter.guild, inter.user
    c = tcfg(guild)
    await inter.response.defer(ephemeral=True)
    for cid in [k for k in c["open"] if guild.get_channel(int(k)) is None]:
        c["open"].pop(cid)
    mine = [k for k, t in c["open"].items() if t["owner"] == user.id]
    if len(mine) >= c["max_per_user"]:
        return await inter.followup.send(f"❌ You already have an open ticket: <#{mine[0]}>", ephemeral=True)
    cat_name = cat_name or "General"
    perms = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                        attach_files=True, embed_links=True)
    ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False), user: perms,
          guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True,
                                                manage_messages=True, embed_links=True, read_message_history=True)}
    roles = [r for rid in c["support_roles"] if (r := guild.get_role(rid))]
    for r in roles:
        ow[r] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                            attach_files=True, manage_messages=True)
    parent = guild.get_channel(c["category"]) if c["category"] else None
    c["counter"] += 1
    try:
        ch = await guild.create_text_channel(
            f"ticket-{c['counter']:04d}", category=parent if isinstance(parent, discord.CategoryChannel) else None,
            overwrites=ow, topic=f"Ticket owner: {user} ({user.id}) | {cat_name}")
    except discord.HTTPException as e:
        return await inter.followup.send(f"❌ Couldn't create the ticket channel: {e}", ephemeral=True)
    c["open"][str(ch.id)] = {"owner": user.id, "category": cat_name, "claimed_by": None}
    await store.save()
    e = discord.Embed(title=f"{cat_name} Ticket", color=color_of(c["panel"]["color"]),
                      description=render(c["open_message"], user, guild, category=cat_name))
    e.set_footer(text="Use the buttons below to close or claim this ticket.")
    await ch.send(content=" ".join([user.mention] + [r.mention for r in roles]), embed=e, view=TicketControls(),
                  allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    await inter.followup.send(f"✅ Ticket created: {ch.mention}", ephemeral=True)


async def build_transcript(channel):
    lines = [f"Transcript of #{channel.name} - {channel.guild.name}", ""]
    async for m in channel.history(limit=5000, oldest_first=True):
        body = m.clean_content or ("[embed]" if m.embeds else "")
        body += "".join(f" [attachment: {a.url}]" for a in m.attachments)
        lines.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {m.author}: {body}")
    return "\n".join(lines).encode("utf-8")


async def close_ticket(channel: discord.TextChannel, closer):
    guild = channel.guild
    c = tcfg(guild)
    info = c["open"].pop(str(channel.id), None)
    if not info:
        return
    await store.save()
    data = await build_transcript(channel)
    e = discord.Embed(title="🔒 Ticket closed", color=RED, timestamp=discord.utils.utcnow())
    e.add_field(name="Ticket", value=channel.name)
    e.add_field(name="Opened by", value=f"<@{info['owner']}>")
    e.add_field(name="Closed by", value=closer.mention)
    e.add_field(name="Category", value=info["category"])
    if info.get("claimed_by"):
        e.add_field(name="Claimed by", value=f"<@{info['claimed_by']}>")
    log_ch = guild.get_channel(c["log_channel"]) if c["log_channel"] else None
    targets = []
    if isinstance(log_ch, discord.TextChannel):
        targets.append(log_ch)
    if c["dm_transcript"] and (owner := guild.get_member(info["owner"])):
        targets.append(owner)
    for t in targets:
        try:
            await t.send(embed=e, file=discord.File(io.BytesIO(data), filename=f"{channel.name}.txt"))
        except discord.HTTPException:
            pass
    await channel.send("🔒 Closing this ticket in 5 seconds…")
    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {closer}")
    except discord.HTTPException:
        pass


class ConfirmClose(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=30)

    @discord.ui.button(label="Yes, close", style=discord.ButtonStyle.danger)
    async def yes(self, inter: discord.Interaction, _):
        await inter.response.edit_message(content="Closing…", view=None)
        await close_ticket(inter.channel, inter.user)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, inter: discord.Interaction, _):
        await inter.response.edit_message(content="Cancelled.", view=None)


class TicketControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close_btn(self, inter: discord.Interaction, _):
        c = tcfg(inter.guild)
        info = c["open"].get(str(inter.channel_id))
        if not info:
            return await inter.response.send_message("This is not an active ticket.", ephemeral=True)
        if inter.user.id != info["owner"] and not is_staff(inter.user, c):
            return await inter.response.send_message("You can't close this ticket.", ephemeral=True)
        await inter.response.send_message("Close this ticket?", view=ConfirmClose(), ephemeral=True)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ticket:claim")
    async def claim_btn(self, inter: discord.Interaction, _):
        c = tcfg(inter.guild)
        info = c["open"].get(str(inter.channel_id))
        if not info:
            return await inter.response.send_message("This is not an active ticket.", ephemeral=True)
        if not is_staff(inter.user, c):
            return await inter.response.send_message("Only support staff can claim tickets.", ephemeral=True)
        if info.get("claimed_by"):
            return await inter.response.send_message(f"Already claimed by <@{info['claimed_by']}>.", ephemeral=True)
        info["claimed_by"] = inter.user.id
        await store.save()
        await inter.response.send_message(embed=discord.Embed(
            description=f"🙋 {inter.user.mention} claimed this ticket.", color=GREEN))


class PanelButtonView(discord.ui.View):
    def __init__(self, label="Create Ticket", emoji="🎫"):
        super().__init__(timeout=None)
        b = discord.ui.Button(label=label or None, emoji=emoji or None, style=discord.ButtonStyle.primary,
                              custom_id="ticket:create")
        b.callback = self.clicked
        self.add_item(b)

    async def clicked(self, inter: discord.Interaction):
        await open_ticket(inter, None)


class TicketSelect(discord.ui.Select):
    def __init__(self, options=None):
        super().__init__(custom_id="ticket:select", placeholder="Select a ticket category…",
                         options=options or [discord.SelectOption(label="General", value="General")])

    async def callback(self, inter: discord.Interaction):
        await open_ticket(inter, self.values[0])
        opts = type_options(tcfg(inter.guild))
        if opts:  # reset the dropdown selection
            try:
                await inter.message.edit(view=PanelSelectView(opts))
            except discord.HTTPException:
                pass


class PanelSelectView(discord.ui.View):
    def __init__(self, options=None):
        super().__init__(timeout=None)
        self.add_item(TicketSelect(options))


async def send_panel(guild, channel):
    c = tcfg(guild)
    p = c["panel"]
    e = discord.Embed(title=p["title"] or None, description=p["description"] or None, color=color_of(p["color"]))
    if p["image"]:
        e.set_image(url=p["image"])
    if p["footer"]:
        e.set_footer(text=p["footer"])
    if c["mode"] == "dropdown":
        if not c["categories"]:
            raise ValueError("Dropdown mode needs at least one ticket type (`ticket addtype`).")
        view = PanelSelectView(type_options(c))
    else:
        view = PanelButtonView(p["button_label"], p["button_emoji"])
    return await channel.send(embed=e, view=view)


class PublicBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(PanelButtonView())
        self.add_view(PanelSelectView())
        self.add_view(TicketControls())
        try:
            await self.tree.sync()
        except discord.HTTPException as e:
            log.error("Slash sync failed: %s", e)


bot = PublicBot(
    command_prefix=commands.when_mentioned_or(PREFIX), intents=intents, help_command=None,
    case_insensitive=True,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
)


@bot.event
async def on_ready():
    await bot.change_presence(activity=discord.Game(f"{PREFIX}help | /help"))
    log.info("Logged in as %s in %d servers", bot.user, len(bot.guilds))


# ───────────────────────── WELCOME / GOODBYE ─────────────────────────
def build_greeting(c, member, guild):
    content, embed = None, None
    if c["mode"] in ("text", "both") and c["text"]:
        content = render(c["text"], member, guild)
    if c["mode"] in ("embed", "both"):
        title, desc = render(c["title"], member, guild), render(c["description"], member, guild)
        if title or desc or c["image"]:
            embed = discord.Embed(title=title or None, description=desc or None, color=color_of(c["color"]),
                                  timestamp=discord.utils.utcnow())
            if c["thumbnail"]:
                embed.set_thumbnail(url=member.display_avatar.url)
            if c["image"]:
                embed.set_image(url=c["image"])
            if c["footer"]:
                embed.set_footer(text=render(c["footer"], member, guild))
    return content, embed


async def deliver_greeting(member, kind):
    c = store.get(member.guild.id)[kind]
    if not c["enabled"]:
        return
    content, embed = build_greeting(c, member, member.guild)
    if not (content or embed):
        return
    if c["dm"] and kind == "welcome":
        try:
            await member.send(content=content, embed=embed)
        except discord.HTTPException:
            pass
    ch = member.guild.get_channel(c["channel"]) if c["channel"] else None
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(content=content, embed=embed)
        except discord.HTTPException as e:
            log.warning("Greeting failed in %s: %s", member.guild.id, e)


def make_greet_group(kind: str, label: str):
    @bot.hybrid_group(name=kind, invoke_without_command=True, description=f"Setup the {label} system")
    async def grp(ctx):
        await group_help(ctx)

    def cfg(ctx):
        return store.get(ctx.guild.id)[kind]

    async def put(ctx, key, value, msg):
        cfg(ctx)[key] = value
        await store.save()
        await say(ctx, f"✅ {msg}")

    @grp.command(name="channel", description=f"Set the {label} channel (also enables it)")
    @manage
    async def _channel(ctx, channel: discord.TextChannel):
        cfg(ctx)["enabled"] = True
        await put(ctx, "channel", channel.id, f"{label.title()} channel set to {channel.mention}. System enabled.")

    @grp.command(name="toggle", description=f"Enable or disable {label} messages")
    @manage
    async def _toggle(ctx):
        c = cfg(ctx)
        c["enabled"] = not c["enabled"]
        await store.save()
        await say(ctx, f"✅ {label.title()} is now **{'enabled' if c['enabled'] else 'disabled'}**.")

    @grp.command(name="mode", description="Message style: embed, text or both")
    @manage
    async def _mode(ctx, mode: Literal["embed", "text", "both"]):
        await put(ctx, "mode", mode, f"Mode set to **{mode}**.")

    def add_text(key, desc):
        @grp.command(name=key, description=desc)
        @manage
        async def _cmd(ctx, *, value: str):
            v = "" if value.strip().lower() in CLEAR_WORDS else value
            await put(ctx, key, v, f"`{key}` updated. Placeholders: {PH}")

    add_text("text", "Set the plain text message")
    add_text("title", "Set the embed title")
    add_text("description", "Set the embed description")
    add_text("footer", "Set the embed footer")

    @grp.command(name="color", description="Set embed color (hex, e.g. #5865F2)")
    @manage
    async def _color(ctx, hex_color: str):
        if not HEX.match(hex_color):
            return await fail(ctx, "Use a hex color like `#5865F2`.")
        await put(ctx, "color", "#" + hex_color.lstrip("#"), "Color updated.")

    @grp.command(name="image", description="Set embed banner image URL (or 'none')")
    @manage
    async def _image(ctx, url: str):
        if url.lower() in CLEAR_WORDS:
            return await put(ctx, "image", None, "Image removed.")
        if not is_url(url):
            return await fail(ctx, "Give a valid http(s) image URL, or `none`.")
        await put(ctx, "image", url, "Image updated.")

    @grp.command(name="thumbnail", description="Show the member's avatar as thumbnail on/off")
    @manage
    async def _thumb(ctx, enabled: bool):
        await put(ctx, "thumbnail", enabled, f"Avatar thumbnail **{'on' if enabled else 'off'}**.")

    if kind == "welcome":
        @grp.command(name="dm", description="Also DM the welcome message to new members on/off")
        @manage
        async def _dm(ctx, enabled: bool):
            await put(ctx, "dm", enabled, f"Welcome DM **{'on' if enabled else 'off'}**.")

    @grp.command(name="test", description=f"Preview the {label} message here")
    @manage
    async def _test(ctx):
        content, embed = build_greeting(cfg(ctx), ctx.author, ctx.guild)
        if not (content or embed):
            return await fail(ctx, "Nothing to show - set a text/title/description first.")
        await ctx.send(content=content, embed=embed)

    @grp.command(name="show", description=f"Show current {label} settings")
    @manage
    async def _show(ctx):
        c = cfg(ctx)
        ch = f"<#{c['channel']}>" if c["channel"] else "not set"
        rows = [f"**channel:** {ch}"] + [f"**{k}:** {str(c[k])[:100] or '—'}" for k in
                                         ("enabled", "mode", "color", "thumbnail", "image", "footer", "title",
                                          "description", "text")]
        rows.append(f"\n**Placeholders:** {PH}")
        await ctx.send(embed=discord.Embed(title=f"{label.title()} settings", description="\n".join(rows),
                                           color=BLURPLE))

    @grp.command(name="reset", description=f"Reset {label} settings to default")
    @manage
    async def _reset(ctx):
        store.get(ctx.guild.id)[kind] = copy.deepcopy(DEFAULT_GUILD[kind])
        await store.save()
        await say(ctx, f"✅ {label.title()} settings reset.")


make_greet_group("welcome", "welcome")
make_greet_group("goodbye", "goodbye")


# ───────────────────────── AUTO ROLE ─────────────────────────
async def apply_autoroles(member):
    ar = store.get(member.guild.id)["autorole"]
    ids = ar["bot_roles"] if member.bot else ar["roles"]
    roles = [r for rid in ids if (r := member.guild.get_role(rid)) and r.is_assignable()]
    if roles:
        try:
            await member.add_roles(*roles, reason="Auto role")
        except discord.HTTPException as e:
            log.warning("Autorole failed in %s: %s", member.guild.id, e)


@bot.listen("on_member_join")
async def _join(member):
    await apply_autoroles(member)
    await deliver_greeting(member, "welcome")


@bot.listen("on_member_remove")
async def _leave(member):
    await deliver_greeting(member, "goodbye")


@bot.listen("on_guild_channel_delete")
async def _chan_deleted(channel):
    c = tcfg(channel.guild)
    if c["open"].pop(str(channel.id), None) is not None:
        await store.save()


@bot.hybrid_group(name="autorole", invoke_without_command=True, description="Roles given automatically on join")
async def autorole(ctx):
    await group_help(ctx)


@autorole.command(name="add", description="Add an auto role (for members or bots)")
@manage
async def ar_add(ctx, role: discord.Role, target: Literal["members", "bots"] = "members"):
    if not role.is_assignable():
        return await fail(ctx, "I can't assign that role (it's managed or above my highest role).")
    lst = store.get(ctx.guild.id)["autorole"]["roles" if target == "members" else "bot_roles"]
    if role.id in lst:
        return await fail(ctx, "That role is already an auto role.")
    lst.append(role.id)
    await store.save()
    await say(ctx, f"✅ {role.mention} will be given to new **{target}**.")


@autorole.command(name="remove", description="Remove an auto role")
@manage
async def ar_remove(ctx, role: discord.Role, target: Literal["members", "bots"] = "members"):
    lst = store.get(ctx.guild.id)["autorole"]["roles" if target == "members" else "bot_roles"]
    if role.id not in lst:
        return await fail(ctx, "That role isn't an auto role.")
    lst.remove(role.id)
    await store.save()
    await say(ctx, f"✅ Removed {role.mention} from auto roles ({target}).")


@autorole.command(name="list", description="Show all auto roles")
@manage
async def ar_list(ctx):
    ar = store.get(ctx.guild.id)["autorole"]
    fmt = lambda ids: " ".join(f"<@&{i}>" for i in ids) or "none"
    await ctx.send(embed=discord.Embed(title="Auto roles", color=BLURPLE,
                                       description=f"**Members:** {fmt(ar['roles'])}\n**Bots:** {fmt(ar['bot_roles'])}"))


# ───────────────────────── TICKET COMMANDS ─────────────────────────
@bot.hybrid_group(name="ticket", invoke_without_command=True, description="Ticket system")
async def ticket(ctx):
    await group_help(ctx)


@ticket.command(name="setup", description="Quick setup: panel channel, support role, category, logs")
@manage
async def t_setup(ctx, channel: discord.TextChannel, support_role: discord.Role,
                  category: Optional[discord.CategoryChannel] = None, logs: Optional[discord.TextChannel] = None):
    await ctx.defer()
    c = tcfg(ctx.guild)
    c["panel_channel"] = channel.id
    if support_role.id not in c["support_roles"]:
        c["support_roles"].append(support_role.id)
    if category:
        c["category"] = category.id
    if logs:
        c["log_channel"] = logs.id
    await store.save()
    try:
        await send_panel(ctx.guild, channel)
    except (discord.HTTPException, ValueError) as e:
        return await fail(ctx, f"Saved, but couldn't send the panel: {e}")
    await say(ctx, f"✅ Ticket system ready! Panel sent to {channel.mention}.\n"
                   f"Customize with `{PREFIX}ticket title/description/color/image/button/mode/addtype…`")


@ticket.command(name="panel", description="Send (or resend) the ticket panel")
@manage
async def t_panel(ctx, channel: Optional[discord.TextChannel] = None):
    c = tcfg(ctx.guild)
    channel = channel or (ctx.guild.get_channel(c["panel_channel"]) if c["panel_channel"] else None) or ctx.channel
    try:
        await send_panel(ctx.guild, channel)
    except (discord.HTTPException, ValueError) as e:
        return await fail(ctx, str(e))
    c["panel_channel"] = channel.id
    await store.save()
    await say(ctx, f"✅ Panel sent to {channel.mention}.")


@ticket.command(name="mode", description="Panel style: button or dropdown")
@manage
async def t_mode(ctx, mode: Literal["button", "dropdown"]):
    tcfg(ctx.guild)["mode"] = mode
    await store.save()
    await say(ctx, f"✅ Ticket mode: **{mode}**. Run `{PREFIX}ticket panel` to resend the panel.")


def add_panel_text(key, desc):
    @ticket.command(name=key, description=desc)
    @manage
    async def _cmd(ctx, *, value: str):
        tcfg(ctx.guild)["panel"][key] = "" if value.strip().lower() in CLEAR_WORDS else value
        await store.save()
        await say(ctx, f"✅ Panel {key} updated. Run `{PREFIX}ticket panel` to resend.")


add_panel_text("title", "Set panel title")
add_panel_text("description", "Set panel description")
add_panel_text("footer", "Set panel footer")


@ticket.command(name="color", description="Set panel color (hex)")
@manage
async def t_color(ctx, hex_color: str):
    if not HEX.match(hex_color):
        return await fail(ctx, "Use a hex color like `#5865F2`.")
    tcfg(ctx.guild)["panel"]["color"] = "#" + hex_color.lstrip("#")
    await store.save()
    await say(ctx, "✅ Panel color updated.")


@ticket.command(name="image", description="Set panel image URL (or 'none')")
@manage
async def t_image(ctx, url: str):
    if url.lower() not in CLEAR_WORDS and not is_url(url):
        return await fail(ctx, "Give a valid http(s) URL, or `none`.")
    tcfg(ctx.guild)["panel"]["image"] = None if url.lower() in CLEAR_WORDS else url
    await store.save()
    await say(ctx, "✅ Panel image updated.")


@ticket.command(name="button", description="Set button label and emoji (button mode)")
@manage
async def t_button(ctx, label: str, emoji: str = "🎫"):
    if not valid_emoji(emoji):
        return await fail(ctx, "Invalid emoji.")
    p = tcfg(ctx.guild)["panel"]
    p["button_label"], p["button_emoji"] = label[:80], emoji
    await store.save()
    await say(ctx, "✅ Button updated. Resend the panel to apply.")


@ticket.command(name="message", description="Set the message shown inside new tickets")
@manage
async def t_message(ctx, *, text: str):
    tcfg(ctx.guild)["open_message"] = text
    await store.save()
    await say(ctx, f"✅ Ticket message updated. Placeholders: {PH} `{{category}}`")


@ticket.command(name="parent", description="Discord category where tickets are created")
@manage
async def t_parent(ctx, category: discord.CategoryChannel):
    tcfg(ctx.guild)["category"] = category.id
    await store.save()
    await say(ctx, f"✅ Tickets will be created under **{category.name}**.")


@ticket.command(name="logs", description="Channel for ticket transcripts")
@manage
async def t_logs(ctx, channel: discord.TextChannel):
    tcfg(ctx.guild)["log_channel"] = channel.id
    await store.save()
    await say(ctx, f"✅ Transcripts will be sent to {channel.mention}.")


@ticket.command(name="addrole", description="Add a support role")
@manage
async def t_addrole(ctx, role: discord.Role):
    c = tcfg(ctx.guild)
    if role.id not in c["support_roles"]:
        c["support_roles"].append(role.id)
        await store.save()
    await say(ctx, f"✅ {role.mention} is now a support role.")


@ticket.command(name="removerole", description="Remove a support role")
@manage
async def t_removerole(ctx, role: discord.Role):
    c = tcfg(ctx.guild)
    if role.id in c["support_roles"]:
        c["support_roles"].remove(role.id)
        await store.save()
    await say(ctx, f"✅ {role.mention} removed from support roles.")


@ticket.command(name="limit", description="Max open tickets per user (1-10)")
@manage
async def t_limit(ctx, amount: commands.Range[int, 1, 10]):
    tcfg(ctx.guild)["max_per_user"] = amount
    await store.save()
    await say(ctx, f"✅ Limit set to **{amount}** open ticket(s) per user.")


@ticket.command(name="addtype", description="Add a dropdown ticket category")
@manage
async def t_addtype(ctx, name: str, emoji: str = "🎫", *, description: str = "Open a ticket"):
    c = tcfg(ctx.guild)
    if len(c["categories"]) >= 25:
        return await fail(ctx, "Maximum 25 ticket types.")
    if not valid_emoji(emoji):
        return await fail(ctx, "Invalid emoji.")
    if any(t["name"].lower() == name.lower() for t in c["categories"]):
        return await fail(ctx, "That type already exists.")
    c["categories"].append({"name": name[:100], "emoji": emoji, "description": description[:100]})
    await store.save()
    await say(ctx, f"✅ Added type **{name}**. Resend the panel to apply.")


@ticket.command(name="removetype", description="Remove a dropdown ticket category")
@manage
async def t_removetype(ctx, *, name: str):
    c = tcfg(ctx.guild)
    before = len(c["categories"])
    c["categories"] = [t for t in c["categories"] if t["name"].lower() != name.lower()]
    if len(c["categories"]) == before:
        return await fail(ctx, "No such type.")
    await store.save()
    await say(ctx, f"✅ Removed **{name}**. Resend the panel to apply.")


@ticket.command(name="types", description="List ticket categories")
@manage
async def t_types(ctx):
    c = tcfg(ctx.guild)
    rows = [f"{t['emoji']} **{t['name']}** - {t['description']}" for t in c["categories"]] or ["none"]
    await ctx.send(embed=discord.Embed(title="Ticket types", description="\n".join(rows), color=BLURPLE))


@ticket.command(name="dmtranscript", description="DM transcript to ticket owner on close on/off")
@manage
async def t_dm(ctx, enabled: bool):
    tcfg(ctx.guild)["dm_transcript"] = enabled
    await store.save()
    await say(ctx, f"✅ Transcript DM **{'on' if enabled else 'off'}**.")


def ticket_info(ctx):
    return tcfg(ctx.guild)["open"].get(str(ctx.channel.id))


@ticket.command(name="close", description="Close the current ticket")
@commands.guild_only()
async def t_close(ctx):
    c, info = tcfg(ctx.guild), ticket_info(ctx)
    if not info:
        return await fail(ctx, "This isn't a ticket channel.")
    if ctx.author.id != info["owner"] and not is_staff(ctx.author, c):
        return await fail(ctx, "You can't close this ticket.")
    await say(ctx, "🔒 Closing ticket…")
    await close_ticket(ctx.channel, ctx.author)


@ticket.command(name="add", description="Add a member to this ticket")
@commands.guild_only()
async def t_add(ctx, member: discord.Member):
    if not ticket_info(ctx):
        return await fail(ctx, "This isn't a ticket channel.")
    if not is_staff(ctx.author, tcfg(ctx.guild)):
        return await fail(ctx, "Only support staff can do that.")
    await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    await say(ctx, f"✅ Added {member.mention}.")


@ticket.command(name="remove", description="Remove a member from this ticket")
@commands.guild_only()
async def t_remove(ctx, member: discord.Member):
    info = ticket_info(ctx)
    if not info:
        return await fail(ctx, "This isn't a ticket channel.")
    if not is_staff(ctx.author, tcfg(ctx.guild)):
        return await fail(ctx, "Only support staff can do that.")
    if member.id == info["owner"]:
        return await fail(ctx, "You can't remove the ticket owner.")
    await ctx.channel.set_permissions(member, overwrite=None)
    await say(ctx, f"✅ Removed {member.mention}.")


# ───────────────────────── MODERATION ─────────────────────────
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(s):
    s = s.lower().replace(" ", "")
    if not re.fullmatch(r"(\d+[smhdw])+", s):
        return None
    total = sum(int(n) * UNITS[u] for n, u in re.findall(r"(\d+)([smhdw])", s))
    return total if 0 < total <= 28 * 86400 else None


def check_target(ctx, target: discord.Member):
    g = ctx.guild
    if target.id == ctx.author.id:
        return "You can't do that to yourself."
    if target.id == g.owner_id:
        return "You can't do that to the server owner."
    if target.id == g.me.id:
        return "I can't do that to myself."
    if ctx.author.id != g.owner_id and target.top_role >= ctx.author.top_role:
        return "That member's top role is equal to or higher than yours."
    if target.top_role >= g.me.top_role:
        return "That member's top role is equal to or higher than mine."


async def notify(target, guild, action, reason, extra=""):
    try:
        await target.send(embed=discord.Embed(title=f"You were {action} in {guild.name}",
                                              description=f"**Reason:** {reason}{extra}", color=RED))
    except discord.HTTPException:
        pass


async def mod_done(ctx, title, target, reason, color, extra=None):
    e = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    e.add_field(name="User", value=f"{target} (`{target.id}`)")
    e.add_field(name="Moderator", value=ctx.author.mention)
    if extra:
        e.add_field(name="Duration", value=extra)
    e.add_field(name="Reason", value=reason, inline=False)
    await ctx.send(embed=e)


def audit(ctx, reason):
    return f"{ctx.author} ({ctx.author.id}): {reason}"[:500]


@bot.hybrid_command(name="kick", description="Kick a member from the server")
@commands.guild_only()
@commands.has_permissions(kick_members=True)
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    if (p := check_target(ctx, member)):
        return await fail(ctx, p)
    await notify(member, ctx.guild, "kicked", reason)
    await member.kick(reason=audit(ctx, reason))
    await mod_done(ctx, "👢 Member kicked", member, reason, 0xE67E22)


@bot.hybrid_command(name="ban", description="Ban a user from the server")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx, user: discord.User, *, reason: str = "No reason provided"):
    member = ctx.guild.get_member(user.id)
    if member:
        if (p := check_target(ctx, member)):
            return await fail(ctx, p)
        await notify(member, ctx.guild, "banned", reason)
    elif user.id == ctx.author.id:
        return await fail(ctx, "You can't do that to yourself.")
    await ctx.guild.ban(user, reason=audit(ctx, reason), delete_message_seconds=0)
    await mod_done(ctx, "🔨 User banned", user, reason, RED)


@bot.hybrid_command(name="unban", description="Unban a user by ID")
@commands.guild_only()
@commands.has_permissions(ban_members=True)
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx, user: discord.User, *, reason: str = "No reason provided"):
    try:
        await ctx.guild.unban(user, reason=audit(ctx, reason))
    except discord.NotFound:
        return await fail(ctx, "That user isn't banned.")
    await mod_done(ctx, "✅ User unbanned", user, reason, GREEN)


@bot.hybrid_command(name="timeout", description="Timeout a member (e.g. 10m, 2h, 1d, 1h30m)")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def timeout(ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    secs = parse_duration(duration)
    if secs is None:
        return await fail(ctx, "Invalid duration. Examples: `10m`, `2h`, `1d`, `1h30m` (max 28d).")
    if (p := check_target(ctx, member)):
        return await fail(ctx, p)
    if member.guild_permissions.administrator:
        return await fail(ctx, "Administrators can't be timed out.")
    td = timedelta(seconds=secs)
    await member.timeout(td, reason=audit(ctx, reason))
    until = discord.utils.format_dt(discord.utils.utcnow() + td, "R")
    await notify(member, ctx.guild, "timed out", reason, f"\n**Ends:** {until}")
    await mod_done(ctx, "⏳ Member timed out", member, reason, 0xF1C40F, extra=f"{duration} (ends {until})")


@bot.hybrid_command(name="untimeout", description="Remove a member's timeout")
@commands.guild_only()
@commands.has_permissions(moderate_members=True)
@commands.bot_has_permissions(moderate_members=True)
async def untimeout(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    await member.timeout(None, reason=audit(ctx, reason))
    await mod_done(ctx, "✅ Timeout removed", member, reason, GREEN)


@bot.hybrid_command(name="purge", description="Delete recent messages (1-100)")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purge(ctx, amount: commands.Range[int, 1, 100]):
    await ctx.defer(ephemeral=True)
    prefix_mode = ctx.interaction is None
    deleted = await ctx.channel.purge(limit=amount + (1 if prefix_mode else 0))
    n = len(deleted) - (1 if prefix_mode else 0)
    kw = {"delete_after": 5} if prefix_mode else {"ephemeral": True}
    await ctx.send(embed=discord.Embed(description=f"🧹 Deleted **{n}** message(s).", color=GREEN), **kw)


# ───────────────────────── GENERAL ─────────────────────────
@bot.hybrid_command(name="ping", description="Check bot latency")
async def ping(ctx):
    await say(ctx, f"🏓 Pong! **{round(bot.latency * 1000)} ms**", BLURPLE)


@bot.hybrid_command(name="help", description="Show all commands")
async def help_cmd(ctx):
    p = PREFIX
    e = discord.Embed(title="📖 Bot Help", color=BLURPLE,
                      description=f"Every command works as **/command** and **{p}command**.\n"
                                  f"Run `{p}welcome`, `{p}goodbye`, `{p}autorole` or `{p}ticket` to see their sub-commands.")
    e.add_field(name="👋 Welcome / Goodbye",
                value="`channel` `toggle` `mode` `text` `title` `description` `color` `image` `thumbnail` "
                      "`footer` `dm`(welcome) `test` `show` `reset`", inline=False)
    e.add_field(name="🎭 Auto role", value="`autorole add/remove/list`", inline=False)
    e.add_field(name="🎫 Tickets",
                value="`ticket setup` `panel` `mode` `title` `description` `color` `image` `button` `message` "
                      "`parent` `logs` `addrole` `removerole` `limit` `addtype` `removetype` `types` "
                      "`dmtranscript` `close` `add` `remove`", inline=False)
    e.add_field(name="🛡️ Moderation", value="`kick` `ban` `unban` `timeout` `untimeout` `purge`", inline=False)
    e.add_field(name="Placeholders", value=PH, inline=False)
    await ctx.send(embed=e)


# ───────────────────────── ERRORS ─────────────────────────
@bot.event
async def on_command_error(ctx, error):
    while hasattr(error, "original") and error.original is not None:
        error = error.original
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        return await fail(ctx, "You need: " + ", ".join(p.replace("_", " ").title() for p in error.missing_permissions))
    if isinstance(error, commands.BotMissingPermissions):
        return await fail(ctx, "I need: " + ", ".join(p.replace("_", " ").title() for p in error.missing_permissions))
    if isinstance(error, commands.MissingRequiredArgument):
        return await fail(ctx, f"Missing argument `{error.param.name}`. Usage: "
                               f"`{PREFIX}{ctx.command.qualified_name} {ctx.command.signature}`")
    if isinstance(error, (commands.BadArgument, commands.BadUnionArgument, commands.RangeError)):
        return await fail(ctx, str(error))
    if isinstance(error, commands.NoPrivateMessage):
        return await fail(ctx, "This command only works in servers.")
    if isinstance(error, commands.CheckFailure):
        return await fail(ctx, "You can't use this command.")
    if isinstance(error, discord.Forbidden):
        return await fail(ctx, "I don't have permission to do that (check my role position/permissions).")
    log.exception("Unhandled command error", exc_info=error)
    await fail(ctx, "Something went wrong. Please try again.")


if __name__ == "__main__":
    if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        sys.exit("❌ Put your bot token in the .env file (DISCORD_TOKEN=...)")
    bot.run(TOKEN)
