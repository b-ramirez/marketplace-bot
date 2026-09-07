"""
Marketplace Approval Bot
-------------------------
Members use /sell, fill out a form for their items/prices and a
description, then send their photos as a normal message right after
(drag-and-drop from their device — no links needed). The submission
goes to a mod-review channel where mods get pinged and can
Approve/Deny with buttons.

- Approve: creates a new post in your public marketplace FORUM channel
  (so nothing is visible there until a mod approves it).
- Deny: DMs the seller the mod's reason, with an "Edit & Resubmit"
  button that reopens a pre-filled form.

SETUP:
1. pip install discord.py python-dotenv
2. Create a .env file next to this script with:
     BOT_TOKEN=your_bot_token_here
     GUILD_ID=your_server_id
     MOD_REVIEW_CHANNEL_ID=channel_id_for_mods_only
     MARKETPLACE_CHANNEL_ID=channel_id_of_your_FORUM_channel
     MOD_ROLE_ID=role_id_that_can_approve
     MARKETPLACE_ACCESS_ROLE_ID=role_id_dyno_grants_for_marketplace_access
3. Invite the bot with "applications.commands" + "bot" scopes and these
   permissions: Send Messages, Embed Links, Manage Messages (in review
   channel), Read Message History, Create Posts / Send Messages in
   Threads, and Manage Threads (in the marketplace forum — needed for
   /sold and /pending to rename and lock listing posts), and Manage
   Roles (server-wide — needed for /marketplaceban to strip access,
   and the bot's own role must sit ABOVE the marketplace access role
   in Server Settings > Roles for this to work).
4. IMPORTANT for the mod ping to actually show up: go to Server
   Settings > Roles > (your mod role) and enable "Allow anyone to
   @mention this role" — otherwise Discord silently won't ping it
   unless the bot also has the "Mention Everyone" permission.
5. Run: python marketplace_bot.py
"""

