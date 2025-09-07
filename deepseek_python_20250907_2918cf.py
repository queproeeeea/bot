import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import time
import logging
import re
from typing import List, Dict, Optional, Set
from datetime import datetime

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('mass_dm_log.txt', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Configuration
CONFIG_FILE = 'config.json'
RATE_LIMIT_DELAY = 0.1  # Reduced delay for faster sending (BE CAREFUL!)
BATCH_SIZE = 20  # Increased batch size for faster sending
MAX_CONCURRENT_TASKS = 25  # Maximum number of concurrent DM tasks

# Your Discord ID - ONLY YOU CAN USE ALL COMMANDS
BOT_OWNER_ID = 1130943268708962365

class MassDMBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.servers = self.load_config().get('servers', {})
        self.log_channel_id = self.load_config().get('log_channel_id')
        self.user_message_log_channel_id = self.load_config().get('user_message_log_channel_id')
        self.invite_log_channel_id = self.load_config().get('invite_log_channel_id')
        self.anti_invite_enabled = self.load_config().get('anti_invite_enabled', {})
        self.sent_messages = 0
        self.failed_messages = 0
        self.start_time = None
        self.current_task = None
        self.is_running = False
        self.processed_users = set()  # Track users we've already messaged
        self.currently_processing_server = None  # Track which server is being processed
        self.unreachable_users = []  # Track users who couldn't be reached
        # Regex pattern to detect Discord invites
        self.invite_pattern = re.compile(r'(discord\.(gg|me|io|com/invite)/[a-zA-Z0-9-]+)')

    def load_config(self) -> Dict:
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            return {'servers': {}, 'log_channel_id': None, 'user_message_log_channel_id': None, 
                    'invite_log_channel_id': None, 'anti_invite_enabled': {}}

    def save_config(self):
        config = {
            'servers': self.servers,
            'log_channel_id': self.log_channel_id,
            'user_message_log_channel_id': self.user_message_log_channel_id,
            'invite_log_channel_id': self.invite_log_channel_id,
            'anti_invite_enabled': self.anti_invite_enabled
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)

    async def on_ready(self):
        logging.info(f'Logged in as {self.user.name} (ID: {self.user.id})')
        logging.info('------')
        
        # Sync slash commands
        try:
            synced = await self.tree.sync()
            logging.info(f"Synced {len(synced)} command(s)")
        except Exception as e:
            logging.error(f"Failed to sync commands: {e}")

    async def log_to_channel(self, message: str, channel_type: str = "log"):
        """Send a log message to the designated logging channel"""
        if channel_type == "log":
            channel_id = self.log_channel_id
        elif channel_type == "user_message":
            channel_id = self.user_message_log_channel_id
        elif channel_type == "invite":
            channel_id = self.invite_log_channel_id
        else:
            return
            
        if not channel_id:
            return
            
        channel = self.get_channel(channel_id)
        if channel:
            try:
                # Split long messages to avoid Discord's character limit
                if len(message) > 2000:
                    chunks = [message[i:i+2000] for i in range(0, len(message), 2000)]
                    for chunk in chunks:
                        await channel.send(f"```{chunk}```")
                else:
                    await channel.send(f"```{message}```")
            except Exception as e:
                logging.error(f"Failed to send log to channel: {e}")

    async def auto_log_dm_message(self, message: discord.Message):
        """Automatically log ONLY direct messages (DMs) sent to the bot"""
        if not self.user_message_log_channel_id:
            return
            
        # Don't log bot's own messages
        if message.author.bot:
            return
            
        # Log ONLY direct messages (DMs), not server messages
        if isinstance(message.channel, discord.DMChannel):
            log_message = (
                f"📩 DM Received from User\n"
                f"• From: {message.author} (ID: {message.author.id})\n"
                f"• Content: {message.content}\n"
                f"• Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            # Check for attachments
            if message.attachments:
                attachment_info = "\n• Attachments: " + ", ".join([a.filename for a in message.attachments])
                log_message += attachment_info
            
            logging.info(f"Auto-logged DM from {message.author}: {message.content}")
            await self.log_to_channel(log_message, "user_message")

    async def auto_log_dm_start(self, server_id: str, server_name: str, total_users: int):
        """Automatically log when DM process starts on a server"""
        if not self.log_channel_id:
            return
            
        log_message = (
            f"🚀 Auto-Log: DM Process Started\n"
            f"• Server: {server_name} (ID: {server_id})\n"
            f"• Target Users: {total_users}\n"
            f"• Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        logging.info(f"Auto-logged DM start for server {server_name} with {total_users} users")
        await self.log_to_channel(log_message)

    async def auto_log_dm_completion(self, server_id: str, server_name: str, success: int, failed: int, elapsed: float):
        """Automatically log when DM process completes on a server"""
        if not self.log_channel_id:
            return
            
        log_message = (
            f"✅ Auto-Log: DM Process Completed\n"
            f"• Server: {server_name} (ID: {server_id})\n"
            f"• Successful: {success}\n"
            f"• Failed: {failed}\n"
            f"• Time Taken: {elapsed:.1f}s\n"
            f"• Completion Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        logging.info(f"Auto-logged DM completion for server {server_name}: {success} success, {failed} failed")
        await self.log_to_channel(log_message)

    async def log_unreachable_users(self, server_name: str):
        """Log users who couldn't be reached"""
        if not self.log_channel_id or not self.unreachable_users:
            return
            
        log_message = (
            f"❌ Unreachable Users in {server_name}\n"
            f"• Total Unreachable: {len(self.unreachable_users)}\n"
        )
        
        # Add first 10 unreachable users to the log
        for i, user_info in enumerate(self.unreachable_users[:10]):
            log_message += f"• {user_info}\n"
        
        if len(self.unreachable_users) > 10:
            log_message += f"• ... and {len(self.unreachable_users) - 10} more\n"
        
        log_message += f"• Log Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        logging.info(f"Logged {len(self.unreachable_users)} unreachable users for server {server_name}")
        await self.log_to_channel(log_message)

    async def log_invite_deletion(self, message: discord.Message, invite_links: list):
        """Log when a message with Discord invites is deleted"""
        if not self.invite_log_channel_id:
            return
            
        log_message = (
            f"🚫 Discord Invite Deleted\n"
            f"• User: {message.author} (ID: {message.author.id})\n"
            f"• Channel: #{message.channel.name} (ID: {message.channel.id})\n"
            f"• Server: {message.guild.name} (ID: {message.guild.id})\n"
            f"• Invite Links: {', '.join(invite_links)}\n"
            f"• Message Content: {message.content[:100]}{'...' if len(message.content) > 100 else ''}\n"
            f"• Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        logging.info(f"Logged invite deletion from {message.author}: {invite_links}")
        await self.log_to_channel(log_message, "invite")

# Custom check to ensure only you can use ALL commands
def is_bot_owner():
    def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == BOT_OWNER_ID
    return app_commands.check(predicate)

# Custom check to ensure only server admins can use commands in their server
def is_server_admin():
    def predicate(interaction: discord.Interaction) -> bool:
        # Bot owner can always use commands
        if interaction.user.id == BOT_OWNER_ID:
            return True
        
        # Check if user has administrator permissions in the server
        if interaction.guild and interaction.user.guild_permissions.administrator:
            return True
        
        return False
    return app_commands.check(predicate)

# Setup bot with necessary intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.messages = True  # Needed to receive message events
bot = MassDMBot(command_prefix='!', intents=intents)

@bot.event
async def on_message(message: discord.Message):
    """Handle all incoming messages"""
    # Automatically log ONLY direct messages (DMs) sent to the bot
    await bot.auto_log_dm_message(message)
    
    # Check for Discord invites if anti-invite is enabled for this server
    if (message.guild and 
        str(message.guild.id) in bot.anti_invite_enabled and 
        bot.anti_invite_enabled[str(message.guild.id)] and
        not message.author.bot):
        
        # Check for Discord invites
        invite_matches = bot.invite_pattern.findall(message.content.lower())
        if invite_matches:
            # Extract just the invite links
            invite_links = [match[0] for match in invite_matches]
            
            # Delete the message
            try:
                await message.delete()
                logging.info(f"Deleted message with Discord invite from {message.author} in {message.guild.name}")
                
                # Log the deletion
                await bot.log_invite_deletion(message, invite_links)
                
                # Send a warning to the user
                try:
                    warning_msg = await message.channel.send(
                        f"{message.author.mention}, Discord invites are not allowed in this server.",
                        delete_after=10.0
                    )
                except:
                    pass  # Skip if we can't send the warning
                    
            except discord.Forbidden:
                logging.error(f"Missing permissions to delete message in {message.guild.name}")
            except discord.NotFound:
                logging.warning(f"Message already deleted in {message.guild.name}")
            except Exception as e:
                logging.error(f"Error deleting message: {e}")
    
    # Process commands
    await bot.process_commands(message)

# ... (all your existing commands remain the same) ...

@bot.tree.command(name="set_invite_log_channel", description="Set the current channel as the invite logging channel")
@is_bot_owner()
async def set_invite_log_channel(interaction: discord.Interaction):
    """Set the current channel as the invite logging channel - ONLY FOR BOT OWNER"""
    bot.invite_log_channel_id = interaction.channel_id
    bot.save_config()
    
    await interaction.response.send_message(
        f'✅ Set this channel as the invite logging channel.', 
        ephemeral=True
    )
    
    log_msg = f'Set invite logging channel to: #{interaction.channel.name} (ID: {interaction.channel_id})'
    logging.info(log_msg)
    await bot.log_to_channel("Invite logging channel set successfully!")

@bot.tree.command(name="anti_invite_toggle", description="Toggle anti-invite feature for this server")
@is_server_admin()
async def anti_invite_toggle(interaction: discord.Interaction):
    """Toggle anti-invite feature for this server - FOR SERVER ADMINS"""
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
        
    server_id = str(interaction.guild.id)
    
    # Toggle the setting
    current_setting = bot.anti_invite_enabled.get(server_id, False)
    bot.anti_invite_enabled[server_id] = not current_setting
    bot.save_config()
    
    status = "enabled" if bot.anti_invite_enabled[server_id] else "disabled"
    
    await interaction.response.send_message(
        f'✅ Anti-invite feature has been {status} for this server.', 
        ephemeral=True
    )
    
    log_msg = f'Anti-invite feature {status} for server: {interaction.guild.name} (ID: {server_id})'
    logging.info(log_msg)
    await bot.log_to_channel(log_msg)

@bot.tree.command(name="anti_invite_status", description="Check anti-invite status for this server")
@is_server_admin()
async def anti_invite_status(interaction: discord.Interaction):
    """Check anti-invite status for this server - FOR SERVER ADMINS"""
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
        
    server_id = str(interaction.guild.id)
    status = bot.anti_invite_enabled.get(server_id, False)
    
    status_embed = discord.Embed(
        title="🛡️ Anti-Invite Status", 
        color=0x00ff00 if status else 0xff0000,
        timestamp=datetime.now()
    )
    
    status_embed.add_field(
        name="Status", 
        value="✅ Enabled" if status else "❌ Disabled", 
        inline=False
    )
    
    status_embed.add_field(
        name="Logging Channel", 
        value=f"<#{bot.invite_log_channel_id}>" if bot.invite_log_channel_id else "Not set",
        inline=False
    )
    
    status_embed.set_footer(text=f"Server: {interaction.guild.name}")
    
    await interaction.response.send_message(embed=status_embed, ephemeral=True)

# ... (the rest of your existing code remains the same) ...

if __name__ == "__main__":
    # Get the bot token from user input
    token = get_bot_token()
    
    # Run the bot
    try:
        print("Starting bot...")
        bot.run(token)
    except discord.LoginFailure:
        print("Error: Invalid token provided. Please check your token and try again.")
    except Exception as e:
        print(f"An error occurred: {e}")