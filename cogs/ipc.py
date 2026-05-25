import discord
from discord.ext import commands, tasks
import json
import os
import asyncio
import logging
from config import BASE_DIR
import notion_api
from env_utils import get_bot_command_queue_file, resolve_active_guild

logger = logging.getLogger('cogs.ipc')

COMMAND_QUEUE_FILE = get_bot_command_queue_file(BASE_DIR)

class IPCCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_queue.start()

    def cog_unload(self):
        self.check_queue.cancel()

    @tasks.loop(seconds=5.0)
    async def check_queue(self):
        if not os.path.exists(COMMAND_QUEUE_FILE):
            return

        try:
            with open(COMMAND_QUEUE_FILE, 'r', encoding='utf-8') as f:
                try:
                    command = json.load(f)
                except json.JSONDecodeError:
                    return # File might be empty or corrupted temporarily
            
            if command.get('status') == 'pending':
                logger.info(f"[IPC] Processing command: {command['type']} (ID: {command['id']})")
                
                # Update status to processing
                command['status'] = 'processing'
                with open(COMMAND_QUEUE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(command, f, ensure_ascii=False)
                
                logger.info(f"[IPC] Status updated to 'processing' for {command['id']}. Starting process_command...")
                try:
                    await self.process_command(command)
                    command['status'] = 'completed'
                    logger.info(f"[IPC] Command execution finished successfully: {command['id']}")
                except Exception as e:
                    logger.error(f"[IPC] Command execution failed for {command['id']} with error: {e}")
                    command['status'] = 'failed'
                    command['error'] = str(e)
                
                with open(COMMAND_QUEUE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(command, f, ensure_ascii=False)
                    
        except Exception as e:
            logger.warning(f"[IPC] Queue check error: {e}")

    async def _send_dm_to_admin(self, message, title="알림", level="info"):
        """
        Send DM to Server Owner (Admin) or Specific User
        """
        TARGET_ADMIN_ID = 1491970119910424666
        
        if not self.bot.guilds:
            logger.warning("[IPC] Bot is not in any guild, cannot find admin.")
            return

        try:
            guild = resolve_active_guild(self.bot)
        except RuntimeError as e:
            logger.error(f"[IPC] Guild resolution failed (test guard): {e}")
            return
        if not guild:
            logger.warning("[IPC] No resolvable guild, cannot send admin DM.")
            return
        admin = None
        
        # 1. Try Target ID
        try:
            admin = await self.bot.fetch_user(TARGET_ADMIN_ID)
        except Exception as e:
            logger.warning(f"[IPC] Failed to fetch target admin {TARGET_ADMIN_ID}: {e}")
        
        # 2. Fallback removed as per user request (Do not message Server Owner)
        # if not admin:
        #    admin = guild.owner
        
        if not admin:
            logger.warning(f"[IPC] Could not find target admin {TARGET_ADMIN_ID}. Owner fallback disabled.")
            return
            
        color = discord.Color.blue()
        if level == 'success': color = discord.Color.green()
        if level == 'error': color = discord.Color.red()
        
        embed = discord.Embed(title=f"📢 {title}", description=message, color=color)
        embed.set_footer(text=f"Server: {guild.name} | To: {admin.name}")
        
        try:
            await admin.send(embed=embed)
            logger.info(f"[IPC] Sent DM to admin {admin.name} ({admin.id}): {title}")
        except Exception as e:
            logger.error(f"[IPC] Failed to send DM to admin {admin.name}: {e}")

    async def process_command(self, cmd):
        cmd_type = cmd['type']
        
        if cmd_type == 'sync_members':
            admin_cog = self.bot.get_cog('AdminCog')
            if not admin_cog:
                raise Exception("AdminCog is not loaded")
            
            if not self.bot.guilds:
                raise Exception("Bot is not in any guild")

            guild = resolve_active_guild(self.bot)
            if not guild:
                raise Exception("No resolvable guild for sync_members")
            
            async def status_log(msg):
                logger.info(f"[IPC] Sync Status: {msg}")
                # Also notify admin when done (detected by content)
                if "완료" in msg or "초기화" not in msg: # Simple heuristic
                     await self._send_dm_to_admin(msg, title="멤버 동기화 결과", level="success")
                
            await admin_cog._sync_members_logic(guild, status_callback=status_log)

        elif cmd_type == 'notify_admin':
            payload = cmd.get('payload', {})
            msg = payload.get('message', 'No content')
            title = payload.get('title', 'Admin Notification')
            level = payload.get('level', 'info')
            
            await self._send_dm_to_admin(msg, title, level)

        elif cmd_type == 'admin_notification':
            # Legacy support for settings updates
            payload = cmd.get('payload', {})
            msg = payload.get('message', 'No content')
            await self._send_dm_to_admin(msg, title="관리자 설정 변경 알림", level="info")

        elif cmd_type == 'test_missed_reminder':
            # Manually trigger daily missed reminders (for testing)
            scheduler_cog = self.bot.get_cog('SchedulerCog')
            if scheduler_cog:
                logger.info("[IPC] Triggering _send_daily_missed_reminders manually...")
                await scheduler_cog._send_daily_missed_reminders()
                logger.info("[IPC] _send_daily_missed_reminders completed")
            else:
                logger.error("[IPC] SchedulerCog not found")
            
        elif cmd_type == 'test_notification':
            payload = cmd.get('payload', {})
            target_id = payload.get('target_id')
            msg_type = payload.get('msg_type', 'Test Message')
            logger.info(f"[IPC] => TEST_NOTIFICATION: Target={target_id}, Type={msg_type}")
            
            if target_id:
                try:
                    logger.info(f"[IPC] Attempting to fetch user: {target_id}")
                    user = await self.bot.fetch_user(int(target_id))
                    if user:
                        logger.info(f"[IPC] User found: {user.name} ({user.id}). Creating embed...")
                        embed = discord.Embed(title="🔔 테스트 알림", description=f"이것은 테스트 메시지입니다.\n유형: {msg_type}", color=discord.Color.orange())
                        await user.send(embed=embed)
                        logger.info(f"[IPC] DM sent successfully to {user.name}")
                    else:
                        logger.warning(f"[IPC] fetch_user returned None for {target_id}")
                except Exception as e:
                    logger.error(f"[IPC] Failed during user fetch/send for {target_id}: {e}")
            else:
                logger.warning("[IPC] No target_id in payload for test_notification")

        elif cmd_type == 'reassign_groups':
            cohort = cmd.get('payload', {}).get('cohort')
            if not cohort:
                raise Exception("Cohort (e.g. '6') is required for reassign_groups")
            
            admin_cog = self.bot.get_cog('AdminCog')
            if not admin_cog:
                raise Exception("AdminCog is not loaded")
            
            logger.info(f"[IPC] Triggering reassign_groups for cohort {cohort}...")
            # We don't have a ctx, so we simulate a basic message or just call the logic
            # The logic _execute_group_assignment is internal and doesn't need ctx
            
            async def run_sync(func, *args, **kwargs):
                 return await self.bot.loop.run_in_executor(None, lambda: func(*args, **kwargs))

            applications = await run_sync(notion_api.get_unprocessed_applications, f"{cohort}기", include_processed=True)
            if not applications:
                raise Exception(f"No applications found for cohort {cohort}기")
            
            group_candidates, _, _, _, _ = await admin_cog._build_candidates(applications, f"{cohort}기")
            assigned_count, failed = await admin_cog._execute_group_assignment(f"{cohort}기", group_candidates)
            
            summary = f"🔄 IPC 조 배정 완료: {assigned_count}명 배정, {len(failed)}명 실패"
            await self._send_dm_to_admin(summary, title="조 배정 결과 (IPC)", level="success" if not failed else "warning")
            logger.info(f"[IPC] {summary}")

        elif cmd_type == 'group_preview_sync_discord':
            payload = cmd.get('payload', {})
            cohort = payload.get('cohortLabel')
            tracks = payload.get('tracks') or []
            if not cohort:
                raise Exception("cohortLabel is required for group_preview_sync_discord")

            admin_cog = self.bot.get_cog('AdminCog')
            if not admin_cog:
                raise Exception("AdminCog is not loaded")

            logger.info(f"[IPC] Triggering group_preview_sync_discord for cohort {cohort}...")
            result = await admin_cog.sync_discord_group_preview(cohort, tracks)
            cmd['result'] = result
            logger.info(
                "[IPC] group_preview_sync_discord completed. tracks=%s roles_created=%s group_channels=%s",
                result.get('tracks_processed', 0),
                result.get('roles_created', 0),
                result.get('group_channels_created', 0),
            )

        elif cmd_type == 'get_discord_group_state':
            # 디스코드 길드의 현재 조 배정 상태 enumerate.
            # admin 의 '디스코드 상태 불러오기' 가 호출. REST API 의 GUILD_MEMBERS
            # intent 제약을 우회 — gateway 캐시로 모든 멤버 접근 가능.
            payload = cmd.get('payload', {})
            cohort_label = str(payload.get('cohortLabel') or '').strip()
            if not cohort_label:
                raise Exception("cohortLabel is required for get_discord_group_state")
            cohort_digits = ''.join(ch for ch in cohort_label if ch.isdigit())
            if not cohort_digits:
                raise Exception(f"cohort label digits not extractable: {cohort_label!r}")

            try:
                guild = resolve_active_guild(self.bot)
            except RuntimeError as e:
                raise Exception(f"guild resolution failed: {e}")
            if not guild:
                raise Exception("Bot is not connected to any guild.")

            # 트랙 prefix → 트랙명 역매핑. cogs/admin.py 의 _TRACK_DISCORD_PREFIX 와 sync.
            prefix_to_track = {
                '크리에이터': '크리에이터 트랙',
                '빌더-기초': '빌더 기초 트랙',
                '빌더-심화': '빌더 심화 트랙',
                '세일즈-실전': '세일즈 실전 트랙',
                'AI에이전트-실전': 'AI 에이전트 트랙',
                '앱개발': '앱 개발 트랙',
                '나탐구': '나 탐구 트랙',
            }
            sorted_prefixes = sorted(prefix_to_track.keys(), key=len, reverse=True)

            import re
            cohort_re_str = re.escape(cohort_digits)
            group_re = re.compile(rf'^(.+?)-{cohort_re_str}기-(\d+)조$')
            leader_re = re.compile(rf'^(.+?)-{cohort_re_str}기-조장$')

            role_id_to_group = {}             # role.id -> (track_name, group_num)
            role_id_to_leader_track = {}      # role.id -> track_name

            for role in guild.roles:
                m = group_re.match(role.name or '')
                if m:
                    prefix, num_str = m.group(1), m.group(2)
                    for p in sorted_prefixes:
                        if prefix == p:
                            role_id_to_group[role.id] = (prefix_to_track[p], int(num_str))
                            break
                    continue
                m = leader_re.match(role.name or '')
                if m:
                    prefix = m.group(1)
                    for p in sorted_prefixes:
                        if prefix == p:
                            role_id_to_leader_track[role.id] = prefix_to_track[p]
                            break

            # gateway 캐시된 멤버 enumerate.
            # 캐시가 비어있다면 guild.chunk() 로 강제 fetch.
            if len(guild.members) < (guild.member_count or 0):
                try:
                    logger.info(f"[IPC] guild.members cache short ({len(guild.members)}/{guild.member_count}) — chunking...")
                    await guild.chunk(cache=True)
                except Exception as e:
                    logger.warning(f"[IPC] guild.chunk failed (proceeding with partial cache): {e}")

            track_to_groups = {}
            scanned = 0
            for m in guild.members:
                if m.bot:
                    continue
                scanned += 1
                user_role_ids = {r.id for r in m.roles}
                leader_track_names = {role_id_to_leader_track[rid] for rid in user_role_ids if rid in role_id_to_leader_track}
                for rid in user_role_ids:
                    grp = role_id_to_group.get(rid)
                    if not grp:
                        continue
                    track_name, group_num = grp
                    member_info = {
                        'userId': str(m.id),
                        'name': (m.nick or m.global_name or m.name or '').strip(),
                        'handle': f'@{m.name}' if m.name else '',
                        'leader': track_name in leader_track_names,
                    }
                    track_to_groups.setdefault(track_name, {}).setdefault(group_num, []).append(member_info)

            out_tracks = []
            for track_name in sorted(track_to_groups.keys()):
                groups_map = track_to_groups[track_name]
                out_groups = []
                for group_num in sorted(groups_map.keys()):
                    out_groups.append({
                        'name': f'{cohort_label} {group_num}조',
                        'groupNumber': group_num,
                        'members': groups_map[group_num],
                    })
                out_tracks.append({
                    'trackName': track_name,
                    'groups': out_groups,
                })

            result = {
                'status': 'success',
                'cohortLabel': cohort_label,
                'guild_id': str(guild.id),
                'guild_name': guild.name,
                'roles_matched': len(role_id_to_group),
                'members_scanned': scanned,
                'tracks': out_tracks,
            }
            cmd['result'] = result
            logger.info(
                "[IPC] get_discord_group_state cohort=%s roles_matched=%d members_scanned=%d tracks_out=%d",
                cohort_label, len(role_id_to_group), scanned, len(out_tracks),
            )

    @check_queue.before_loop
    async def before_check_queue(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(IPCCog(bot))
