import asyncio
import io
import json
import os
import re
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_PATH = Path(__file__).with_name("ticket_config.json")
PAINT_OPTIONS_PATH = Path(__file__).with_name("base_paint_options.json")
TICKET_CONFIGS: dict[str, dict] = {}
TICKET_CREATION_LOCKS: dict[int, asyncio.Lock] = {}
TICKET_WAITING_MESSAGE = (
    "Please send photos of your payment and collateral. Thank you!"
)
TICKET_EMBED_COLOR = discord.Color.from_rgb(46, 204, 113)
POSTER_SUBMIT_ROLE_ID = 1474481893809586206
POSTER_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def load_ticket_configs() -> None:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            saved_configs = json.load(config_file)
        if not isinstance(saved_configs, dict):
            raise ValueError("ticket_config.json must contain a JSON object.")
        TICKET_CONFIGS.update(saved_configs)


def save_ticket_configs() -> None:
    temporary_path = CONFIG_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as config_file:
        json.dump(TICKET_CONFIGS, config_file, indent=2)
    os.replace(temporary_path, CONFIG_PATH)


def poster_path_for(
    guild_id: int, paint_slug: str, extension: str, system: str = "stb"
) -> Path:
    return (
        CONFIG_PATH.parent
        / "posters"
        / str(guild_id)
        / system
        / f"{paint_slug}{extension}"
    )


def legacy_stb_poster_path(guild_id: int, paint_slug: str, extension: str) -> Path:
    return (
        CONFIG_PATH.parent
        / "posters"
        / str(guild_id)
        / f"{paint_slug}{extension}"
    )


def valid_poster_extension(content_type: str | None, data: bytes) -> str | None:
    normalized_type = (content_type or "").lower().split(";", maxsplit=1)[0].strip()
    extension = POSTER_IMAGE_EXTENSIONS.get(normalized_type)
    signatures = {
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".gif": data.startswith((b"GIF87a", b"GIF89a")),
        ".webp": (
            len(data) >= 12
            and data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
        ),
    }
    if extension is not None:
        return extension if signatures[extension] else None
    return next(
        (candidate for candidate, matches in signatures.items() if matches), None
    )


def make_panel_embed(image_url: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="__Base Indexing Tickets__",
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**Base indexing ticket rules:**\n\n"
            "**Pricing and Collateral will automatically be sent in the ticket.**\n\n"
            "- Please follow all forms of payment set up by the index giver, if this rule isn't followed a 7 day timeout will be handed out, a 2nd offense will be a ban.\n"
            "- Please don't @painters in tickets over and over again. It isn't needed and only annoys the painters.\n"
            "- Collateral is required for all indexing sessions no matter what, if you don't have sufficient collateral the ticket will be closed.\n"
            "- Be ready and aware, our painters work hard and have real lives, please respond to them whenever they ask questions.\n"
            "- Please be respectful, our painters will show you the same respect that you show them."
        ),
        color=TICKET_EMBED_COLOR,
    )
    if image_url:
        embed.set_image(url=image_url)
    return embed


def make_ticket_rules_embed() -> discord.Embed:
    return discord.Embed(
        title="Base indexing ticket rules",
        description=(
            "**Pricing and Collateral will automatically be sent in the ticket.**\n\n"
            "- Please follow all forms of payment set up by the index giver, if this rule isn't followed a 7 day timeout will be handed out, a 2nd offense will be a ban.\n"
            "- Please don't @painters in tickets over and over again. It isn't needed and only annoys the painters.\n"
            "- Collateral is required for all indexing sessions no matter what, if you don't have sufficient collateral the ticket will be closed.\n"
            "- Be ready and aware, our painters work hard and have real lives, please respond to them whenever they ask questions.\n"
            "- Please be respectful, our painters will show you the same respect that you show them."
        ),
        color=TICKET_EMBED_COLOR,
    )


def make_ticket_answers_embed(
    paint_slug: str,
    total_items: str,
    total_payment: str,
    collateral: str,
    system: str = "stb",
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{PAINT_SYSTEMS[system]['label']} Base Paint Request",
        color=TICKET_EMBED_COLOR,
    )
    embed.add_field(name="Base paint", value=PAINT_LABELS[system][paint_slug], inline=False)
    embed.add_field(
        name="How many total items do you need?",
        value=total_items,
        inline=False,
    )
    embed.add_field(
        name="What will be your total payment?",
        value=total_payment,
        inline=False,
    )
    embed.add_field(
        name="What do you have as collateral?",
        value=collateral,
        inline=False,
    )
    return embed


