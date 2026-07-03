"""
음성/화상 채널 참여(입퇴장) 기록 cog.

랭킹보드의 '라운지(화상) 참여 점수'용 원천 데이터를 쌓는다.
- on_voice_state_update 로 입장/이동/퇴장을 감지해 세션(userId, channelId, 시간)을 기록.
- 저장: voice_sessions_{env}.json (BASE_DIR). admin_server 가 나중에 읽어 점수화.
- 음성은 메시지처럼 과거 조회가 안 되므로, 이 cog 이 붙은 시점부터만 집계된다.
- 어뷰징 방지: 세션당 최대 2시간 캡, 1분 미만 세션 무시.

봇 전체를 죽이지 않도록 모든 I/O 는 방어적으로 처리한다(로드 실패해도 빈 상태로 시작).
"""
import discord
from discord.ext import commands
import json
import os
import time
import logging
from datetime import datetime, timezone

from config import BASE_DIR

logger = logging.getLogger('cogs.voice_tracker')

_ENV = (os.getenv('ASC_ENV') or 'test').strip().lower() or 'test'
VOICE_FILE = os.path.join(BASE_DIR, f"voice_sessions_{_ENV}.json")

MAX_SESSION_SEC = 2 * 60 * 60   # 세션당 캡 2시간 (켜놓고 잠수 방지)
MIN_SESSION_SEC = 60            # 1분 미만은 무시 (스쳐 지나감)


class VoiceTrackerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._active = {}          # user_id(str) -> {channelId, guildId, joinedAt(epoch)}
        self._data = self._load()

    def _load(self):
        try:
            with open(VOICE_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get('sessions'), list):
                return d
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            logger.warning(f"[voice] load failed: {e}")
        return {'sessions': []}

    def _save(self):
        try:
            with open(VOICE_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[voice] save failed: {e}")

    def _close_session(self, uid, now):
        sess = self._active.pop(uid, None)
        if not sess:
            return
        dur = min(int(now - sess.get('joinedAt', now)), MAX_SESSION_SEC)
        if dur < MIN_SESSION_SEC:
            return
        self._data['sessions'].append({
            'userId': uid,
            'channelId': sess.get('channelId'),
            'guildId': sess.get('guildId'),
            'joinedAt': datetime.fromtimestamp(sess['joinedAt'], timezone.utc).isoformat(),
            'durationSec': dur,
        })
        self._save()

    @commands.Cog.listener()
    async def on_ready(self):
        # 재시작 시 이미 음성에 있는 멤버를 '지금'부터 세션 시작으로 seed (진행중 세션 유실 최소화).
        now = time.time()
        seeded = 0
        try:
            for guild in self.bot.guilds:
                for vc in guild.voice_channels:
                    for m in vc.members:
                        if m.bot:
                            continue
                        if str(m.id) not in self._active:
                            self._active[str(m.id)] = {
                                'channelId': str(vc.id),
                                'guildId': str(guild.id),
                                'joinedAt': now,
                            }
                            seeded += 1
        except Exception as e:
            logger.warning(f"[voice] on_ready seed failed: {e}")
        if seeded:
            logger.info(f"[voice] seeded {seeded} active voice sessions on ready")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        b = before.channel.id if before.channel else None
        a = after.channel.id if after.channel else None
        if b == a:
            return  # 음소거/화면공유 등 — 채널 이동 아님
        now = time.time()
        uid = str(member.id)
        try:
            if b is not None:
                self._close_session(uid, now)
            if a is not None:
                self._active[uid] = {
                    'channelId': str(a),
                    'guildId': str(member.guild.id),
                    'joinedAt': now,
                }
        except Exception as e:
            logger.warning(f"[voice] state update failed for {uid}: {e}")


async def setup(bot):
    await bot.add_cog(VoiceTrackerCog(bot))
