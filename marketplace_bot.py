"""
Marketplace Approval Bot
-------------------------
Members use /sell to submit a listing. It goes to a mod-review channel
for Approve/Deny. Approved listings post to your public marketplace
channel. Denied listings DM the member with the mod's reason, plus an
"Edit & Resubmit" button that reopens a pre-filled form.

SETUP:
1. pip install discord.py python-dotenv
2. Create a .env file next to this script with:
     BOT_TOKEN=your_bot_token_here
     GUILD_ID=your_server_id
     MOD_REVIEW_CHANNEL_ID=channel_id_for_mods_only
     MARKETPLACE_CHANNEL_ID=channel_id_for_public_listings
     MOD_ROLE_ID=role_id_that_can_approve
3. Invite the bot with "applications.commands" + "bot" scopes and these
   permissions: Send Messages, Embed Links, Manage Messages (in review channel),
   Read Message History.
4. Run: python marketplace_bot.py
"""

import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MOD_REVIEW_CHANNEL_ID = int(os.getenv("MOD_REVIEW_CHANNEL_ID", "0"))
MARKETPLACE_CHANNEL_ID = int(os.getenv("MARKETPLACE_CHANNEL_ID", "0"))
MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0"))

intents = discord.Intents.default()
intents.members = True  # needed to DM users reliably

bot = commands.Bot(command_prefix="!", intents=intents)


def is_mod(member: discord.Member) -> bool:
    return any(role.id == MOD_ROLE_ID for role in member.roles)


# ---------- Submission / Edit Modal ----------

class ListingModal(discord.ui.Modal, title="New Marketplace Listing"):
    item_name = discord.ui.TextInput(
        label="Item Name",
        placeholder="e.g. Charizard VMAX Rainbow Rare",
        max_length=100,
    )
    price = discord.ui.TextInput(
        label="Price",
        placeholder="e.g. $45 or trade offers",
        max_length=50,
    )
    description = discord.ui.TextInput(
        label="Description / Condition",
        style=discord.TextStyle.paragraph,
        placeholder="Condition, shipping info, extra details...",
        max_length=500,
        required=False,
    )
    image_url = discord.ui.TextInput(
        label="Image URL (optional)",
        placeholder="https://...",
        required=False,
    )

    def __init__(self, prefill: dict | None = None):
        super().__init__()
        if prefill:
            self.item_name.default = prefill.get("item_name")
            self.price.default = prefill.get("price")
            self.description.default = prefill.get("description")
            self.image_url.default = prefill.get("image_url")

    async def on_submit(self, interaction: discord.Interaction):
        review_channel = bot.get_channel(MOD_REVIEW_CHANNEL_ID)
        if review_channel is None:
            await interaction.response.send_message(
                "Couldn't reach the mod review channel. Contact an admin.",
                ephemeral=True,
            )
            return

        embed = build_listing_embed(
            item_name=self.item_name.value,
            price=self.price.value,
            description=self.description.value,
            image_url=self.image_url.value,
            author=interaction.user,
            status="Pending Review",
            color=discord.Color.yellow(),
        )

        view = ReviewView(
            author_id=interaction.user.id,
            item_name=self.item_name.value,
            price=self.price.value,
            description=self.description.value,
            image_url=self.image_url.value,
        )

        await review_channel.send(
            content="📥 New listing awaiting approval:", embed=embed, view=view
        )

        await interaction.response.send_message(
            "Your listing was submitted for mod approval. You'll be notified once reviewed.",
            ephemeral=True,
        )


def build_listing_embed(item_name, price, description, image_url, author, status, color):
    embed = discord.Embed(
        title=item_name,
        description=description or "No description provided.",
        color=color,
    )
    embed.add_field(name="Price", value=price, inline=True)
    embed.add_field(name="Seller", value=author.mention if hasattr(author, "mention") else f"<@{author}>", inline=True)
    embed.set_footer(text=f"Seller ID: {getattr(author, 'id', author)} • {status}")
    if image_url:
        embed.set_image(url=image_url)
    return embed


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
        await v._notify_seller_denied(self.reason.value)

        for child in v.children:
            child.disabled = True
        embed = self.review_message.embeds[0]
        embed.color = discord.Color.red()
        embed.set_footer(text=f"❌ Denied by {interaction.user.display_name}")
        await self.review_message.edit(embed=embed, view=v)

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
    def __init__(self, author_id: int, item_name: str, price: str,
                 description: str, image_url: str):
        super().__init__(timeout=None)
        self.author_id = author_id
        self.item_name = item_name
        self.price = price
        self.description = description
        self.image_url = image_url

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_mod(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to approve listings.", ephemeral=True
            )
            return

        marketplace_channel = bot.get_channel(MARKETPLACE_CHANNEL_ID)
        seller = interaction.guild.get_member(self.author_id)

        embed = build_listing_embed(
            item_name=self.item_name,
            price=self.price,
            description=self.description,
            image_url=self.image_url,
            author=seller if seller else self.author_id,
            status=f"Approved by {interaction.user.display_name}",
            color=discord.Color.green(),
        )

        if marketplace_channel:
            await marketplace_channel.send(embed=embed)

        await self._notify_seller(
            f"✅ Your listing **{self.item_name}** was approved and posted!"
        )
        await self._finalize(interaction, discord.Color.green(),
                              f"✅ Approved by {interaction.user.display_name}")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_mod(interaction.user):
            await interaction.response.send_message(
                "You don't have permission to deny listings.", ephemeral=True
            )
            return
        # Opens a modal to collect the reason; finalization happens in DenyReasonModal.on_submit
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
            title=f"❌ Listing Denied: {self.item_name}",
            description=f"**Reason:** {reason}",
            color=discord.Color.red(),
        )
        prefill = {
            "item_name": self.item_name,
            "price": self.price,
            "description": self.description,
            "image_url": self.image_url,
        }
        try:
            await seller.send(embed=embed, view=ResubmitView(prefill=prefill))
        except discord.Forbidden:
            pass

    async def _finalize(self, interaction: discord.Interaction, color: discord.Color, footer: str):
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.color = color
        embed.set_footer(text=footer)
        await interaction.response.edit_message(embed=embed, view=self)


# ---------- Slash command entry point ----------

@bot.tree.command(name="sell", description="Submit a marketplace listing for mod approval")
async def sell(interaction: discord.Interaction):
    await interaction.response.send_modal(ListingModal())


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