def owner_id_from_topic(topic: str | None) -> int | None:
    match = re.search(r"(?:^|\|)\s*owner_id=(\d+)", topic or "")
    return int(match.group(1)) if match else None


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="STB Base Paint Ticket",
        style=discord.ButtonStyle.success,
        custom_id="basepaint:create_ticket",
    )
    async def create_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Tickets can only be opened from a server.", ephemeral=True
            )
            return

        config = TICKET_CONFIGS.get(str(guild.id))
        if config is None:
            await interaction.response.send_message(
                "Ticket setup is incomplete. Please ask a server administrator to run `/ticket_setupbp`.",
                ephemeral=True,
            )
            return

        category = guild.get_channel(int(config["category_id"]))
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "The configured ticket category is no longer available. Please ask an administrator to run `/ticket_setupbp` again.",
                ephemeral=True,
            )
            return

        existing_ticket = next(
            (
                channel
                for channel in category.text_channels
                if owner_id_from_topic(channel.topic) == member.id
            ),
            None,
        )
        if existing_ticket is not None:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing_ticket.mention}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        for role_id in config.get("support_role_ids", []):
            role = guild.get_role(int(role_id))
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    manage_messages=True,
                )
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            )

        safe_name = re.sub(r"[^a-z0-9-]+", "-", member.display_name.lower()).strip("-")
        channel_name = f"base-indexing-{safe_name or member.id}"[:100]
        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"Base indexing ticket | owner_id={member.id}",
                reason=f"Base indexing ticket opened by {member}",
            )
        except discord.HTTPException as error:
            await interaction.followup.send(
                f"I couldn't create the ticket. Check that I have permission to manage channels. Details: `{error}`",
                ephemeral=True,
            )
            return

        try:
            await ticket_channel.send(
                "Thanks for opening a base indexing ticket.",
                view=TicketCloseView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            try:
                await ticket_channel.delete(
                    reason="Ticket setup message could not be sent."
                )
            except discord.HTTPException as cleanup_error:
                await interaction.followup.send(
                    f"The ticket channel {ticket_channel.mention} was created, but I couldn't send its setup message or remove the channel. Details: `{error}`; cleanup error: `{cleanup_error}`",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"I couldn't send the ticket's setup message, so I removed the new channel. Details: `{error}`",
                    ephemeral=True,
                )
            return

        await interaction.followup.send(
            f"Your ticket is ready: {ticket_channel.mention}", ephemeral=True
        )


class TicketCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close ticket",
        style=discord.ButtonStyle.danger,
        custom_id="basepaint:close_ticket",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        channel = interaction.channel
        guild = interaction.guild
        member = interaction.user
        if (
            guild is None
            or not isinstance(channel, discord.TextChannel)
            or not isinstance(member, discord.Member)
        ):
            await interaction.response.send_message(
                "This button can only be used in a server ticket channel.",
                ephemeral=True,
            )
            return

        config = TICKET_CONFIGS.get(str(guild.id))
        owner_id = owner_id_from_topic(channel.topic)
        is_support = bool(
            config
            and any(
                role.id in {int(role_id) for role_id in config.get("support_role_ids", [])}
                for role in member.roles
            )
        )
        if owner_id is None or (member.id != owner_id and not is_support):
            await interaction.response.send_message(
                "Only the ticket opener or a configured support role can close this ticket.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if config and config.get("transcripts_enabled", True):
            transcript_channel_id = config.get("transcript_channel_id")
            if transcript_channel_id is None:
                transcript_channel_id = config.get("panel_channel_id")
            transcript_channel = guild.get_channel(int(transcript_channel_id))
            if not isinstance(transcript_channel, discord.TextChannel):
                await interaction.followup.send(
                    "The transcript channel is unavailable, so I left the ticket open. Please ask an administrator to fix the ticket setup.",
                    ephemeral=True,
                )
                return

            transcript_lines = [
                f"Transcript for #{channel.name}",
                f"Opened by user ID: {owner_id}",
                f"Closed by: {member} (ID: {member.id})",
                "",
            ]
            try:
                messages = [
                    message
                    async for message in channel.history(limit=1000, oldest_first=True)
                ]
            except discord.HTTPException as error:
                await interaction.followup.send(
                    f"I couldn't read the ticket history, so I left the ticket open. Check my permissions in this channel. Details: `{error}`",
                    ephemeral=True,
                )
                return
            for message in messages:
                timestamp = message.created_at.astimezone(timezone.utc).isoformat()
                transcript_lines.append(
                    f"[{timestamp}] {message.author} (ID: {message.author.id}):"
                )
                if message.content:
                    transcript_lines.append(message.content)
                transcript_lines.extend(
                    f"Attachment: {attachment.url}"
                    for attachment in message.attachments
                )
                for embed in message.embeds:
                    if embed.title:
                        transcript_lines.append(f"Embed title: {embed.title}")
                    if embed.description:
                        transcript_lines.append(f"Embed: {embed.description}")
                transcript_lines.append("")

            transcript = "\n".join(transcript_lines).encode("utf-8")
            filename = f"transcript-{channel.name}.txt"
            try:
                await transcript_channel.send(
                    content=f"Transcript for **#{channel.name}**.",
                    file=discord.File(io.BytesIO(transcript), filename=filename),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as error:
                await interaction.followup.send(
                    f"I couldn't save the transcript, so I left the ticket open. Check my permissions in {transcript_channel.mention}. Details: `{error}`",
                    ephemeral=True,
                )
                return

        try:
            await channel.delete(reason=f"Ticket closed by {member}")
        except discord.HTTPException as error:
            await interaction.followup.send(
                f"The ticket could not be deleted. Details: `{error}`",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Ticket closed and transcript saved." if config and config.get("transcripts_enabled", True)
            else "Ticket closed.",
            ephemeral=True,
        )


def base_paint_from_topic(topic: str | None) -> str | None:
    match = re.search(r"(?:^|\|)\s*basepaint=([a-z0-9-]+)", topic or "")
    return match.group(1) if match else None


def member_can_close_ticket(
    member: discord.Member, channel: discord.TextChannel, config: dict | None
) -> bool:
    support_role_ids = {
        int(role_id) for role_id in (config or {}).get("support_role_ids", [])
    }
    return owner_id_from_topic(channel.topic) == member.id or any(
        role.id in support_role_ids for role in member.roles
    )


BASE_PAINT_OPTIONS = (
    ("gold", "🏆 Gold"),
    ("diamond", "💎 Diamond"),
    ("galaxy", "🌌 Galaxy"),
    ("rainbow", "🌈 Rainbow"),
    ("zombie", "🧟 Zombie"),
    ("dreamy", "💤 Dreamy"),
    ("ice-n-fire", "🔥 Ice-n-fire"),
    ("carnival", "🎪 Carnival"),
    ("aqua", "🌊 Aqua"),
    ("neon", "👾 Neon"),
    ("og-halloween", "🎃 Og-halloween"),
    ("chocolate", "🍫 Chocolate"),
    ("lovely", "💗 Lovely"),
    ("toxic", "🧪 Toxic"),
    ("darkness", "🕷️ Darkness"),
    ("christmas", "🎅 Christmas"),
    ("heaven", "🌤️ Heaven"),
    ("crystal", "🔮 Crystal"),
    ("gothic", "⛪ Gothic"),
    ("football", "⚽ Football"),
    ("void", "🥏 Void"),
    ("magical", "🪄 Magical"),
    ("summer", "🌞 Summer"),
    ("rave", "🪩 Rave"),
    ("jungle", "🪴 Jungle"),
    ("spooky", "👻 Spooky"),
)
GOUP_PAINT_OPTIONS = (
    ("default", "📦 Default"),
    ("silver", "🥈 Silver"),
    ("gold", "🥇 Gold"),
    ("crystal", "🔮 Crystal"),
    ("bloodmoon", "🩸 Bloodmoon"),
    ("rainbow", "🦄 Rainbow"),
    ("candy", "🍭 Candy"),
    ("cyber", "🦾 Cyber"),
    ("party", "🥳 Party"),
    ("black-n-white", "🖤 Black-n-white"),
    ("aqua", "🌊 Aqua"),
    ("lava", "🍬 Lava"),
    ("football", "⚽ Football"),
    ("summer", "🌞 Summer"),
    ("dreamy", "💤 Dreamy"),
    ("galaxy", "🌌 Galaxy"),
    ("radioactive", "☢️ Radioactive"),
    ("halloween", "🎃 Halloween"),
)
PAINT_SYSTEMS = {
    "stb": {"label": "STB", "options": list(BASE_PAINT_OPTIONS)},
    "goup": {"label": "Go-Up", "options": list(GOUP_PAINT_OPTIONS)},
}
CUSTOM_PAINT_OPTIONS: dict[str, list[tuple[str, str]]] = {
    system: [] for system in PAINT_SYSTEMS
}


def load_custom_paint_options() -> None:
    if not PAINT_OPTIONS_PATH.exists():
        return

    with PAINT_OPTIONS_PATH.open("r", encoding="utf-8") as options_file:
        saved_options = json.load(options_file)
    if not isinstance(saved_options, dict):
        raise ValueError("base_paint_options.json must contain a JSON object.")

    for system, options in saved_options.items():
        if system not in PAINT_SYSTEMS or not isinstance(options, list):
            raise ValueError("base_paint_options.json contains an invalid paint system.")
        for option in options:
            if (
                not isinstance(option, list)
                or len(option) != 2
                or not all(isinstance(value, str) for value in option)
            ):
                raise ValueError("base_paint_options.json contains an invalid paint.")
            paint_slug, paint_label = option
            if (
                re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", paint_slug) is None
                or not paint_label
                or len(paint_label) > 100
                or any(
                    existing_slug == paint_slug
                    for existing_slug, _ in PAINT_SYSTEMS[system]["options"]
                )
            ):
                raise ValueError("base_paint_options.json contains a duplicate or invalid paint.")
            PAINT_SYSTEMS[system]["options"].append((paint_slug, paint_label))
            CUSTOM_PAINT_OPTIONS[system].append((paint_slug, paint_label))


def save_custom_paint_options(
    custom_options: dict[str, list[tuple[str, str]]],
) -> None:
    temporary_path = PAINT_OPTIONS_PATH.with_suffix(".json.tmp")
    with temporary_path.open("w", encoding="utf-8") as options_file:
        json.dump(
            {
                system: [list(option) for option in options]
                for system, options in custom_options.items()
            },
            options_file,
            indent=2,
            ensure_ascii=False,
        )
    os.replace(temporary_path, PAINT_OPTIONS_PATH)


load_custom_paint_options()
PAINT_LABELS = {
    system: dict(data["options"]) for system, data in PAINT_SYSTEMS.items()
}
PAINT_ORDERS = {
    system: {
        paint_slug: index
        for index, (paint_slug, _) in enumerate(data["options"])
    }
    for system, data in PAINT_SYSTEMS.items()
}
BASE_PAINT_LABELS = PAINT_LABELS["stb"]
BASE_PAINT_ORDER = PAINT_ORDERS["stb"]
NO_TICKET_ACCESS_ROLE_IDS = {1429489447292109022, 1483862223435862076}


def ticket_system_from_topic(topic: str | None) -> str:
    match = re.search(r"(?:^|\|)\s*ticket_system=(stb|go-up|goup)", topic or "")
    if match is None:
        return "stb"
    return "stb" if match.group(1) == "stb" else "goup"


def ticket_channel_sort_key(
    channel: discord.TextChannel,
) -> tuple[int, int, int]:
    match = re.fullmatch(
        r"(?:(stb|go-up|goup)-)?ticket-([a-z0-9-]+)-(\d+)",
        channel.name,
        flags=re.IGNORECASE,
    )
    if match is None:
        return len(PAINT_SYSTEMS), 0, 0
    system = (match.group(1) or "stb").lower()
    if system == "go-up":
        system = "goup"
    paint_slug = match.group(2).lower()
    system_order = list(PAINT_SYSTEMS).index(system)
    paint_order = PAINT_ORDERS[system].get(
        paint_slug, len(PAINT_ORDERS[system])
    )
    return system_order, paint_order, int(match.group(3))


async def organize_ticket_channels(category: discord.CategoryChannel) -> None:
    ticket_channels = [
        channel
        for channel in category.text_channels
        if ticket_channel_sort_key(channel)[0] < len(PAINT_SYSTEMS)
    ]
    ticket_channels.sort(key=ticket_channel_sort_key)
    if not ticket_channels:
        return

    first_position = min(channel.position for channel in ticket_channels)
    for offset, channel in enumerate(ticket_channels):
        target_position = first_position + offset
        if channel.position != target_position:
            await channel.edit(
                position=target_position,
                reason="Keep STB and Go-Up tickets grouped by system, paint, and number",
            )


class BasePaintDropdown(discord.ui.Select):
    def __init__(
        self,
        paints: tuple[tuple[str, str], ...],
        page: int,
        page_count: int,
        system: str,
    ) -> None:
        placeholder = f"Choose a {PAINT_SYSTEMS[system]['label']} base paint"
        if page_count > 1:
            placeholder += f" ({page}/{page_count})"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=slug)
                for slug, label in paints
            ],
            row=page - 1,
        )
        self.system = system

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            BasePaintQuestionsModal(self.values[0], self.system)
        )


