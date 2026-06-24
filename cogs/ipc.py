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
                "[IPC] group_preview_sync_discord completed. tracks=%s roles_created=%s roles_assigned=%s announcement=%s assignment=%s mentoring=%s networking=%s lounge=%s",
                result.get('tracks_processed', 0),
                result.get('roles_created', 0),
                result.get('roles_assigned', 0),
                result.get('announcement_channels_created', 0),
                result.get('assignment_channels_created', 0),
                result.get('mentoring_channels_created', 0),
                result.get('networking_channels_created', 0),
                result.get('voice_channels_created', 0),
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
                'AI에이전트': 'AI 에이전트 트랙',
                'AI에이전트-실전': 'AI 에이전트 트랙',   # 구 prefix 호환
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
            # 트랙 그룹 인벤토리 — 멤버 유무와 별개로 '어떤 트랙·조 역할이 길드에
            # 존재하는지' 기록. 멤버 0명인 그룹도 응답에 빈 그룹으로 포함시키기 위함.
            # (예: create-track-infra 로 만든 역할은 멤버 배정 X → 그래도 admin 화면에
            # 빈 그룹 2개로 보여줘야 함. 안 그러면 localStorage 의 stale 3조가 유지됨.)
            track_group_inventory = {}        # track_name -> set of group_num

            for role in guild.roles:
                m = group_re.match(role.name or '')
                if m:
                    prefix, num_str = m.group(1), m.group(2)
                    for p in sorted_prefixes:
                        if prefix == p:
                            track_name_resolved = prefix_to_track[p]
                            group_num_int = int(num_str)
                            role_id_to_group[role.id] = (track_name_resolved, group_num_int)
                            track_group_inventory.setdefault(track_name_resolved, set()).add(group_num_int)
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

            # 멤버 보유 트랙 + 역할만 있는 트랙 (인프라만 부트스트랩된 경우) 합집합.
            out_tracks = []
            all_track_names = set(track_to_groups.keys()) | set(track_group_inventory.keys())
            for track_name in sorted(all_track_names):
                groups_map = track_to_groups.get(track_name, {})
                inventory_nums = track_group_inventory.get(track_name, set())
                # 역할 인벤토리 + 멤버 수집된 그룹 번호의 합집합.
                all_nums = sorted(set(groups_map.keys()) | inventory_nums)
                out_groups = []
                for group_num in all_nums:
                    out_groups.append({
                        'name': f'{cohort_label} {group_num}조',
                        'groupNumber': group_num,
                        'members': groups_map.get(group_num, []),
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

        elif cmd_type == 'cleanup_track_groups_beyond':
            # 특정 트랙의 group_num > keep_count 인 역할 + 채널 삭제 (고스트 정리).
            #
            # 사용 시점: 이전 실패한 commit 이 의도하지 않은 그룹 (예: 나탐구 3조) 을
            # 디스코드에 만들어버린 경우 수동 정리. keep_count 이하 그룹은 안 건드림.
            #
            # dryRun=true → 실제 삭제 안 하고 매칭만 보고 (진단용).
            payload = cmd.get('payload', {})
            cohort_label = str(payload.get('cohortLabel') or '').strip()
            track_name = str(payload.get('trackName') or '').strip()
            dry_run = bool(payload.get('dryRun'))
            try:
                keep_count = int(payload.get('keepGroupCount') or 0)
            except (TypeError, ValueError):
                raise Exception("keepGroupCount must be int")
            if not cohort_label or not track_name:
                raise Exception("cohortLabel + trackName required")
            if keep_count < 0:
                raise Exception("keepGroupCount must be >= 0")
            cohort_digits = ''.join(ch for ch in cohort_label if ch.isdigit())
            if not cohort_digits:
                raise Exception(f"cohort label digits not extractable: {cohort_label!r}")

            try:
                guild = resolve_active_guild(self.bot)
            except RuntimeError as e:
                raise Exception(f"guild resolution failed: {e}")
            if not guild:
                raise Exception("Bot is not connected to any guild.")

            # 트랙명 → prefix 매핑 (admin.py 와 동일).
            prefix_map = {
                '크리에이터 트랙':        '크리에이터',
                '빌더 기초 트랙':         '빌더-기초',
                '빌더 심화 트랙':         '빌더-심화',
                '세일즈 실전 트랙':       '세일즈-실전',
                'AI 에이전트 트랙':       'AI에이전트',
                '앱 개발 트랙':           '앱개발',
                '나 탐구 트랙':           '나탐구',
            }
            prefix = prefix_map.get(track_name)
            if not prefix:
                raise Exception(f"unknown trackName: {track_name!r}")

            import re
            # 진단용: 디스코드의 실제 역할/채널 이름 dump.
            all_matching_roles = []   # prefix 가 들어간 모든 역할 (정규식 매칭 여부 무관)
            all_matching_channels = []

            # 엄격 패턴: '{prefix}-{cohort}기-{N}조'
            strict_re = re.compile(rf'^{re.escape(prefix)}-{re.escape(cohort_digits)}기-(\d+)조$')
            # 느슨 패턴: prefix 와 cohort 가 어떤 형태로든 포함 + 숫자조
            loose_re = re.compile(rf'{re.escape(cohort_digits)}\s*기.*?(\d+)\s*조', re.IGNORECASE)

            # 1) 역할 후보 수집 + 엄격 매칭 삭제 (group_num > keep_count).
            deleted_roles = []
            matched_loose_roles = []
            failures = []

            for role in list(guild.roles):
                rname = role.name or ''
                if prefix in rname or prefix.replace('-', '') in rname or prefix.replace('-', ' ') in rname:
                    all_matching_roles.append({'name': rname, 'id': str(role.id)})
                strict_m = strict_re.match(rname)
                if strict_m:
                    num = int(strict_m.group(1))
                    matched_loose_roles.append({'name': rname, 'num': num, 'pattern': 'strict'})
                    if num > keep_count:
                        if dry_run:
                            deleted_roles.append({'name': rname, 'id': str(role.id), 'dryRun': True})
                        else:
                            try:
                                await role.delete(reason=f"수동 정리: {track_name} keep={keep_count} 이하만 유지")
                                deleted_roles.append({'name': rname, 'id': str(role.id)})
                                logger.info(f"[IPC cleanup] deleted role: {rname}")
                            except Exception as e:
                                failures.append({'kind': 'role', 'name': rname, 'error': str(e)})
                    continue
                # 느슨 매칭도 시도해서 정보 노출.
                loose_m = loose_re.search(rname)
                if loose_m and prefix in rname:
                    matched_loose_roles.append({'name': rname, 'num': int(loose_m.group(1)), 'pattern': 'loose'})

            # 2) 같은 패턴의 채널도 삭제 (text + voice).
            deleted_channels = []
            matched_loose_channels = []
            for channel in list(guild.channels):
                cname = channel.name or ''
                if prefix in cname or prefix.replace('-', '') in cname:
                    all_matching_channels.append({'name': cname, 'id': str(channel.id), 'type': str(channel.type)})
                # voice 채널은 prefix-매칭 (suffix 허용)
                strict_m = re.match(
                    rf'^{re.escape(prefix)}-{re.escape(cohort_digits)}기-(\d+)조',
                    cname,
                )
                if strict_m:
                    num = int(strict_m.group(1))
                    matched_loose_channels.append({'name': cname, 'num': num, 'pattern': 'strict'})
                    if num > keep_count:
                        if dry_run:
                            deleted_channels.append({'name': cname, 'id': str(channel.id), 'dryRun': True})
                        else:
                            try:
                                await channel.delete(reason=f"수동 정리: {track_name} keep={keep_count} 이하만 유지")
                                deleted_channels.append({'name': cname, 'id': str(channel.id), 'type': str(channel.type)})
                                logger.info(f"[IPC cleanup] deleted channel: {cname}")
                            except Exception as e:
                                failures.append({'kind': 'channel', 'name': cname, 'error': str(e)})

            result = {
                'status': 'success',
                'cohortLabel': cohort_label,
                'trackName': track_name,
                'prefix': prefix,
                'keepGroupCount': keep_count,
                'dryRun': dry_run,
                'deleted_roles': deleted_roles,
                'deleted_channels': deleted_channels,
                'failures': failures,
                # 진단 dump — prefix 포함된 모든 역할/채널 + 엄격 매칭 결과.
                'diag_all_prefix_roles': all_matching_roles,
                'diag_all_prefix_channels': all_matching_channels,
                'diag_strict_matched_roles': matched_loose_roles,
                'diag_strict_matched_channels': matched_loose_channels,
            }
            cmd['result'] = result
            logger.info(
                "[IPC] cleanup_track_groups_beyond track=%s keep=%d dryRun=%s prefix_roles=%d strict_roles=%d deleted=%d failures=%d",
                track_name, keep_count, dry_run, len(all_matching_roles),
                len(matched_loose_roles), len(deleted_roles), len(failures),
            )

        elif cmd_type == 'migrate_track_role_prefix':
            # 옛 prefix 의 group 역할에 속한 멤버를 새 prefix 의 같은 group 번호 역할로
            # 추가 부여. 옛 역할 자체는 안 건드림 (사용자가 별도 정리).
            #
            # 예: '{oldPrefix}-9기-1조' 보유 멤버 → '{newPrefix}-9기-1조' 역할 추가.
            #     '{oldPrefix}-9기-조장' 보유 멤버 → '{newPrefix}-9기-조장' 역할 추가.
            #
            # 새 역할이 없으면 생성.
            payload = cmd.get('payload', {})
            cohort_label = str(payload.get('cohortLabel') or '').strip()
            old_prefix = str(payload.get('oldPrefix') or '').strip()
            new_prefix = str(payload.get('newPrefix') or '').strip()
            if not cohort_label or not old_prefix or not new_prefix:
                raise Exception("cohortLabel + oldPrefix + newPrefix required")
            cohort_digits = ''.join(ch for ch in cohort_label if ch.isdigit())
            if not cohort_digits:
                raise Exception(f"cohort label digits not extractable: {cohort_label!r}")
            if old_prefix == new_prefix:
                raise Exception("oldPrefix and newPrefix must differ")

            try:
                guild = resolve_active_guild(self.bot)
            except RuntimeError as e:
                raise Exception(f"guild resolution failed: {e}")
            if not guild:
                raise Exception("Bot is not connected to any guild.")

            import re
            old_group_re = re.compile(rf'^{re.escape(old_prefix)}-{re.escape(cohort_digits)}기-(\d+)조$')
            old_leader_re = re.compile(rf'^{re.escape(old_prefix)}-{re.escape(cohort_digits)}기-조장$')

            old_group_roles = {}   # group_num -> Role
            old_leader_role = None
            for role in guild.roles:
                m = old_group_re.match(role.name or '')
                if m:
                    old_group_roles[int(m.group(1))] = role
                    continue
                if old_leader_re.match(role.name or ''):
                    old_leader_role = role

            if not old_group_roles and not old_leader_role:
                cmd['result'] = {
                    'status': 'nothing_to_migrate',
                    'cohortLabel': cohort_label,
                    'oldPrefix': old_prefix,
                    'newPrefix': new_prefix,
                    'message': f"옛 prefix '{old_prefix}-{cohort_digits}기-N조' 역할을 찾지 못했습니다.",
                }
                logger.info(f"[IPC migrate] nothing to migrate: {old_prefix} -> {new_prefix}")
                return  # process_command 종료 (loop 의 continue 가 아님)

            reason = f"트랙 prefix 마이그레이션: {old_prefix} → {new_prefix}"

            # 새 group 역할 ensure.
            new_group_roles = {}
            roles_created = []
            for num in sorted(old_group_roles.keys()):
                new_name = f"{new_prefix}-{cohort_digits}기-{num}조"
                new_role = discord.utils.get(guild.roles, name=new_name)
                if not new_role:
                    new_role = await guild.create_role(name=new_name, reason=reason)
                    roles_created.append(new_name)
                new_group_roles[num] = new_role

            new_leader_role = None
            if old_leader_role:
                new_leader_name = f"{new_prefix}-{cohort_digits}기-조장"
                new_leader_role = discord.utils.get(guild.roles, name=new_leader_name)
                if not new_leader_role:
                    new_leader_role = await guild.create_role(name=new_leader_name, reason=reason)
                    roles_created.append(new_leader_name)

            # gateway 캐시 풀 fill (멤버 누락 방지).
            if len(guild.members) < (guild.member_count or 0):
                try:
                    await guild.chunk(cache=True)
                except Exception as e:
                    logger.warning(f"[IPC migrate] guild.chunk failed: {e}")

            # 멤버 순회 — 옛 역할 보유시 새 역할 추가.
            migrated_per_group = {}
            migrated_leader_count = 0
            members_touched = 0
            failures = []
            for member in guild.members:
                if member.bot:
                    continue
                member_role_ids = {r.id for r in member.roles}
                roles_to_add = []
                for num, old_role in old_group_roles.items():
                    if old_role.id in member_role_ids:
                        new_role = new_group_roles[num]
                        if new_role.id not in member_role_ids:
                            roles_to_add.append(new_role)
                            migrated_per_group[num] = migrated_per_group.get(num, 0) + 1
                if (old_leader_role and old_leader_role.id in member_role_ids
                        and new_leader_role and new_leader_role.id not in member_role_ids):
                    roles_to_add.append(new_leader_role)
                    migrated_leader_count += 1
                if roles_to_add:
                    try:
                        await member.add_roles(*roles_to_add, reason=reason)
                        members_touched += 1
                    except Exception as e:
                        failures.append({
                            'member_id': str(member.id),
                            'member_name': str(member.display_name or member.name),
                            'error': str(e),
                        })

            cmd['result'] = {
                'status': 'success',
                'cohortLabel': cohort_label,
                'oldPrefix': old_prefix,
                'newPrefix': new_prefix,
                'roles_created': roles_created,
                'migrated_per_group': migrated_per_group,
                'migrated_leader_count': migrated_leader_count,
                'members_touched': members_touched,
                'failures': failures,
            }
            logger.info(
                "[IPC migrate] %s -> %s members_touched=%d per_group=%s leader=%d roles_created=%d failures=%d",
                old_prefix, new_prefix, members_touched, migrated_per_group,
                migrated_leader_count, len(roles_created), len(failures),
            )

    @check_queue.before_loop
    async def before_check_queue(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    await bot.add_cog(IPCCog(bot))
