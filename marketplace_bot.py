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
3. Invite the bot with "applications.commands" + "bot" scopes and these
   permissions: Send Messages, Embed Links, Manage Messages (in review
   channel), Read Message History, Create Posts / Send Messages in
   Threads, and Manage Threads (in the marketplace forum — needed for
   /sold and /pending to rename and lock listing posts).
4. IMPORTANT for the mod ping to actually show up: go to Server
   Settings > Roles > (your mod role) and enable "Allow anyone to
   @mention this role" — otherwise Discord silently won't ping it
   unless the bot also has the "Mention Everyone" permission.
5. Run: python marketplace_bot.py
"""

import asyncio
import os
import re
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MOD_REVIEW_CHANNEL_ID = int(os.getenv("MOD_REVIEW_CHANNEL_ID", "0"))
MARKETPLACE_CHANNEL_ID = int(os.getenv("MARKETPLACE_CHANNEL_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))

MAX_PHOTOS = 5
PHOTO_WAIT_SECONDS = 180  # how long we wait for the seller to send photos

intents = discord.Intents.default()
intents.members = True  # needed to DM users reliably
intents.message_content = True  # needed to see attachments on the seller's photo message

bot = commands.Bot(command_prefix="!", intents=intents)
print(f"[STARTUP] discord.py version: {discord.__version__}")


def is_mod(member: discord.Member) -> bool:
    return any(role.id == MOD_ROLE_ID for role in member.roles)


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


def build_listing_embeds(items, description, author, image_filenames, status, color):
    """image_filenames are the names of files being sent ALONGSIDE this
    embed in the same message (via the `files=` kwarg) — referenced with
    the special attachment://<filename> scheme so the image is permanently
    hosted on this message rather than pointing at someone else's."""
    lines = "\n".join(f"• **{name}** — {price}" for name, price in items) or "No items listed."
    main_embed = discord.Embed(
        title="New Marketplace Listing",
        description=description or "No description provided.",
        color=color,
    )
    main_embed.add_field(name="Items & Prices", value=lines, inline=False)
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


async def get_thread_seller_id(thread: discord.Thread):
    """Reads the 'Seller ID: <id>' footer we stamped on the listing embed
    to figure out who posted it, since the thread's own owner is the bot."""
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


# ---------- Submission Modal (text fields only) ----------

class ListingModal(discord.ui.Modal, title="New Marketplace Listing"):
    items_and_prices = discord.ui.TextInput(
        label="Items & Prices (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="Charizard VMAX Rainbow Rare - $45\nPikachu V - $20 or trade",
        max_length=1000,
    )
    description = discord.ui.TextInput(
        label="Description / Condition",
        style=discord.TextStyle.paragraph,
        placeholder="Condition, shipping info, extra details...",
        max_length=500,
        required=False,
    )

    def __init__(self, prefill: dict | None = None):
        super().__init__()
        if prefill:
            self.items_and_prices.default = prefill.get("items_and_prices")
            self.description.default = prefill.get("description")

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
        )

        view = ReviewView(
            author_id=interaction.user.id,
            items=items,
            description=self.description.value,
        )

        mod_ping = f"<@&{MOD_ROLE_ID}>" if MOD_ROLE_ID else ""
        # Send the listing (with photos) and the Approve/Deny buttons as TWO
        # separate messages — combining files= and view= in one send has
        # been unreliable for actually attaching the files.
        sent_review_msg = await review_channel.send(
            content=f"{mod_ping} 📥 New listing awaiting approval:",
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
        await interaction.response.send_modal(ListingModal(prefill=self.prefill))


# ---------- Approve / Deny Buttons ----------

class ReviewView(discord.ui.View):
    def __init__(self, author_id: int, items: list, description: str):
        super().__init__(timeout=None)
        self.author_id = author_id
        self.items = items
        self.description = description
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

            # Re-host the same images (already permanently attached to this review
            # message) onto the new forum post, rather than reusing URLs.
            photo_files = [await att.to_file() for att in review_msg.attachments]
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
            )

            if isinstance(forum_channel, discord.ForumChannel):
                first_item = self.items[0][0] if self.items else "New Listing"
                thread_name = f"{first_item} — {seller.display_name if seller else 'Seller'}"[:100]
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

@bot.tree.command(name="sell", description="Submit a marketplace listing for mod approval")
async def sell(interaction: discord.Interaction):
    await interaction.response.send_modal(ListingModal())


@bot.tree.command(name="sold", description="Mark your listing as sold (run this inside your listing's post)")
async def sold(interaction: discord.Interaction):
    await mark_listing_status(interaction, status="SOLD", lock=True)


@bot.tree.command(name="pending", description="Mark your listing as pending (run this inside your listing's post)")
async def pending(interaction: discord.Interaction):
    await mark_listing_status(interaction, status="PENDING", lock=False)


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID) if GUILD_ID else None
    if guild:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    print(f"Logged in as {bot.user} — ready.")


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