class BasePaintQuestionsModal(discord.ui.Modal):
    def __init__(self, paint_slug: str, system: str) -> None:
        labels = PAINT_LABELS[system]
        system_label = PAINT_SYSTEMS[system]["label"]
        super().__init__(title=f"{system_label} {labels[paint_slug]} ticket details")
        self.paint_slug = paint_slug
        self.system = system
        self.total_items = discord.ui.TextInput(
            label="How many total items do you need?",
            placeholder="Enter the total number of items",
            max_length=500,
            required=True,
        )
        self.total_payment = discord.ui.TextInput(
            label="What will be your total payment?",
            placeholder="Enter your total payment",
            max_length=500,
            required=True,
        )
        self.collateral = discord.ui.TextInput(
            label="What do you have as collateral?",
            placeholder="List the collateral you can provide",
            max_length=500,
            required=True,
        )
        self.add_item(self.total_items)
        self.add_item(self.total_payment)
        self.add_item(self.collateral)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await create_ticket_for_paint(
            interaction,
            self.paint_slug,
            system=self.system,
            total_items=self.total_items.value,
            total_payment=self.total_payment.value,
            collateral=self.collateral.value,
        )


class BasePaintPickerView(discord.ui.View):
    def __init__(self, system: str = "stb") -> None:
        super().__init__(timeout=180)
        paints = PAINT_SYSTEMS[system]["options"]
        page_count = (len(paints) + 24) // 25
        page_size = (len(paints) + page_count - 1) // page_count
        for page in range(page_count):
            start = page * page_size
            self.add_item(
                BasePaintDropdown(
                    paints[start : start + page_size],
                    page + 1,
                    page_count,
                    system,
                )
            )