import asyncio
import io
import json
import os
import re
import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MOD_REVIEW_CHANNEL_ID = int(os.getenv("MOD_REVIEW_CHANNEL_ID", "0"))
MARKETPLACE_CHANNEL_ID = int(os.getenv("MARKETPLACE_CHANNEL_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))
MARKETPLACE_ACCESS_ROLE_ID = int(os.getenv("MARKETPLACE_ACCESS_ROLE_ID", "0"))

MAX_PHOTOS = 5
PHOTO_WAIT_SECONDS = 180  # how long we wait for the seller to send photos
BANNED_LIST_MARKER = "MARKETPLACE_BANNED_LIST"

banned_user_ids: set[int] = set()
banned_list_message: discord.Message | None = None

intents = discord.Intents.default()
intents.members = True  # needed to DM users reliably
intents.message_content = True  # needed to see attachments on the seller's photo message

bot = commands.Bot(command_prefix="!", intents=intents)
print(f"[STARTUP] discord.py version: {discord.__version__}")


def is_mod(member: discord.Member) -> bool:
    return any(role.id == MOD_ROLE_ID for role in member.roles)


async def load_or_create_banned_list():
    """The marketplace ban list is stored as hidden data inside a message in
    the mods-only review channel — not a visible role — so banned users have
    no way to tell they've been banned."""
    global banned_list_message, banned_user_ids
    review_channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
    if review_channel is None:
        print("[BANLIST] mod review channel not found, cannot load banned list")
        return

    async for msg in review_channel.history(limit=200):
        if msg.author.id == bot.user.id and BANNED_LIST_MARKER in msg.content:
            banned_list_message = msg
            try:
                json_part = msg.content.split("```json")[1].split("```")[0]
                banned_user_ids = set(json.loads(json_part))
            except (IndexError, json.JSONDecodeError):
                banned_user_ids = set()
            print(f"[BANLIST] loaded {len(banned_user_ids)} banned user(s)")
            return

    content = f"🔒 {BANNED_LIST_MARKER} — internal data, do not delete\n```json\n[]\n```"
    banned_list_message = await review_channel.send(content)
    banned_user_ids = set()
    print("[BANLIST] created new banned list message")


async def save_banned_list():
    global banned_list_message
    content = (
        f"🔒 {BANNED_LIST_MARKER} — internal data, do not delete\n"
        f"```json\n{json.dumps(sorted(banned_user_ids))}\n```"
    )
    if banned_list_message:
        try:
            await banned_list_message.edit(content=content)
            return
        except discord.NotFound:
            pass
    review_channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
    if review_channel:
        banned_list_message = await review_channel.send(content)


async def enforce_ban_on_member(member: discord.Member, reason: str):
    """Strips marketplace access from a banned member if they currently have it."""
    if not MARKETPLACE_ACCESS_ROLE_ID:
        return
    access_role = member.guild.get_role(MARKETPLACE_ACCESS_ROLE_ID)
    if access_role and access_role in member.roles:
        try:
            await member.remove_roles(access_role, reason=reason)
            print(f"[BANENFORCE] removed marketplace access from {member.id} ({reason})")
        except discord.Forbidden:
            print(f"[BANENFORCE] failed to remove role from {member.id} — check bot role position")


def parse_items(raw_text: str):
    """
    Parses lines like:
        Charizard VMAX - $45
        Pikachu V: $20
        Random loose item
    into a list of (item_name, price) tuples. If no delimiter is found,
    price defaults to "Price not listed".
    """
    items = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        for delim in (" - ", ":", "—"):
            if delim in line:
                name, _, price = line.partition(delim)
                items.append((name.strip(), price.strip() or "Price not listed"))
                break
        else:
            items.append((line, "Price not listed"))
    return items


def is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type and att.content_type.startswith("image/"):
        return True
    return att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic"))


def build_listing_embeds(items, description, author, image_filenames, status, color, listing_type="SELL"):
    """image_filenames are the names of files being sent ALONGSIDE this
    embed in the same message (via the `files=` kwarg) — referenced with
    the special attachment://<filename> scheme so the image is permanently
    hosted on this message rather than pointing at someone else's."""
    lines = "\n".join(f"• **{name}** — {price}" for name, price in items) or "No items listed."
    main_embed = discord.Embed(
        title=f"{TYPE_TAGS.get(listing_type, '')} {TYPE_TITLES.get(listing_type, 'New Marketplace Listing')}".strip(),
        description=description or "No description provided.",
        color=color,
    )
    main_embed.add_field(name=TYPE_ITEMS_LABELS.get(listing_type, "Items & Prices").split(" (")[0], value=lines, inline=False)
    main_embed.add_field(
        name="Seller",
        value=author.mention if hasattr(author, "mention") else f"<@{author}>",
        inline=True,
    )
    main_embed.set_footer(text=f"Seller ID: {getattr(author, 'id', author)} • {status}")

    embeds = [main_embed]
    if image_filenames:
        main_embed.set_image(url=f"attachment://{image_filenames[0]}")
        # Extra images become their own bare embeds, forming a gallery
        for filename in image_filenames[1:]:
            extra = discord.Embed(color=color)
            extra.set_image(url=f"attachment://{filename}")
            embeds.append(extra)
    return embeds


async def download_embed_images(embeds, filename_prefix="photo"):
    """Downloads whatever images are actually rendered in these embeds, using
    the embed's own resolved image URL rather than trusting message.attachments
    — which has proven unreliable for images referenced via attachment://."""
    files = []
    async with aiohttp.ClientSession() as session:
        for i, embed in enumerate(embeds):
            if embed.image and embed.image.url and not embed.image.url.startswith("attachment://"):
                url = embed.image.url
                try:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            ext = url.split("?")[0].rsplit(".", 1)[-1] if "." in url.split("?")[0] else "png"
                            files.append(discord.File(io.BytesIO(data), filename=f"{filename_prefix}_{i}.{ext}"))
                        else:
                            print(f"[DOWNLOAD] status {resp.status} for {url}")
                except Exception as e:
                    print(f"[DOWNLOAD] failed to fetch {url}: {e!r}")
    return files


async def get_thread_seller_id(thread: discord.Thread):
    try:
        msg = thread.starter_message or await thread.fetch_message(thread.id)
    except (discord.NotFound, discord.HTTPException):
        return None
    for embed in msg.embeds:
        if embed.footer and embed.footer.text:
            match = re.search(r"Seller ID: (\d+)", embed.footer.text)
            if match:
                return int(match.group(1))
    return None


async def mark_listing_status(interaction: discord.Interaction, status: str, lock: bool):
    channel = interaction.channel
    if not isinstance(channel, discord.Thread) or channel.parent_id != MARKETPLACE_CHANNEL_ID:
        await interaction.response.send_message(
            "This only works inside your listing's post in the marketplace forum.",
            ephemeral=True,
        )
        return

    seller_id = await get_thread_seller_id(channel)
    if seller_id is None or seller_id != interaction.user.id:
        await interaction.response.send_message(
            "Only the seller who posted this listing can update it.", ephemeral=True
        )
        return

    base_name = re.sub(r"^\[(SOLD|PENDING)\]\s*", "", channel.name)
    new_name = f"[{status}] {base_name}"[:100]

    try:
        if lock:
            await channel.edit(name=new_name, locked=True, archived=True)
        else:
            await channel.edit(name=new_name)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I don't have permission to update this thread — ask a mod to check my "
            "Manage Threads permission in the marketplace forum.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(f"Marked your listing as **{status}**.", ephemeral=True)


TYPE_TITLES = {
    "SELL": "New Marketplace Listing",
    "BUY": "New Buy Request",
    "TRADE": "New Trade Offer",
}
TYPE_TAGS = {"SELL": "[WTS]", "BUY": "[WTB]", "TRADE": "[WTT]"}
TYPE_ITEMS_LABELS = {
    "SELL": "Items & Prices (one per line)",
    "BUY": "Items Wanted & Budget (one per line)",
    "TRADE": "Items You Have & What You Want (one per line)",
}
TYPE_ITEMS_PLACEHOLDERS = {
    "SELL": "Charizard VMAX Rainbow Rare - $45\nPikachu V - $20 or trade",
    "BUY": "Charizard VMAX Rainbow Rare - up to $50\nAny Pikachu V - $15-20",
    "TRADE": "Have: Charizard VMAX / Want: Umbreon VMAX or similar value",
}


class ListingTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Sell", description="List an item for sale", emoji="💰", value="SELL"),
            discord.SelectOption(label="Looking to Buy", description="Post what you're looking to buy", emoji="🔍", value="BUY"),
            discord.SelectOption(label="Looking to Trade", description="Post what you want to trade", emoji="🔄", value="TRADE"),
        ]
        super().__init__(placeholder="What kind of listing is this?", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ListingModal(listing_type=self.values[0]))


class ListingTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.add_item(ListingTypeSelect())


# ---------- Submission Modal (text fields only) ----------

class ListingModal(discord.ui.Modal):
    def __init__(self, listing_type: str = "SELL", prefill: dict | None = None):
        super().__init__(title=TYPE_TITLES.get(listing_type, TYPE_TITLES["SELL"]))
        self.listing_type = listing_type

        self.items_and_prices = discord.ui.TextInput(
            label=TYPE_ITEMS_LABELS.get(listing_type, TYPE_ITEMS_LABELS["SELL"]),
            style=discord.TextStyle.paragraph,
            placeholder=TYPE_ITEMS_PLACEHOLDERS.get(listing_type, TYPE_ITEMS_PLACEHOLDERS["SELL"]),
            max_length=1000,
        )
        self.description = discord.ui.TextInput(
            label="Description / Notes",
            style=discord.TextStyle.paragraph,
            placeholder="Condition, shipping info, extra details...",
            max_length=500,
            required=False,
        )
        if prefill:
            self.items_and_prices.default = prefill.get("items_and_prices")
            self.description.default = prefill.get("description")
        self.add_item(self.items_and_prices)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        items = parse_items(self.items_and_prices.value)

        await interaction.response.send_message(
            f"Got your listing! Now **send up to {MAX_PHOTOS} photos** as your next "
            f"message in this channel (just attach them like normal and hit send). "
            f"Type `skip` if you don't want to add photos. You have "
            f"{PHOTO_WAIT_SECONDS // 60} minutes.",
            ephemeral=True,
        )

        photo_files: list[discord.File] = []

        def check(m: discord.Message):
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id

        try:
            msg = await bot.wait_for("message", check=check, timeout=PHOTO_WAIT_SECONDS)
            print(f"[SUBMIT] received message with {len(msg.attachments)} raw attachments: "
                  f"{[(a.filename, a.content_type) for a in msg.attachments]}")
            if msg.content.strip().lower() != "skip":
                image_atts = [a for a in msg.attachments if is_image_attachment(a)][:MAX_PHOTOS]
                print(f"[SUBMIT] {len(image_atts)} passed is_image_attachment filter")
                # Re-download and re-attach each image now, BEFORE deleting the
                # seller's message — otherwise the file becomes unreachable.
                photo_files = [await att.to_file() for att in image_atts]
                print(f"[SUBMIT] photo_files built: {[f.filename for f in photo_files]}")
            try:
                await msg.delete()
            except (discord.Forbidden, discord.NotFound):
                pass
        except asyncio.TimeoutError:
            print("[SUBMIT] timed out waiting for photo message")
            try:
                await interaction.followup.send(
                    "No photos received in time — submitting your listing without photos.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

        review_channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
        if review_channel is None:
            await interaction.followup.send(
                "Couldn't reach the mod review channel. Contact an admin.",
                ephemeral=True,
            )
            return

        image_filenames = [f.filename for f in photo_files]
        for f in photo_files:
            f.fp.seek(0)  # ensure the read position is at the start before uploading
            f.fp.seek(0, 2)
            print(f"[SUBMIT] file {f.filename} size={f.fp.tell()} bytes")
            f.fp.seek(0)
        print(f"[SUBMIT] about to send review message with {len(photo_files)} file(s): {image_filenames}")

        embeds = build_listing_embeds(
            items=items,
            description=self.description.value,
            author=interaction.user,
            image_filenames=image_filenames,
            status="Pending Review",
            color=discord.Color.yellow(),
            listing_type=self.listing_type,
        )

        view = ReviewView(
            author_id=interaction.user.id,
            items=items,
            description=self.description.value,
            listing_type=self.listing_type,
        )

        mod_ping = f"<@&{MOD_ROLE_ID}>" if MOD_ROLE_ID else ""
        # Send the listing (with photos) and the Approve/Deny buttons as TWO
        # separate messages — combining files= and view= in one send has
        # been unreliable for actually attaching the files.
        sent_review_msg = await review_channel.send(
            content=f"{mod_ping} 📥 New {self.listing_type.lower()} listing awaiting approval:",
            embeds=embeds,
            files=photo_files,
        )
        print(f"[SUBMIT] sent review message id={sent_review_msg.id} in channel={sent_review_msg.channel.id} "
              f"attachments={[a.filename for a in sent_review_msg.attachments]}")
        view.listing_message_id = sent_review_msg.id
        await review_channel.send(
            content="👆 Mods, review the listing above:",
            view=view,
        )

        await interaction.followup.send(
            "Your listing was submitted for mod approval. You'll be notified once reviewed.",
            ephemeral=True,
        )


# ---------- Deny Reason Modal ----------

class DenyReasonModal(discord.ui.Modal, title="Reason for Denial"):
    reason = discord.ui.TextInput(
        label="Why is this listing being denied?",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Price doesn't match item value, missing proof photos...",
        max_length=300,
    )

    def __init__(self, review_view: "ReviewView", review_message: discord.Message):
        super().__init__()
        self.review_view = review_view
        self.review_message = review_message

    async def on_submit(self, interaction: discord.Interaction):
        v = self.review_view
        if v.resolved:
            await interaction.response.send_message(
                "This listing has already been reviewed.", ephemeral=True
            )
            return
        v.resolved = True

        await v._notify_seller_denied(self.reason.value)

        for child in v.children:
            child.disabled = True

        try:
            listing_msg = await self.review_message.channel.fetch_message(v.listing_message_id)
            if listing_msg.embeds:
                listing_msg.embeds[0].color = discord.Color.red()
                listing_msg.embeds[0].set_footer(text=f"❌ Denied by {interaction.user.display_name}")
                # Must explicitly pass attachments= on edit, or Discord wipes
                # the existing photo attachments on this message.
                await listing_msg.edit(embeds=listing_msg.embeds, attachments=listing_msg.attachments)
        except (discord.NotFound, discord.HTTPException):
            pass

        await self.review_message.edit(
            content=f"❌ Denied by {interaction.user.display_name}", view=v
        )

        await interaction.response.send_message("Denial reason sent to the seller.", ephemeral=True)


# ---------- Resubmit view sent in the denial DM ----------

class ResubmitView(discord.ui.View):
    def __init__(self, prefill: dict):
        super().__init__(timeout=None)
        self.prefill = prefill

    @discord.ui.button(label="Edit & Resubmit", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_resubmit(self, interaction: discord.Interaction, button: discord.ui.Button):
        listing_type = self.prefill.get("listing_type", "SELL")
        await interaction.response.send_modal(ListingModal(listing_type=listing_type, prefill=self.prefill))


# ---------- Approve / Deny Buttons ----------

class ReviewView(discord.ui.View):
    def __init__(self, author_id: int, items: list, description: str, listing_type: str = "SELL"):
        super().__init__(timeout=None)
        self.author_id = author_id
        self.items = items
        self.description = description
        self.listing_type = listing_type
        self.resolved = False  # guards against double Approve/Deny clicks
        self.listing_message_id = None  # set right after the listing message is sent

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message(
                "This listing has already been reviewed.", ephemeral=True
            )
            return
        if not is_mod(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to approve listings.", ephemeral=True
            )
            return
        self.resolved = True  # lock immediately, before any awaits, to block a second click

        try:
            forum_channel = bot.get_channel(MARKETPLACE_CHANNEL_ID)
            seller = interaction.guild.get_member(self.author_id)

            # Fetch the ORIGINAL listing message (the one with the photos),
            # not this button message, to reliably get its attachments.
            review_msg = await interaction.channel.fetch_message(self.listing_message_id)
            print(f"[APPROVE] fetching message id={self.listing_message_id} in channel={interaction.channel.id}")
            print(f"[APPROVE] forum_channel={forum_channel!r} type={type(forum_channel)}")
            print(f"[APPROVE] review_msg attachments found: {[a.filename for a in review_msg.attachments]}")
            print(f"[APPROVE] review_msg embed image urls: "
                  f"{[e.image.url if e.image else None for e in review_msg.embeds]}")

            # Download the actual rendered images from the embeds directly —
            # message.attachments has proven unreliable for this pattern.
            photo_files = await download_embed_images(review_msg.embeds, filename_prefix="listing")
            for f in photo_files:
                f.fp.seek(0)  # ensure the read position is at the start before uploading
            print(f"[APPROVE] photo_files built: {[f.filename for f in photo_files]}")
            image_filenames = [f.filename for f in photo_files]

            embeds = build_listing_embeds(
                items=self.items,
                description=self.description,
                author=seller if seller else self.author_id,
                image_filenames=image_filenames,
                status=f"Approved by {interaction.user.display_name}",
                color=discord.Color.green(),
                listing_type=self.listing_type,
            )

            if isinstance(forum_channel, discord.ForumChannel):
                first_item = self.items[0][0] if self.items else "New Listing"
                tag = TYPE_TAGS.get(self.listing_type, "")
                thread_name = f"{tag} {first_item} — {seller.display_name if seller else 'Seller'}".strip()[:100]
                # Create the post with just a placeholder first, then send the
                # real content as a follow-up — attaching files directly on
                # ForumChannel.create_thread() is unreliable in discord.py.
                thread_with_message = await forum_channel.create_thread(
                    name=thread_name, content="📋 New marketplace listing:"
                )
                print(f"[APPROVE] thread created: {thread_with_message.thread.id}, sending {len(photo_files)} photo(s)")
                sent = await thread_with_message.thread.send(embeds=embeds, files=photo_files)
                print(f"[APPROVE] follow-up message sent, attachments on it: {[a.filename for a in sent.attachments]}")
            elif forum_channel:
                # Fallback if MARKETPLACE_CHANNEL_ID isn't actually a forum channel
                await forum_channel.send(embeds=embeds, files=photo_files)
        except Exception as e:
            print(f"[APPROVE] ERROR: {e!r}")
            import traceback
            traceback.print_exc()
            raise

        await self._notify_seller(
            "✅ Your listing was approved and posted to the marketplace!"
        )
        await self._finalize(interaction, discord.Color.green(),
                              f"✅ Approved by {interaction.user.display_name}")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.resolved:
            await interaction.response.send_message(
                "This listing has already been reviewed.", ephemeral=True
            )
            return
        if not is_mod(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to deny listings.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            DenyReasonModal(review_view=self, review_message=interaction.message)
        )

    async def _notify_seller(self, message: str):
        seller = bot.get_user(self.author_id)
        if seller:
            try:
                await seller.send(message)
            except discord.Forbidden:
                pass

    async def _notify_seller_denied(self, reason: str):
        seller = bot.get_user(self.author_id)
        if not seller:
            return
        embed = discord.Embed(
            title="❌ Listing Denied",
            description=f"**Reason:** {reason}",
            color=discord.Color.red(),
        )
        items_text = "\n".join(f"{name} - {price}" for name, price in self.items)
        prefill = {
            "items_and_prices": items_text,
            "description": self.description,
            "listing_type": self.listing_type,
        }
        try:
            await seller.send(embed=embed, view=ResubmitView(prefill=prefill))
        except discord.Forbidden:
            pass

    async def _finalize(self, interaction: discord.Interaction, color: discord.Color, footer: str):
        for child in self.children:
            child.disabled = True

        # Update the listing message's own embed (color + footer)
        try:
            listing_msg = await interaction.channel.fetch_message(self.listing_message_id)
            if listing_msg.embeds:
                listing_msg.embeds[0].color = color
                listing_msg.embeds[0].set_footer(text=footer)
                # Must explicitly pass attachments= on edit, or Discord wipes
                # the existing photo attachments on this message.
                await listing_msg.edit(embeds=listing_msg.embeds, attachments=listing_msg.attachments)
        except (discord.NotFound, discord.HTTPException):
            pass

        await interaction.response.edit_message(content=footer, view=self)


# ---------- Slash command entry point ----------

@bot.tree.command(name="sell", description="Post a listing: sell, buy, or trade")
async def sell(interaction: discord.Interaction):
    await interaction.response.send_message(
        "What kind of listing is this?", view=ListingTypeView(), ephemeral=True
    )


@bot.tree.command(name="sold", description="Mark your listing as sold (run this inside your listing's post)")
async def sold(interaction: discord.Interaction):
    await mark_listing_status(interaction, status="SOLD", lock=True)


@bot.tree.command(name="pending", description="Mark your listing as pending (run this inside your listing's post)")
async def pending(interaction: discord.Interaction):
    await mark_listing_status(interaction, status="PENDING", lock=False)


@bot.tree.command(name="marketplaceban", description="[Mod] Revoke a user's marketplace access, even if they re-accept the rules")
@discord.app_commands.describe(member="The member to ban from the marketplace")
async def marketplaceban(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction.user):
        await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
        return

    banned_user_ids.add(member.id)
    await save_banned_list()

    removed_note = ""
    if MARKETPLACE_ACCESS_ROLE_ID:
        access_role = interaction.guild.get_role(MARKETPLACE_ACCESS_ROLE_ID)
        if access_role and access_role in member.roles:
            try:
                await member.remove_roles(access_role, reason=f"Marketplace ban by {interaction.user}")
                removed_note = " Their current marketplace access was also removed."
            except discord.Forbidden:
                removed_note = " (Couldn't remove their current access role — check my role position.)"

    await interaction.response.send_message(
        f"🚫 {member.mention} is now banned from the marketplace.{removed_note}", ephemeral=True
    )


@bot.tree.command(name="marketplaceunban", description="[Mod] Restore a user's ability to have marketplace access")
@discord.app_commands.describe(member="The member to unban from the marketplace")
async def marketplaceunban(interaction: discord.Interaction, member: discord.Member):
    if not is_mod(interaction.user):
        await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
        return

    banned_user_ids.discard(member.id)
    await save_banned_list()
    await interaction.response.send_message(
        f"✅ {member.mention} can have marketplace access again.", ephemeral=True
    )


@bot.tree.command(name="marketplacebanlist", description="[Mod] List users currently banned from the marketplace")
async def marketplacebanlist(interaction: discord.Interaction):
    if not is_mod(interaction.user):
        await interaction.response.send_message("You don't have permission to do this.", ephemeral=True)
        return

    if not banned_user_ids:
        await interaction.response.send_message("No one is currently banned from the marketplace.", ephemeral=True)
        return

    lines = "\n".join(f"<@{uid}>" for uid in sorted(banned_user_ids))
    await interaction.response.send_message(f"**Banned from marketplace:**\n{lines}", ephemeral=True)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Enforces marketplace bans: if a banned user gains marketplace access
    again (e.g. re-reacting to Dyno's rules message), strip it right back off."""
    if not MARKETPLACE_ACCESS_ROLE_ID or after.id not in banned_user_ids:
        return
    had_access = any(r.id == MARKETPLACE_ACCESS_ROLE_ID for r in before.roles)
    has_access = any(r.id == MARKETPLACE_ACCESS_ROLE_ID for r in after.roles)
    if has_access and not had_access:
        await enforce_ban_on_member(after, reason="Marketplace ban enforcement")


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID) if GUILD_ID else None
    if guild:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()

    await load_or_create_banned_list()

    # Catch any bans that should have been enforced while the bot was offline
    # (e.g. someone re-accepted the rules during a restart/redeploy).
    real_guild = bot.get_guild(GUILD_ID)
    if real_guild and banned_user_ids:
        for uid in list(banned_user_ids):
            member = real_guild.get_member(uid)
            if member is None:
                try:
                    member = await real_guild.fetch_member(uid)
                except discord.NotFound:
                    continue
            await enforce_ban_on_member(member, reason="Marketplace ban enforcement (startup sweep)")

    print(f"Logged in as {bot.user} — ready.")


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
