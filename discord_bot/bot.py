import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiohttp
import os
from dotenv import load_dotenv
import json
from typing import Optional

load_dotenv()

# Bot configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
API_URL = os.getenv("API_URL", "http://localhost:8080")
WS_URL = os.getenv("WS_URL", "ws://localhost:8080")

# Store active connections per guild
active_connections = {}
print("test")
class SoundboardBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        
    async def setup_hook(self):
        await self.tree.sync()
        print(f"Synced slash commands")

bot = SoundboardBot()

class SoundboardConnection:
    def __init__(self, voice_client, room_id, guild_id):
        self.voice_client = voice_client
        self.room_id = room_id
        self.guild_id = guild_id
        self.ws = None
        self.session = None
        self.running = False
        
    async def connect(self):
        """Connect to soundboard WebSocket"""
        print("got in")
        print(self.voice_client)
        print(self.room_id)
        self.session = aiohttp.ClientSession()
        try:
            self.ws = await self.session.ws_connect(f"{WS_URL}/ws/{self.room_id}")
            self.running = True
            print(f"Connected to soundboard room: {self.room_id}")
            
            # Start listening for sounds
            asyncio.create_task(self.listen_for_sounds())
            
        except Exception as e:
            print(f"Failed to connect to soundboard: {e}")
            await self.disconnect()
            
    async def listen_for_sounds(self):
        """Listen for sound events from WebSocket"""
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    
                    if data.get("type") == "play_sound":
                        sound_id = data.get("soundId")
                        await self.play_sound(sound_id)
                        
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"WebSocket error: {self.ws.exception()}")
                    break
                    
        except Exception as e:
            print(f"Error listening for sounds: {e}")
        finally:
            await self.disconnect()
            
    async def play_sound(self, sound_id: str):
        """Download and play a sound in voice channel"""
        if not self.voice_client or not self.voice_client.is_connected():
            return
            
        try:
            # Download the sound file
            async with self.session.get(f"{API_URL}/api/sounds/{sound_id}/audio") as resp:
                if resp.status != 200:
                    print(f"Failed to download sound {sound_id}")
                    return
                    
                # Save temporarily
                temp_file = f"temp_{self.guild_id}_{sound_id}.mp3"
                with open(temp_file, 'wb') as f:
                    f.write(await resp.read())
                
                # Wait if currently playing
                if self.voice_client.is_playing():
                    self.voice_client.stop()
                    await asyncio.sleep(0.1)
                
                # Play the sound
                audio_source = discord.FFmpegPCMAudio(temp_file)
                self.voice_client.play(
                    audio_source,
                    after=lambda e: asyncio.run_coroutine_threadsafe(
                        self.cleanup_temp_file(temp_file), 
                        bot.loop
                    )
                )
                print(f"Playing sound: {sound_id}")
                
        except Exception as e:
            print(f"Error playing sound: {e}")
            
    async def cleanup_temp_file(self, filename: str):
        """Clean up temporary audio file"""
        await asyncio.sleep(1)  # Wait a bit to ensure playback started
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except Exception as e:
            print(f"Error cleaning up temp file: {e}")
            
    async def disconnect(self):
        """Disconnect from soundboard and voice channel"""
        self.running = False
        
        if self.ws:
            await self.ws.close()
            
        if self.session:
            await self.session.close()
            
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()
            
        if self.guild_id in active_connections:
            del active_connections[self.guild_id]
            
        print(f"Disconnected from room: {self.room_id}")

@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")
    print(f"Connected to {len(bot.guilds)} guilds")

@bot.tree.command(name="join", description="Join a voice channel and connect to a soundboard room")
@app_commands.describe(
    room="The soundboard room code to connect to",
    channel="The voice channel to join (optional, defaults to your current channel)"
)
async def join_command(
    interaction: discord.Interaction,
    room: str,
    channel: Optional[discord.VoiceChannel] = None
):
    """Join voice channel and connect to soundboard room"""
    
    # Defer the response as this might take a moment
    await interaction.response.defer()

    # Check if already connected in this guild
    if interaction.guild.id in active_connections:
        await interaction.followup.send("❌ Already connected in this server. Use `/leave` first.")
        return
    
    # Determine which channel to join
    voice_channel = channel
    if not voice_channel:
        # Try to get user's current voice channel
        print(interaction.user)
        if interaction.user.voice:
            voice_channel = interaction.user.voice.channel
        else:
            await interaction.followup.send("❌ You need to be in a voice channel or specify one!")
            return
    
    try:
        # Connect to voice channel
        voice_client = await voice_channel.connect(reconnect=True)
        print("connected to voice")
        # Create soundboard connection
        connection = SoundboardConnection(voice_client, room, interaction.guild.id)
        await connection.connect()
        
        active_connections[interaction.guild.id] = connection
        
        await interaction.followup.send(
            f"✅ Connected to voice channel **{voice_channel.name}** and soundboard room **{room}**!\n"
            f"🔊 I'll play any sounds from that room here."
        )
        
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to connect: {str(e)}")
        print(f"Error in join command: {e}")

@bot.tree.command(name="leave", description="Disconnect from voice channel and soundboard")
async def leave_command(interaction: discord.Interaction):
    """Disconnect from voice and soundboard"""
    
    if interaction.guild.id not in active_connections:
        await interaction.response.send_message("❌ Not connected to any room in this server.")
        return
    
    connection = active_connections[interaction.guild.id]
    await connection.disconnect()
    
    await interaction.response.send_message("👋 Disconnected from voice and soundboard.")

@bot.tree.command(name="status", description="Check bot connection status")
async def status_command(interaction: discord.Interaction):
    """Check current connection status"""
    
    if interaction.guild.id not in active_connections:
        await interaction.response.send_message("❌ Not connected to any room.")
        return
    
    connection = active_connections[interaction.guild.id]
    
    voice_status = "✅ Connected" if connection.voice_client.is_connected() else "❌ Disconnected"
    ws_status = "✅ Connected" if connection.running else "❌ Disconnected"
    
    await interaction.response.send_message(
        f"**Connection Status**\n"
        f"Voice Channel: {voice_status}\n"
        f"Soundboard Room: {ws_status}\n"
        f"Room Code: `{connection.room_id}`"
    )

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN not set in .env file")
        exit(1)
    
    bot.run(DISCORD_TOKEN)