async def create_ticket_for_paint(
    interaction: discord.Interaction,
    paint_slug: str,
    *,
    system: str = "stb",
    total_items: str,
    total_payment: str,
    collateral: str,
) -> None:
    guild = interaction.guild
    member = interaction.user
    config = TICKET_CONFIGS.get(str(guild.id)) if guild else None
    if (
        guild is None
        or not isinstance(member, discord.Member)
        or config is None
        or system not in PAINT_SYSTEMS
    ):
        await interaction.edit_original_response(
            content="Ticket setup is unavailable. Please ask an administrator to run `/ticket_setupbp`.",
            view=None,
        )
        return

    category = guild.get_channel(int(config["category_id"]))
    if not isinstance(category, discord.CategoryChannel):
        await interaction.edit_original_response(
            content="The configured ticket category is unavailable. Ask an administrator to run `/ticket_setupbp` again.",
            view=None,
        )
        return
    paint_labels = PAINT_LABELS[system]
    paint_order = PAINT_ORDERS[system]
    system_label = PAINT_SYSTEMS[system]["label"]
    if paint_slug not in paint_labels:
        await interaction.edit_original_response(
            content="That base paint selection is invalid. Please use the ticket panel again.",
            view=None,
        )
        return

    poster_settings = config.get("posters", {})
    poster_extension = poster_settings.get(f"{system}:{paint_slug}")
    if poster_extension is None and system == "stb":
        poster_extension = poster_settings.get(paint_slug)
    poster_bytes: bytes | None = None
    poster_filename: str | None = None
    if poster_extension is not None:
        if poster_extension not in set(POSTER_IMAGE_EXTENSIONS.values()):
            await interaction.edit_original_response(
                content=f"The saved {paint_labels[paint_slug]} poster has an invalid file type. Ask an administrator to submit it again.",
                view=None,
            )
            return
        poster_path = poster_path_for(
            guild.id, paint_slug, poster_extension, system
        )
        if system == "stb" and not poster_path.exists():
            legacy_path = legacy_stb_poster_path(
                guild.id, paint_slug, poster_extension
            )
            if legacy_path.exists():
                poster_path = legacy_path
        try:
            poster_bytes = poster_path.read_bytes()
        except OSError as error:
            await interaction.edit_original_response(
                content=f"The saved {paint_labels[paint_slug]} poster is unavailable, so I couldn't create the ticket. Ask an administrator to submit it again. Details: `{error}`",
                view=None,
            )
            return
        poster_filename = f"{system}-{paint_slug}-poster{poster_extension}"

    lock = TICKET_CREATION_LOCKS.setdefault(guild.id, asyncio.Lock())
    async with lock:
        existing = next(
            (
                channel
                for channel in category.text_channels
                if owner_id_from_topic(channel.topic) == member.id
                and base_paint_from_topic(channel.topic) == paint_slug
                and ticket_system_from_topic(channel.topic) == system
            ),
            None,
        )
        if existing:
            await interaction.edit_original_response(
                content=f"You already have an open {system_label} {paint_labels[paint_slug]} ticket: {existing.mention}",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if len(category.channels) >= 50:
            await interaction.edit_original_response(
                content=(
                    f"The configured ticket category ({category.name}) reached "
                    "Discord's 50-channel limit. Ask an administrator to choose another category."
                ),
                view=None,
            )
            return

        counters = config.setdefault("ticket_counters", {})
        if system == "goup":
            name_pattern = re.compile(
                rf"^(?:go-up|goup)-ticket-{re.escape(paint_slug)}-(\d+)$",
                re.IGNORECASE,
            )
        else:
            name_pattern = re.compile(
                rf"^(?:stb-)?ticket-{re.escape(paint_slug)}-(\d+)$",
                re.IGNORECASE,
            )
        max_existing = max(
            (
                int(match.group(1))
                for channel in category.text_channels
                if ticket_system_from_topic(channel.topic) == system
                and (match := name_pattern.fullmatch(channel.name))
            ),
            default=0,
        )
        counter_key = f"{system}:{paint_slug}"
        legacy_counter = counters.get(paint_slug, 0) if system == "stb" else 0
        ticket_number = max(
            max_existing,
            int(counters.get(counter_key, 0)),
            int(legacy_counter),
        ) + 1
        prefix = "stb" if system == "stb" else "go-up"
        channel_name = f"{prefix}-ticket-{paint_slug}-{ticket_number:04d}"
        ticket_channels = [
            child
            for child in category.text_channels
            if ticket_channel_sort_key(child)[0] < len(PAINT_SYSTEMS)
        ]
        ticket_channels.sort(key=ticket_channel_sort_key)
        current_key = (
            list(PAINT_SYSTEMS).index(system),
            paint_order[paint_slug],
            ticket_number,
        )
        insertion_index = next(
            (
                index
                for index, child in enumerate(ticket_channels)
                if ticket_channel_sort_key(child) > current_key
            ),
            len(ticket_channels),
        )
        if insertion_index < len(ticket_channels):
            channel_position = ticket_channels[insertion_index].position
        elif ticket_channels:
            channel_position = ticket_channels[-1].position + 1
        else:
            channel_position = max(
                (child.position for child in category.channels), default=-1
            ) + 1
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        for role_id in config.get("support_role_ids", []):
            if int(role_id) in NO_TICKET_ACCESS_ROLE_IDS:
                continue
            role = guild.get_role(int(role_id))
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    manage_messages=True,
                )
        for role_id in NO_TICKET_ACCESS_ROLE_IDS:
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=False,
                    send_messages=False,
                    read_message_history=False,
                )
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            )

        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                position=channel_position,
                overwrites=overwrites,
                topic=(
                    f"{system_label} base indexing ticket | owner_id={member.id} "
                    f"| basepaint={paint_slug} "
                    f"| ticket_system={'go-up' if system == 'goup' else system}"
                ),
                reason=f"{system_label} base indexing ticket opened by user ID {member.id}",
            )
        except discord.HTTPException as error:
            await interaction.edit_original_response(
                content=f"I couldn't create the ticket. Check the bot's channel permissions. Details: `{error}`",
                view=None,
            )
            return

        organization_warning = None
        try:
            await organize_ticket_channels(category)
        except discord.HTTPException as error:
            organization_warning = (
                f"I couldn't organize the ticket channels in the category. Details: `{error}`"
            )

        embeds = [
            make_ticket_rules_embed(),
            make_ticket_answers_embed(
                paint_slug, total_items, total_payment, collateral, system
            ),
        ]
        send_kwargs: dict[str, Any] = {
            "content": f"{member.mention} {TICKET_WAITING_MESSAGE}",
            "embeds": embeds,
            "view": TicketCloseView(),
            "allowed_mentions": discord.AllowedMentions(users=[member]),
        }
        if poster_bytes is not None and poster_filename is not None:
            poster_embed = discord.Embed(
                title=f"{system_label} {paint_labels[paint_slug]} Poster",
                color=TICKET_EMBED_COLOR,
            )
            poster_embed.set_image(url=f"attachment://{poster_filename}")
            embeds.append(poster_embed)
            send_kwargs["files"] = [
                discord.File(
                    io.BytesIO(poster_bytes), filename=poster_filename
                )
            ]

        try:
            await channel.send(
                **send_kwargs,
            )
        except discord.HTTPException as error:
            try:
                await channel.delete(reason="Ticket setup message could not be sent.")
            except discord.HTTPException as cleanup_error:
                await interaction.edit_original_response(
                    content=(
                        f"Ticket channel {channel.name} was created, but the setup message "
                        f"failed and I could not remove the channel. Errors: `{error}` / `{cleanup_error}`"
                    ),
                    view=None,
                )
            else:
                await interaction.edit_original_response(
                    content=f"I couldn't send the ticket setup message, so I removed the new channel. Details: `{error}`",
                    view=None,
                )
            return

        counters[counter_key] = ticket_number
        try:
            save_ticket_configs()
        except OSError as error:
            await interaction.edit_original_response(
                content=(
                    f"Ticket created: {channel.mention}. Counter persistence failed; "
                    "the next number will be recovered from the channel name. "
                    f"Details: `{error}`"
                ),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.edit_original_response(
            content=(
                f"Your ticket is ready: {channel.mention}"
                + (f"\n{organization_warning}" if organization_warning else "")
            ),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="STB Base Paint Ticket",
        style=discord.ButtonStyle.success,
        custom_id="basepaint:create_ticket",
    )
    async def create_stb_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.send_paint_picker(interaction, "stb")

    @discord.ui.button(
        label="Go-Up Base Paint Ticket",
        style=discord.ButtonStyle.success,
        custom_id="basepaint:create_go_up_ticket",
    )
    async def create_goup_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.send_paint_picker(interaction, "goup")

    async def send_paint_picker(
        self, interaction: discord.Interaction, system: str
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Tickets can only be opened from a server.", ephemeral=True
            )
            return
        if str(interaction.guild.id) not in TICKET_CONFIGS:
            await interaction.response.send_message(
                "Ticket setup is incomplete. Please ask an administrator to run `/ticket_setupbp`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Thank you for making a {PAINT_SYSTEMS[system]['label']} base paint ticket. In order for us to know what base you want painted please select 1 option below, once selected your ticket will be created.",
            view=BasePaintPickerView(system),
            ephemeral=True,
        )


class TicketCloseConfirmationView(discord.ui.View):
    def __init__(self, channel_id: int, requester_id: int) -> None:
        super().__init__(timeout=90)
        self.channel_id = channel_id
        self.requester_id = requester_id

    @discord.ui.button(label="Confirm close", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who requested this confirmation can use it.",
                ephemeral=True,
            )
            return
        await close_ticket_after_confirmation(interaction, self.channel_id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who requested this confirmation can use it.",
                ephemeral=True,
            )
            return
        self.stop()
        await interaction.response.edit_message(
            content="Ticket closing cancelled.", view=None
        )


async def request_ticket_close(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    member = interaction.user
    channel = interaction.channel
    if (
        guild is None
        or not isinstance(member, discord.Member)
        or not isinstance(channel, discord.TextChannel)
        or owner_id_from_topic(channel.topic) is None
    ):
        await interaction.response.send_message(
            "This command can only be used inside a ticket channel.",
            ephemeral=True,
        )
        return
    config = TICKET_CONFIGS.get(str(guild.id))
    if not member_can_close_ticket(member, channel, config):
        await interaction.response.send_message(
            "Only the ticket opener or a configured support role can close this ticket.",
            ephemeral=True,
        )
        return
    await interaction.response.send_message(
        f"Are you sure you want to close **#{channel.name}**?",
        view=TicketCloseConfirmationView(channel.id, member.id),
        ephemeral=True,
    )


class TicketCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close ticket",
        style=discord.ButtonStyle.danger,
        custom_id="basepaint:close_ticket",
    )
    async def close_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await request_ticket_close(interaction)


async def close_ticket_after_confirmation(
    interaction: discord.Interaction, channel_id: int
) -> None:
    guild = interaction.guild
    member = interaction.user
    channel = guild.get_channel(channel_id) if guild else None
    if (
        guild is None
        or not isinstance(member, discord.Member)
        or not isinstance(channel, discord.TextChannel)
    ):
        await interaction.response.send_message(
            "This ticket is no longer available.", ephemeral=True
        )
        return
    config = TICKET_CONFIGS.get(str(guild.id))
    if not member_can_close_ticket(member, channel, config):
        await interaction.response.send_message(
            "You no longer have permission to close this ticket.", ephemeral=True
        )
        return

    await interaction.response.edit_message(
        content="Closing this ticket. Please wait...",
        view=None,
    )
    ticket_system = ticket_system_from_topic(channel.topic)
    if ticket_system == "goup":
        transcripts_enabled = config.get(
            "goup_transcripts_enabled",
            config.get("transcripts_enabled", True),
        ) if config else True
        transcript_channel_id = (
            config.get("goup_transcript_channel_id") or config.get("panel_channel_id")
            if config
            else None
        )
    else:
        transcripts_enabled = config.get("transcripts_enabled", True) if config else True
        transcript_channel_id = (
            config.get("transcript_channel_id") or config.get("panel_channel_id")
            if config
            else None
        )

    if transcripts_enabled:
        transcript_channel = (
            guild.get_channel(int(transcript_channel_id))
            if transcript_channel_id
            else None
        )
        if not isinstance(transcript_channel, discord.TextChannel):
            await interaction.followup.send(
                content="The transcript channel is unavailable, so I left the ticket open. Ask an administrator to fix ticket setup.",
                ephemeral=True,
            )
            return

        transcript_lines = [
            f"Transcript for #{channel.name}",
            f"Opened by user ID: {owner_id_from_topic(channel.topic)}",
            f"Closed by user ID: {member.id}",
            "",
        ]
        try:
            messages = [
                message
                async for message in channel.history(limit=1000, oldest_first=True)
            ]
        except discord.HTTPException as error:
            await interaction.followup.send(
                f"I couldn't read the ticket history, so I left the ticket open. Details: `{error}`",
                ephemeral=True,
            )
            return
        for message in messages:
            timestamp = message.created_at.astimezone(timezone.utc).isoformat()
            transcript_lines.append(
                f"[{timestamp}] {message.author} (ID: {message.author.id}):"
            )
            if message.content:
                transcript_lines.append(message.content)
            transcript_lines.extend(
                f"Attachment: {attachment.url}" for attachment in message.attachments
            )
            for embed in message.embeds:
                if embed.title:
                    transcript_lines.append(f"Embed title: {embed.title}")
                if embed.description:
                    transcript_lines.append(f"Embed: {embed.description}")
            transcript_lines.append("")

        try:
            await transcript_channel.send(
                content=f"Transcript for **#{channel.name}**.",
                file=discord.File(
                    io.BytesIO("\n".join(transcript_lines).encode("utf-8")),
                    filename=f"transcript-{channel.name}.txt",
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as error:
            await interaction.followup.send(
                f"I couldn't save the transcript, so I left the ticket open. Details: `{error}`",
                ephemeral=True,
            )
            return

    try:
        await channel.delete(reason=f"Ticket closed by user ID {member.id}")
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"The ticket could not be deleted. Details: `{error}`",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        (
            "Ticket closed and transcript saved."
            if transcripts_enabled
            else "Ticket closed."
        ),
        ephemeral=True,
    )


class TicketSetupView(discord.ui.View):
    def __init__(self, guild_id: int, existing_config: dict | None = None) -> None:
        super().__init__(timeout=600)
        self.guild_id = guild_id
        self._guild_ref: discord.Guild | None = None
        self.config = {
            "panel_channel_id": None,
            "transcript_channel_id": None,
            "category_id": None,
            "support_role_ids": [],
            "transcripts_enabled": True,
            "goup_transcript_channel_id": None,
            "goup_transcripts_enabled": True,
            "panel_image_url": None,
        }
        if existing_config:
            self.config.update(existing_config)
        self.setup_message: discord.Message | None = None

    def setup_embed(self) -> discord.Embed:
        guild = self._guild

        def channel_name(channel_id: int | str | None) -> str:
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            return channel.mention if channel is not None else "Not selected"

        roles = [
            role.mention
            for role_id in self.config["support_role_ids"]
            if (role := guild.get_role(int(role_id))) is not None
        ]
        transcript_channel = self.config["transcript_channel_id"]
        transcript_destination = (
            channel_name(transcript_channel)
            if transcript_channel
            else f"{channel_name(self.config['panel_channel_id'])} (panel channel)"
        )
        goup_transcript_channel = self.config["goup_transcript_channel_id"]
        goup_transcript_destination = (
            channel_name(goup_transcript_channel)
            if goup_transcript_channel
            else f"{channel_name(self.config['panel_channel_id'])} (panel channel)"
        )
        image_url = self.config["panel_image_url"]
        embed = discord.Embed(
            title="Base Indexing Ticket Setup",
            description=(
                f"**Panel channel:** {channel_name(self.config['panel_channel_id'])}\n"
                f"**STB transcript channel:** {transcript_destination}\n"
                f"**Go-Up transcript channel:** {goup_transcript_destination}\n"
                f"**Ticket category:** {channel_name(self.config['category_id'])}\n"
                f"**Support roles:** {', '.join(roles) if roles else 'None selected'}\n"
                f"**STB automatic transcripts:** {'On' if self.config['transcripts_enabled'] else 'Off'}\n"
                f"**Go-Up automatic transcripts:** {'On' if self.config['goup_transcripts_enabled'] else 'Off'}\n\n"
                f"**Panel image/GIF:** {'Configured' if image_url else 'Not set'}\n\n"
                "Choose the settings below, then publish the ticket panel."
            ),
            color=TICKET_EMBED_COLOR,
        )
        if image_url:
            embed.set_image(url=image_url)
        return embed

    @property
    def _guild(self) -> discord.Guild:
        guild = self._guild_ref
        if guild is None:
            raise RuntimeError("Ticket setup view is no longer attached to a server.")
        return guild

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.setup_embed(), view=self)

    async def refresh_setup_message(self) -> None:
        if self.setup_message is not None:
            self.setup_message = await self.setup_message.edit(
                embed=self.setup_embed(), view=self
            )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Choose where the ticket panel will be posted",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def choose_panel_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self._guild_ref = interaction.guild
        self.config["panel_channel_id"] = select.values[0].id
        await self.refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Choose where transcripts will be sent (optional)",
        min_values=1,
        max_values=1,
        row=1,
    )
    async def choose_transcript_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self._guild_ref = interaction.guild
        self.config["transcript_channel_id"] = select.values[0].id
        await self.refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.category],
        placeholder="Choose the category for ticket channels",
        min_values=1,
        max_values=1,
        row=2,
    )
    async def choose_category(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self._guild_ref = interaction.guild
        self.config["category_id"] = select.values[0].id
        await self.refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Choose support/index giver roles (optional)",
        min_values=0,
        max_values=10,
        row=3,
    )
    async def choose_support_roles(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        self._guild_ref = interaction.guild
        self.config["support_role_ids"] = [role.id for role in select.values]
        await self.refresh(interaction)

    @discord.ui.button(
        label="Toggle automatic transcripts",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def toggle_transcripts(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self._guild_ref = interaction.guild
        self.config["transcripts_enabled"] = not self.config["transcripts_enabled"]
        await self.refresh(interaction)

    @discord.ui.button(
        label="Set panel image/GIF",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def set_panel_image(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(TicketPanelImageModal(self))

    @discord.ui.button(
        label="Go-Up transcript settings",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def configure_goup_transcripts(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self._guild_ref = interaction.guild
        settings_view = GoupTranscriptSetupView(self)
        await interaction.response.send_message(
            embed=settings_view.setup_embed(),
            view=settings_view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Publish ticket panel",
        style=discord.ButtonStyle.success,
        row=4,
    )
    async def publish_panel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message(
                "This setup panel is no longer available in this server.",
                ephemeral=True,
            )
            return

        panel_channel = guild.get_channel(
            int(self.config["panel_channel_id"])
        ) if self.config["panel_channel_id"] else None
        category = guild.get_channel(int(self.config["category_id"])) if self.config["category_id"] else None
        if not isinstance(panel_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Select a valid panel channel before publishing.", ephemeral=True
            )
            return
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Select a valid ticket category before publishing.", ephemeral=True
            )
            return
        if self.config["transcripts_enabled"]:
            transcript_channel_id = self.config["transcript_channel_id"] or panel_channel.id
            transcript_channel = guild.get_channel(int(transcript_channel_id))
            if not isinstance(transcript_channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Select a valid transcript channel or disable automatic transcripts.",
                    ephemeral=True,
                )
                return
        if self.config["goup_transcripts_enabled"]:
            goup_transcript_channel_id = (
                self.config["goup_transcript_channel_id"] or panel_channel.id
            )
            goup_transcript_channel = guild.get_channel(
                int(goup_transcript_channel_id)
            )
            if not isinstance(goup_transcript_channel, discord.TextChannel):
                await interaction.response.send_message(
                    "Select a valid Go-Up transcript channel or disable Go-Up automatic transcripts.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_config = TICKET_CONFIGS.setdefault(str(guild.id), {})
        guild_config.update({
            "panel_channel_id": panel_channel.id,
            "transcript_channel_id": self.config["transcript_channel_id"],
            "category_id": category.id,
            "support_role_ids": self.config["support_role_ids"],
            "transcripts_enabled": self.config["transcripts_enabled"],
            "goup_transcript_channel_id": self.config[
                "goup_transcript_channel_id"
            ],
            "goup_transcripts_enabled": self.config[
                "goup_transcripts_enabled"
            ],
            "panel_image_url": self.config["panel_image_url"],
        })
        try:
            save_ticket_configs()
            await panel_channel.send(
                embed=make_panel_embed(self.config["panel_image_url"]),
                view=TicketPanelView(),
            )
        except (OSError, discord.HTTPException, ValueError) as error:
            await interaction.followup.send(
                f"I couldn't finish publishing the ticket panel. Check the bot's channel permissions and local file access. Details: `{error}`",
                ephemeral=True,
            )
            return

        self.stop()
        await interaction.followup.send(
            f"Ticket panel published in {panel_channel.mention}.", ephemeral=True
        )


class GoupTranscriptSetupView(discord.ui.View):
    def __init__(self, setup_view: TicketSetupView) -> None:
        super().__init__(timeout=300)
        self.setup_view = setup_view

    def setup_embed(self) -> discord.Embed:
        guild = self.setup_view._guild
        channel_id = self.setup_view.config["goup_transcript_channel_id"]
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        destination = (
            channel.mention
            if channel is not None
            else f"{guild.get_channel(int(self.setup_view.config['panel_channel_id'])).mention} (panel channel)"
            if self.setup_view.config["panel_channel_id"]
            and guild.get_channel(int(self.setup_view.config["panel_channel_id"]))
            else "Panel channel (fallback)"
        )
        return discord.Embed(
            title="Go-Up Transcript Settings",
            description=(
                f"**Go-Up transcript destination:** {destination}\n"
                f"**Go-Up automatic transcripts:** "
                f"{'On' if self.setup_view.config['goup_transcripts_enabled'] else 'Off'}\n\n"
                "These settings apply only to Go-Up tickets."
            ),
            color=TICKET_EMBED_COLOR,
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="Choose the Go-Up transcript channel",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def choose_goup_transcript_channel(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        self.setup_view.config["goup_transcript_channel_id"] = (
            select.values[0].id
        )
        await interaction.response.edit_message(
            embed=self.setup_embed(), view=self
        )

    @discord.ui.button(
        label="Toggle Go-Up transcripts",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def toggle_goup_transcripts(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.setup_view.config["goup_transcripts_enabled"] = not (
            self.setup_view.config["goup_transcripts_enabled"]
        )
        await interaction.response.edit_message(
            embed=self.setup_embed(), view=self
        )

    @discord.ui.button(
        label="Use panel channel",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def use_panel_channel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        self.setup_view.config["goup_transcript_channel_id"] = None
        await interaction.response.edit_message(
            embed=self.setup_embed(), view=self
        )

    @discord.ui.button(
        label="Done",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def done(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        try:
            await self.setup_view.refresh_setup_message()
        except discord.HTTPException as error:
            await interaction.response.send_message(
                f"Go-Up transcript settings are updated in this setup session, but I couldn't refresh the main setup panel. Details: `{error}`",
                ephemeral=True,
            )
            return
        self.stop()
        await interaction.response.edit_message(
            content="Go-Up transcript settings updated. Publish the panel to save them.",
            embed=None,
            view=None,
        )


class TicketPanelImageModal(discord.ui.Modal):
    def __init__(self, setup_view: TicketSetupView) -> None:
        super().__init__(title="Set Ticket Panel Image/GIF")
        self.setup_view = setup_view
        current_url = setup_view.config.get("panel_image_url")
        self.image_url = discord.ui.TextInput(
            label="Direct image/GIF URL",
            placeholder="https://example.com/image.gif (leave blank to remove)",
            default=current_url,
            required=False,
            max_length=2000,
        )
        self.add_item(self.image_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        image_url = self.image_url.value.strip()
        if image_url:
            try:
                parsed_url = urlsplit(image_url)
            except ValueError:
                parsed_url = None
            if (
                parsed_url is None
                or parsed_url.scheme not in {"http", "https"}
                or not parsed_url.netloc
            ):
                await interaction.response.send_message(
                    "Enter a valid direct image or GIF URL beginning with `https://` or `http://`, or leave it blank to remove the image.",
                    ephemeral=True,
                )
                return

        self.setup_view.config["panel_image_url"] = image_url or None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.setup_view.refresh_setup_message()
        except discord.HTTPException as error:
            await interaction.followup.send(
                f"The image setting was updated for this setup session, but I couldn't refresh the setup panel. Details: `{error}`",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Panel image/GIF setting updated. Publish the panel to use it.",
            ephemeral=True,
        )


class YGGBot(commands.Bot):
    async def setup_hook(self) -> None:
        self.add_view(TicketPanelView())
        self.add_view(TicketCloseView())
        await self.tree.sync()


intents = discord.Intents.default()
intents.members = True
bot = YGGBot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    allowed_mentions=discord.AllowedMentions.none(),
)


@bot.event
async def on_ready() -> None:
    print(f"{bot.user} is online!")


@bot.tree.command(
    name="ticket_setupbp",
    description="Configure and publish the Base Indexing ticket panel.",
)
async def ticket_setupbp(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message(
            "Run ticket setup from a server.", ephemeral=True
        )
        return
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "You need the Manage Server permission to configure tickets.",
            ephemeral=True,
        )
        return

    view = TicketSetupView(
        interaction.guild.id, TICKET_CONFIGS.get(str(interaction.guild.id))
    )
    view._guild_ref = interaction.guild
    await interaction.response.send_message(
        embed=view.setup_embed(),
        view=view,
        ephemeral=True,
    )
    view.setup_message = await interaction.original_response()


@bot.tree.command(
    name="ticket_closebp",
    description="Request to close this base paint ticket.",
)
async def ticket_closebp(interaction: discord.Interaction) -> None:
    await request_ticket_close(interaction)


@bot.tree.command(
    name="basepaint_add",
    description="Add a base paint to the STB or Go-Up ticket picker.",
)
@discord.app_commands.choices(
    system=[
        discord.app_commands.Choice(name="STB", value="stb"),
        discord.app_commands.Choice(name="Go-Up", value="goup"),
    ]
)
async def basepaint_add(
    interaction: discord.Interaction,
    system: discord.app_commands.Choice[str],
    paint_name: str,
) -> None:
    guild = interaction.guild
    member = interaction.user
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message(
            "Add base paints from a server.", ephemeral=True
        )
        return

    authorized = (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or any(role.id == POSTER_SUBMIT_ROLE_ID for role in member.roles)
    )
    if not authorized:
        await interaction.response.send_message(
            "You need Manage Server permission or the authorized painter role to add base paints.",
            ephemeral=True,
        )
        return

    normalized_name = paint_name.strip()
    paint_slug = re.sub(r"[^a-z0-9]+", "-", normalized_name.casefold()).strip("-")
    paint_label = normalized_name.title()
    if (
        not normalized_name
        or len(paint_label) > 100
        or not paint_slug
        or len(paint_slug) > 60
    ):
        await interaction.response.send_message(
            "Enter a paint name that contains letters or numbers and is at most 100 characters.",
            ephemeral=True,
        )
        return

    system_key = system.value
    system_paints = PAINT_SYSTEMS[system_key]["options"]
    if any(existing_slug == paint_slug for existing_slug, _ in system_paints):
        await interaction.response.send_message(
            f"**{paint_label}** already exists in the {PAINT_SYSTEMS[system_key]['label']} paint list.",
            ephemeral=True,
        )
        return
    if len(system_paints) >= 125:
        await interaction.response.send_message(
            f"The {PAINT_SYSTEMS[system_key]['label']} paint picker has reached its 125-paint limit.",
            ephemeral=True,
        )
        return

    updated_custom_options = {
        key: list(options) for key, options in CUSTOM_PAINT_OPTIONS.items()
    }
    updated_custom_options[system_key].append((paint_slug, paint_label))
    try:
        save_custom_paint_options(updated_custom_options)
    except (OSError, TypeError, ValueError) as error:
        await interaction.response.send_message(
            f"I couldn't save the new paint. Check the bot's local file access. Details: `{error}`",
            ephemeral=True,
        )
        return

    CUSTOM_PAINT_OPTIONS[system_key].append((paint_slug, paint_label))
    system_paints.append((paint_slug, paint_label))
    PAINT_LABELS[system_key][paint_slug] = paint_label
    PAINT_ORDERS[system_key][paint_slug] = len(PAINT_ORDERS[system_key])
    await interaction.response.send_message(
        f"Added **{paint_label}** to the {PAINT_SYSTEMS[system_key]['label']} ticket picker. It is now available for new tickets and poster submissions.",
        ephemeral=True,
    )


@bot.tree.command(
    name="ticketbp_add",
    description="Add one member to the current base paint ticket.",
)
async def ticketbp_add(
    interaction: discord.Interaction, member: discord.Member
) -> None:
    guild = interaction.guild
    actor = interaction.user
    channel = interaction.channel
    if (
        guild is None
        or not isinstance(actor, discord.Member)
        or not isinstance(channel, discord.TextChannel)
        or owner_id_from_topic(channel.topic) is None
    ):
        await interaction.response.send_message(
            "Use this command inside a base paint ticket.", ephemeral=True
        )
        return

    is_higher_up = any(role.id == POSTER_SUBMIT_ROLE_ID for role in actor.roles)
    is_admin = (
        actor.guild_permissions.administrator
        or actor.guild_permissions.manage_guild
    )
    if not is_admin and not is_higher_up:
        await interaction.response.send_message(
            "Only admins or authorized higher-ups can add members to tickets.",
            ephemeral=True,
        )
        return

    if any(role.id in NO_TICKET_ACCESS_ROLE_IDS for role in member.roles):
        await interaction.response.send_message(
            "This member has a role that is not allowed to view Base Paint tickets.",
            ephemeral=True,
        )
        return

    try:
        await channel.set_permissions(
            member,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            reason=f"Added to ticket by authorized user ID {actor.id}",
        )
    except discord.HTTPException as error:
        await interaction.response.send_message(
            f"I couldn't add {member.mention} to this ticket. Check the bot's channel permissions. Details: `{error}`",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    await interaction.response.send_message(
        f"{member.mention} can now view and participate in this ticket.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="poster_submit",
    description="Submit or replace an STB base paint poster.",
)
async def poster_submit(
    interaction: discord.Interaction,
    base_paint: str,
    poster: discord.Attachment,
) -> None:
    await submit_poster_for_system(interaction, base_paint, poster, "stb")


@bot.tree.command(
    name="poster_go_up_submit",
    description="Submit or replace a Go-Up base paint poster.",
)
async def poster_go_up_submit(
    interaction: discord.Interaction,
    base_paint: str,
    poster: discord.Attachment,
) -> None:
    await submit_poster_for_system(interaction, base_paint, poster, "goup")


async def submit_poster_for_system(
    interaction: discord.Interaction,
    base_paint: str,
    poster: discord.Attachment,
    system: str,
) -> None:
    guild = interaction.guild
    member = interaction.user
    paint_labels = PAINT_LABELS[system]
    system_label = PAINT_SYSTEMS[system]["label"]
    if guild is None or not isinstance(member, discord.Member):
        await interaction.response.send_message(
            f"Submit a {system_label} base paint poster from a server.",
            ephemeral=True,
        )
        return
    authorized = (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
        or any(role.id == POSTER_SUBMIT_ROLE_ID for role in member.roles)
    )
    if not authorized:
        await interaction.response.send_message(
            "You need Manage Server permission or the authorized painter role to submit posters.",
            ephemeral=True,
        )
        return

    paint_slug = base_paint.strip().casefold()
    if paint_slug not in paint_labels:
        await interaction.response.send_message(
            "Select one of the available base paints from the autocomplete list.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        image_data = await poster.read()
    except discord.HTTPException as error:
        await interaction.followup.send(
            f"I couldn't download the attached poster. Details: `{error}`",
            ephemeral=True,
        )
        return

    extension = valid_poster_extension(poster.content_type, image_data)
    if extension is None:
        await interaction.followup.send(
            "The attachment must be a valid PNG, JPEG, GIF, or WebP image.",
            ephemeral=True,
        )
        return

    poster_path = poster_path_for(guild.id, paint_slug, extension, system)
    try:
        poster_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = poster_path.with_suffix(poster_path.suffix + ".tmp")
        temporary_path.write_bytes(image_data)
        os.replace(temporary_path, poster_path)

        guild_config = TICKET_CONFIGS.setdefault(str(guild.id), {})
        posters = guild_config.setdefault("posters", {})
        poster_key = f"{system}:{paint_slug}"
        previous_extension = posters.get(poster_key)
        posters[poster_key] = extension
        try:
            save_ticket_configs()
        except (OSError, TypeError, ValueError):
            if previous_extension is None:
                posters.pop(poster_key, None)
            else:
                posters[poster_key] = previous_extension
            raise
    except (OSError, TypeError, ValueError) as error:
        await interaction.followup.send(
            f"I couldn't save the poster. Check the bot's local file access. Details: `{error}`",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"Poster submitted for **{system_label} {paint_labels[paint_slug]}**. It will appear in future matching tickets.",
        ephemeral=True,
    )


@poster_submit.autocomplete("base_paint")
async def poster_paint_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    return paint_autocomplete(interaction, current, "stb")


@poster_go_up_submit.autocomplete("base_paint")
async def poster_go_up_paint_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[discord.app_commands.Choice[str]]:
    return paint_autocomplete(interaction, current, "goup")


def paint_autocomplete(
    interaction: discord.Interaction, current: str, system: str
) -> list[discord.app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    query = current.strip().casefold()
    return [
        discord.app_commands.Choice(name=label, value=slug)
        for slug, label in PAINT_LABELS[system].items()
        if query in slug.casefold() or query in label.casefold()
    ][:25]


load_ticket_configs()
if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in the environment before starting the bot.")
bot.run(TOKEN)
