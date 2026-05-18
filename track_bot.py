"""
ASC Track Bot — Discord bot dedicated to track application + group assignment provisioning.

Responsibilities:
  - Listen for IPC commands from admin_server.py (track-apply Flask backend)
  - Create Discord categories / channels / roles per track / group
  - Manage light track member access to parent track channels
  - Provide cleanup commands (e.g., !채널삭제 <cohort>) — deletes a cohort's track channels, preserving 공지 channels / categories / roles

Loads only cogs.ipc + cogs.admin so no scheduler / announcement / notion_sync
background tasks ever run inside this process.

Run:
  ASC_ENV=test python3 track_bot.py        # test workspace + test Discord guild
  ASC_ENV=prod python3 track_bot.py        # production track Discord guild

Env requirements:
  - DISCORD_BOT_TOKEN      (the track bot's Discord token)
  - NOTION_TOKEN           (Notion integration token for the workspace)
  - In test mode: TEST_DISCORD_GUILD_ID
"""
import os
import sys
import logging
import asyncio
import discord
from discord.ext import commands

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from env_utils import load_backend_env, is_prod_guild_blacklisted

env_info = load_backend_env(BASE_DIR)
ENV_NAME = env_info.get('env_name')
print(f"[TrackBot] Env mode: {ENV_NAME}")
print(f"[TrackBot] Loaded files: {env_info.get('loaded_files')}")

# Test 모드 가드: TEST_DISCORD_GUILD_ID 필수, prod 블랙리스트 길드면 즉시 거부.
EXPECTED_TEST_GUILD_ID = None
if ENV_NAME == 'test':
    raw = os.getenv('TEST_DISCORD_GUILD_ID', '').strip()
    if not raw:
        raise RuntimeError("TEST_DISCORD_GUILD_ID 환경변수가 설정되지 않았습니다 (test 모드).")
    try:
        EXPECTED_TEST_GUILD_ID = int(raw)
    except ValueError:
        raise RuntimeError(f"TEST_DISCORD_GUILD_ID 가 숫자가 아닙니다: {raw!r}")
    if is_prod_guild_blacklisted(EXPECTED_TEST_GUILD_ID):
        raise RuntimeError(
            f"TEST_DISCORD_GUILD_ID={EXPECTED_TEST_GUILD_ID} 는 PROD 블랙리스트입니다. 거부합니다."
        )

token = os.getenv('DISCORD_BOT_TOKEN', '').strip()
if not token:
    raise RuntimeError("DISCORD_BOT_TOKEN 이 환경변수에 없습니다.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('track_bot')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"   Connected guilds: {[(g.id, g.name) for g in bot.guilds]}")

    if ENV_NAME == 'test':
        test_guild = next((g for g in bot.guilds if g.id == EXPECTED_TEST_GUILD_ID), None)
        if not test_guild:
            logger.error(
                f"❌ 봇이 테스트 길드 {EXPECTED_TEST_GUILD_ID} 에 가입돼 있지 않습니다. 종료합니다."
            )
            await bot.close()
            return

        extra_guilds = [g for g in bot.guilds if g.id != EXPECTED_TEST_GUILD_ID]
        if extra_guilds:
            logger.warning(
                f"⚠️  봇이 테스트 길드 외 다른 길드에도 가입돼 있습니다: "
                f"{[(g.id, g.name) for g in extra_guilds]}"
            )
        else:
            logger.info(f"✅ 테스트 길드 단독 가입 확인: {test_guild.name} ({test_guild.id})")
    else:
        # Prod 모드: 블랙리스트 가드는 admin cog 의 명령어 단위에서 처리.
        for g in bot.guilds:
            if is_prod_guild_blacklisted(g.id):
                logger.warning(
                    f"⚠️  prod 블랙리스트 길드에 가입돼 있습니다: {g.id} ({g.name}) — "
                    f"트랙 봇은 prod 워크스페이스 길드에는 들어가면 안 됩니다."
                )


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)


async def load_extensions():
    """오직 ipc + admin 만 로드. background scheduler / announcement / notion_sync 차단."""
    for ext in ('cogs.ipc', 'cogs.admin'):
        try:
            await bot.load_extension(ext)
            logger.info(f"  ✓ Loaded: {ext}")
        except Exception as e:
            logger.error(f"  ✗ Failed to load {ext}: {e}")
            raise


async def main():
    async with bot:
        await load_extensions()
        await bot.start(token)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[TrackBot] Shutdown requested.")
