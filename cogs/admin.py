import discord
import asyncio
from discord.ext import commands
import logging
import json
import os
from datetime import datetime # Changed from `import datetime`
import random
import re
from typing import Optional, Dict, Any, List, Tuple

import notion_api
from config import CONFIG, CONFIG_FILE, load_bot_config, merge_discord_runtime_resources, update_bot_config
from env_utils import resolve_active_guild

# 트랙명 → Discord 역할 접두사 매핑 (dropout_handler.py와 동일)
_TRACK_DISCORD_PREFIX = {
    "크리에이터 트랙":                "크리에이터",
    "크리에이터 숏폼 트랙":          "크리에이터",
    "크리에이터 롱폼 트랙":          "크리에이터",
    "크리에이터 라이트 트랙 (숏폼)": "크리에이터",
    "크리에이터 라이트 트랙 (롱폼)": "크리에이터",
    "빌더 기초 트랙":                "빌더-기초",
    "빌더 라이트 트랙 (기초)":       "빌더-기초",
    "빌더 심화 트랙":                "빌더-심화",
    "빌더 라이트 트랙 (심화)":       "빌더-심화",
    "세일즈 실전 트랙":              "세일즈-실전",
    "AI 에이전트 트랙":              "AI에이전트",
    "앱 개발 트랙":                  "앱개발",
    "나 탐구 트랙":                  "나탐구",
}

_TRACK_ASSIGNMENT_CHANNEL_KEYS = {
    "\ube4c\ub354 \uae30\ucd08 \ud2b8\ub799": ["BUILDER_BASIC_ID"],
    "\ube4c\ub354 \ub77c\uc774\ud2b8 \ud2b8\ub799 (\uae30\ucd08)": ["BUILDER_BASIC_ID"],
    "\ube4c\ub354 \uc2ec\ud654 \ud2b8\ub799": ["BUILDER_ADVANCED_ID"],
    "\ube4c\ub354 \ub77c\uc774\ud2b8 \ud2b8\ub799 (\uc2ec\ud654)": ["BUILDER_ADVANCED_ID"],
    "\uc138\uc77c\uc988 \uc2e4\uc804 \ud2b8\ub799": ["SALES_PRACTICAL_ID"],
    "AI \uc5d0\uc774\uc804\ud2b8 \ud2b8\ub799": ["AI_AGENT_ID"],
    "\uc571 \uac1c\ubc1c \ud2b8\ub799": ["APP_DEV_ID"],
    "\ub098 \ud0d0\uad6c \ud2b8\ub799": ["SELF_INQUIRY_ID"],
}

_LIGHT_TRACK_TAB_IDS = {"creator_light", "builder_light"}
_LIGHT_TRACK_NAMES = {"크리에이터 라이트 트랙", "빌더 라이트 트랙"}
_LIGHT_TRACK_PARENT_TRACKS = {
    "크리에이터 라이트 트랙": "크리에이터 트랙",
    "크리에이터 라이트 트랙 (숏폼)": "크리에이터 트랙",
    "크리에이터 라이트 트랙 (롱폼)": "크리에이터 트랙",
    "빌더 라이트 트랙 (기초)": "빌더 기초 트랙",
    "빌더 라이트 트랙 (심화)": "빌더 심화 트랙",
}
_LIGHT_TRACK_ROLE_PREFIX = {
    "크리에이터 라이트 트랙": "크리에이터",
    "크리에이터 라이트 트랙 (숏폼)": "크리에이터",
    "크리에이터 라이트 트랙 (롱폼)": "크리에이터",
    "빌더 라이트 트랙 (기초)": "빌더-기초",
    "빌더 라이트 트랙 (심화)": "빌더-심화",
}

# 크리에이터 라이트는 sub-form (숏폼/롱폼) 별로 독립 역할 + 채널 분리.
# 빌더 라이트는 (기초/심화) 가 이미 별도 부모 트랙으로 분리되어 있어 sub_form 불필요.
_LIGHT_TRACK_SUB_FORM = {
    "크리에이터 라이트 트랙 (숏폼)": "숏폼",
    "크리에이터 라이트 트랙 (롱폼)": "롱폼",
}

def _track_short_name(track_name: str) -> str:
    return _TRACK_DISCORD_PREFIX.get(track_name, track_name.replace(" 트랙", "").replace(" ", "-"))


def _creator_member_subform(member_data: Dict[str, Any]) -> str:
    """
    정규 크리에이터 멤버가 '숏폼만' 인지 '숏폼+롱폼' 인지 판정한다.
    - rowTrackName 에 '롱폼' 포함 (예: '크리에이터 롱폼 트랙') → '롱폼'
    - creatorSub == 'short_long' / '숏폼 + 롱폼' → '롱폼'
    - 그 외(정보 없음 포함) → '숏폼' (보수적: 롱폼 과제 채널 접근을 부여하지 않음)
    """
    row = str(member_data.get("rowTrackName") or "")
    sub = str(member_data.get("creatorSub") or "").strip().lower()
    if "롱폼" in row or sub in ("short_long", "숏폼 + 롱폼", "숏폼+롱폼", "롱폼"):
        return "롱폼"
    return "숏폼"


def _track_category_name(track_name: str) -> str:
    """
    트랙 카테고리명. 운영 디스코드 서버의 기존 카테고리 형식 (`=====세일즈 실전 트랙=====`)
    을 그대로 사용해, 같은 카테고리 안에 모든 기수의 채널이 누적되도록 한다.

    예) "세일즈 실전 트랙" → "=====세일즈 실전 트랙====="
    track_name 이 비어있을 경우 빈 문자열 반환.
    """
    name = str(track_name or "").strip()
    if not name:
        return ""
    return f"====={name}====="

def _cohort_display_label(cohort: str) -> str:
    cohort = str(cohort or "").strip()
    if not cohort:
        return ""
    return cohort if cohort.endswith("기") else f"{cohort}기"

def _cohort_name_fragment(cohort: str) -> str:
    cohort_display = _cohort_display_label(cohort)
    return cohort_display[:-1] if cohort_display.endswith("기") else cohort_display

def _runtime_track_bucket(runtime_updates: Dict[str, Any], track_name: str, track_short: str) -> Dict[str, Any]:
    track_bucket = runtime_updates.setdefault(track_name, {
        "trackKey": track_short,
        "roles": {},
        "channels": {},
    })
    track_bucket["trackKey"] = track_short
    return track_bucket

def _persist_role_resource(
    runtime_updates: Dict[str, Any],
    track_name: str,
    track_short: str,
    role_kind: str,
    role_obj,
    *,
    group_number: Optional[int] = None,
) -> None:
    if not role_obj:
        return

    track_bucket = _runtime_track_bucket(runtime_updates, track_name, track_short)
    roles_bucket = track_bucket.setdefault("roles", {})

    payload = {
        "id": str(role_obj.id),
        "name": role_obj.name,
    }
    if group_number is not None:
        payload["groupNumber"] = str(group_number)

    if role_kind == "track":
        roles_bucket["track"] = payload
    elif role_kind == "short":
        roles_bucket["short"] = payload
    elif role_kind == "long":
        roles_bucket["long"] = payload
    elif role_kind == "leader":
        roles_bucket["leader"] = payload
    elif role_kind == "light":
        roles_bucket["light"] = payload
    else:
        roles_bucket.setdefault("groups", {})[str(group_number)] = payload

def _persist_channel_resource(
    runtime_updates: Dict[str, Any],
    track_name: str,
    track_short: str,
    resource_kind: str,
    channel_obj,
    *,
    group_number: Optional[int] = None,
) -> None:
    if not channel_obj:
        return

    track_bucket = _runtime_track_bucket(runtime_updates, track_name, track_short)
    channels_bucket = track_bucket.setdefault("channels", {})
    payload = {
        "id": str(channel_obj.id),
        "name": channel_obj.name,
    }

    category_id = getattr(channel_obj, "category_id", None)
    if category_id:
        payload["categoryId"] = str(category_id)

    if resource_kind == "category":
        channels_bucket["category"] = payload
    elif resource_kind == "group":
        payload["groupNumber"] = str(group_number)
        channels_bucket.setdefault("groups", {})[str(group_number)] = payload
    else:
        channels_bucket[resource_kind] = payload

def _config_channel_keys_for_track(track_name: str) -> List[str]:
    return _TRACK_ASSIGNMENT_CHANNEL_KEYS.get(track_name, [])


def _is_light_track_payload(track_name: str, tab_id: Optional[str] = None) -> bool:
    if str(tab_id or "").strip() in _LIGHT_TRACK_TAB_IDS:
        return True
    return str(track_name or "").strip() in _LIGHT_TRACK_NAMES


def _empty_discord_sync_summary() -> Dict[str, int]:
    return {
        "tracks_processed": 0,
        "roles_created": 0,
        "roles_assigned": 0,
        "role_failures": 0,
        "member_lookup_failures": 0,
        "mock_members_skipped": 0,
        "categories_created": 0,
        "announcement_channels_created": 0,
        "assignment_channels_created": 0,
        "mentoring_channels_created": 0,
        "networking_channels_created": 0,
        "group_channels_created": 0,
        "leader_channels_created": 0,
        "voice_channels_created": 0,
    }


def _merge_discord_sync_summary(base: Dict[str, int], extra: Dict[str, int]) -> Dict[str, int]:
    merged = dict(base or {})
    for key, value in (extra or {}).items():
        if isinstance(value, (int, float)):
            merged[key] = int(merged.get(key, 0)) + int(value)
    return merged


def _runtime_track_resources_for_cohort(cohort_label: str, track_name: str) -> Dict[str, Any]:
    config = load_bot_config() or {}
    resources = config.get("discord_runtime_resources", {}) or {}
    cohort_bucket = resources.get(cohort_label, {}) or {}
    tracks_bucket = cohort_bucket.get("tracks", {}) or {}
    return tracks_bucket.get(track_name, {}) or {}


def _role_overwrite_for_access(*, read_only: bool) -> discord.PermissionOverwrite:
    if read_only:
        return discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            add_reactions=False,
        )
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        embed_links=True,
        add_reactions=True,
    )

# Setup Logger
logger = logging.getLogger('cogs.admin')

class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        """
        Global check for this Cog: execute only if user is admin.
        (Redundant if individual commands have checks, but good for safety)
        """
        # Allow Bot Owner to bypass checks
        if await self.bot.is_owner(ctx.author):
            return True

        if not ctx.guild:
            return False
        return ctx.author.guild_permissions.administrator

    @commands.Cog.listener()
    async def on_ready(self):
        """
        Notify Admin on Server Restart
        """
        ADMIN_ID = 1491970119910424666
        try:
            admin = await self.bot.fetch_user(ADMIN_ID)
            if admin:
                embed = discord.Embed(
                    title="🚀 Server Restarted",
                    description=f"Bot has been restarted and is now online.\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    color=discord.Color.green()
                )
                await admin.send(embed=embed)
                logger.info(f"Sent restart notification to Admin ({ADMIN_ID})")
        except Exception as e:
            logger.warning(f"Failed to send restart notification: {e}")

    async def _ensure_role(self, guild: discord.Guild, role_name: str, reason: str) -> Tuple[Optional[discord.Role], bool]:
        role = discord.utils.get(guild.roles, name=role_name)
        if role:
            return role, False

        try:
            role = await guild.create_role(name=role_name, reason=reason)
            logger.info(f"[Role] Created: {role_name}")
            return role, True
        except Exception as e:
            logger.warning(f"[Role] Failed to create {role_name}: {e}")
            return None, False

    async def _ensure_category_channel(
        self,
        guild: discord.Guild,
        category_name: str,
        reason: str,
    ) -> Tuple[Optional[discord.CategoryChannel], bool]:
        # 기존 카테고리가 있으면 그대로 재사용 — 권한(overwrites) 은 손대지 않는다.
        # (운영 서버의 기존 `=====...=====` 카테고리 권한을 임의로 덮어쓰지 않기 위함)
        category = discord.utils.get(guild.categories, name=category_name)
        if category:
            return category, False

        # 새로 만들 때만 default_role 비공개 + 봇 본인은 채널 관리 가능 으로 설정
        bot_member = guild.me
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                manage_channels=True,
                manage_roles=True,
            )

        try:
            category = await guild.create_category(category_name, overwrites=overwrites, reason=reason)
            logger.info(f"[Channel] Created category: {category_name}")
            return category, True
        except Exception as e:
            logger.warning(f"[Channel] Failed to create category {category_name}: {e}")
            return None, False

    def _build_channel_overwrites(
        self,
        guild: discord.Guild,
        allowed_roles: List[discord.Role],
        *,
        read_only: bool = False,
    ) -> Dict[Any, discord.PermissionOverwrite]:
        overwrites: Dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        for role in allowed_roles:
            if not role:
                continue
            if read_only:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=False,
                    add_reactions=False,
                )
            else:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                    add_reactions=True,
                )
        return overwrites

    def _build_voice_channel_overwrites(
        self,
        guild: discord.Guild,
        allowed_roles: List[discord.Role],
    ) -> Dict[Any, discord.PermissionOverwrite]:
        """
        화상미팅(음성 채널) 전용 overwrite.
        텍스트 채널과 달리 connect/speak/stream(=비디오, Go Live) 을 명시 부여해야
        그룹 역할 보유자가 실제로 입장·발언·화상 공유 가능.
        @everyone 은 view + connect 둘 다 deny — 운영 서버 '비공개 채널' 토글과 동일.
        """
        overwrites: Dict[Any, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                connect=False,
            ),
        }
        for role in allowed_roles:
            if not role:
                continue
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                connect=True,
                speak=True,
                stream=True,
                use_voice_activation=True,
                read_message_history=True,
            )
        return overwrites

    async def _ensure_text_channel(
        self,
        guild: discord.Guild,
        category: Optional[discord.CategoryChannel],
        channel_name: str,
        reason: str,
        *,
        overwrites: Dict[Any, discord.PermissionOverwrite],
        topic: Optional[str] = None,
    ) -> Tuple[Optional[discord.TextChannel], bool]:
        # Discord 는 텍스트 채널명을 소문자로 정규화해 저장한다 (예: 'AI에이전트' → 'ai에이전트').
        # 대소문자를 무시하고 매칭해야 이미 있는 채널을 못 찾아 중복 생성하는 일을 막는다.
        _target = (channel_name or "").strip().lower()
        channel = next(
            (c for c in guild.text_channels if (c.name or "").strip().lower() == _target),
            None,
        )
        if channel:
            try:
                await channel.edit(category=category, overwrites=overwrites, topic=topic, reason=reason)
            except Exception as e:
                logger.warning(f"[Channel] Failed to update {channel_name}: {e}")
            return channel, False

        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=topic,
                reason=reason,
            )
            logger.info(f"[Channel] Created text channel: {channel_name}")
            return channel, True
        except Exception as e:
            logger.warning(f"[Channel] Failed to create {channel_name}: {e}")
            return None, False

    async def _ensure_voice_channel(
        self,
        guild: discord.Guild,
        category: Optional[discord.CategoryChannel],
        channel_name: str,
        reason: str,
        *,
        overwrites: Dict[Any, discord.PermissionOverwrite],
    ) -> Tuple[Optional[discord.VoiceChannel], bool]:
        _target = (channel_name or "").strip().lower()
        channel = next(
            (c for c in guild.voice_channels if (c.name or "").strip().lower() == _target),
            None,
        )
        if channel:
            try:
                await channel.edit(category=category, overwrites=overwrites, reason=reason)
            except Exception as e:
                logger.warning(f"[Channel] Failed to update voice {channel_name}: {e}")
            return channel, False

        try:
            channel = await guild.create_voice_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                reason=reason,
            )
            logger.info(f"[Channel] Created voice channel: {channel_name}")
            return channel, True
        except Exception as e:
            logger.warning(f"[Channel] Failed to create voice {channel_name}: {e}")
            return None, False

    def _resolve_runtime_category(
        self,
        guild: discord.Guild,
        runtime_track: Dict[str, Any],
    ) -> Optional[discord.CategoryChannel]:
        category_payload = ((runtime_track.get("channels") or {}).get("category") or {})
        category_id = str(category_payload.get("id") or "").strip()
        if category_id.isdigit():
            channel = guild.get_channel(int(category_id))
            if isinstance(channel, discord.CategoryChannel):
                return channel

        category_name = str(category_payload.get("name") or "").strip()
        if category_name:
            found = discord.utils.get(guild.categories, name=category_name)
            if isinstance(found, discord.CategoryChannel):
                return found
        return None

    def _resolve_runtime_text_channel(
        self,
        guild: discord.Guild,
        runtime_track: Dict[str, Any],
        resource_kind: str,
    ) -> Optional[discord.TextChannel]:
        channel_payload = ((runtime_track.get("channels") or {}).get(resource_kind) or {})
        channel_id = str(channel_payload.get("id") or "").strip()
        if channel_id.isdigit():
            channel = guild.get_channel(int(channel_id))
            if isinstance(channel, discord.TextChannel):
                return channel

        channel_name = str(channel_payload.get("name") or "").strip()
        if channel_name:
            found = discord.utils.get(guild.text_channels, name=channel_name)
            if isinstance(found, discord.TextChannel):
                return found
        return None

    def _resolve_runtime_track_roles(
        self,
        guild: discord.Guild,
        runtime_track: Dict[str, Any],
    ) -> List[discord.Role]:
        roles_payload = runtime_track.get("roles", {}) or {}
        resolved: List[discord.Role] = []

        def _append_role(role_payload: Dict[str, Any]) -> None:
            role_id = str((role_payload or {}).get("id") or "").strip()
            if not role_id.isdigit():
                return
            role = guild.get_role(int(role_id))
            if role and role not in resolved:
                resolved.append(role)

        _append_role(roles_payload.get("track") or {})
        _append_role(roles_payload.get("short") or {})
        _append_role(roles_payload.get("long") or {})
        _append_role(roles_payload.get("leader") or {})
        _append_role(roles_payload.get("light") or {})
        for group_payload in (roles_payload.get("groups") or {}).values():
            _append_role(group_payload or {})

        return resolved

    async def _grant_role_access_to_channel(
        self,
        channel: Optional[discord.TextChannel],
        role: Optional[discord.Role],
        *,
        read_only: bool,
        reason: str,
    ) -> None:
        if not channel or not role:
            return

        overwrites = dict(channel.overwrites)
        overwrites[role] = _role_overwrite_for_access(read_only=read_only)
        await channel.edit(overwrites=overwrites, reason=reason)

    async def _grant_role_voice_access_to_channel(
        self,
        channel: Optional[discord.VoiceChannel],
        role: Optional[discord.Role],
        *,
        reason: str,
    ) -> None:
        # 음성 채널에 역할 접근을 additive 로 부여 (기존 다른 역할 overwrite 보존).
        if not channel or not role:
            return
        overwrites = dict(channel.overwrites)
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_voice_activation=True,
            read_message_history=True,
        )
        await channel.edit(overwrites=overwrites, reason=reason)

    async def _ensure_light_track_channel_access(
        self,
        guild: discord.Guild,
        category: Optional[discord.CategoryChannel],
        parent_track_name: str,
        parent_track_short: str,
        clean_cohort: str,
        cohort_display: str,
        light_role: discord.Role,
        sub_form: Optional[str],
        parent_roles: List[discord.Role],
        reason: str,
        summary: Dict[str, Any],
        runtime_updates: Dict[str, Any],
        assignment_channel_updates: Dict[str, str],
    ) -> None:
        """
        라이트 트랙 멤버에게 부모 트랙의 전체 채널 세트 접근을 부여한다.
        - 라이트도 정식과 동일하게 공지/과제-멘토링/네트워킹/라운지 전부 접근.
        - 과제-인증은 sub_form 규칙: 숏폼 라이트=숏폼만, 롱폼 라이트=숏폼+롱폼, sub_form 없으면 단일.
        - 채널이 없으면 생성(라이트만 있어도 전체 세트 보장), 있으면 overwrite 를 덮어쓰지 않고
          additive 로 라이트 역할만 추가 (다른 라이트/정식 역할 grant 보존).
        """
        base = f"{parent_track_short}-{clean_cohort}기"
        _norm = lambda s: (s or "").strip().lower()

        def _find(channels, name):
            t = _norm(name)
            return next((c for c in channels if _norm(c.name) == t), None)

        # (kind, name, mode) — mode: 'read'(공지) / 'write'(텍스트) / 'voice'
        specs: List[Tuple[str, str, str]] = [("announcement", f"{base}-공지", "read")]
        if parent_track_short == "크리에이터":
            specs.append(("assignment", f"{base}-숏폼-과제-인증", "write"))
            if sub_form == "롱폼":
                specs.append(("assignment", f"{base}-롱폼-과제-인증", "write"))
        else:
            specs.append(("assignment", f"{base}-과제-인증", "write"))
        specs += [
            ("mentoring", f"{base}-과제-멘토링", "write"),
            ("networking", f"{base}-네트워킹", "write"),
            ("lounge", f"{base}-라운지", "voice"),
        ]
        _created_key = {
            "announcement": "announcement_channels_created",
            "assignment": "assignment_channels_created",
            "mentoring": "mentoring_channels_created",
            "networking": "networking_channels_created",
        }

        for kind, name, mode in specs:
            if mode == "voice":
                ch = _find(guild.voice_channels, name)
                if ch is None:
                    ch, created = await self._ensure_voice_channel(
                        guild, category, name, reason,
                        overwrites=self._build_voice_channel_overwrites(guild, parent_roles + [light_role]),
                    )
                    if created:
                        summary["voice_channels_created"] += 1
                    _persist_channel_resource(runtime_updates, parent_track_name, parent_track_short, "lounge", ch)
                else:
                    await self._grant_role_voice_access_to_channel(ch, light_role, reason=reason)
            else:
                read_only = mode == "read"
                ch = _find(guild.text_channels, name)
                if ch is None:
                    ch, created = await self._ensure_text_channel(
                        guild, category, name, reason,
                        overwrites=self._build_channel_overwrites(guild, parent_roles + [light_role], read_only=read_only),
                        topic=f"{cohort_display} {parent_track_name} 채널",
                    )
                    if created:
                        summary[_created_key[kind]] += 1
                    _persist_channel_resource(runtime_updates, parent_track_name, parent_track_short, kind, ch)
                else:
                    await self._grant_role_access_to_channel(ch, light_role, read_only=read_only, reason=reason)
                if kind == "assignment" and ch:
                    for config_key in _config_channel_keys_for_track(parent_track_name):
                        assignment_channel_updates.setdefault(config_key, str(ch.id))

    def _build_light_track_targets(
        self,
        track_name: str,
        members: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized_track_name = str(track_name or "").strip()
        buckets: Dict[str, Dict[str, Any]] = {}

        def _ensure_bucket(effective_track_name: str) -> Optional[Dict[str, Any]]:
            parent_track_name = _LIGHT_TRACK_PARENT_TRACKS.get(effective_track_name)
            role_prefix = _LIGHT_TRACK_ROLE_PREFIX.get(effective_track_name)
            if not parent_track_name or not role_prefix:
                return None
            bucket = buckets.get(effective_track_name)
            if bucket:
                return bucket
            bucket = {
                "trackName": effective_track_name,
                "parentTrackName": parent_track_name,
                "rolePrefix": role_prefix,
                "members": [],
            }
            buckets[effective_track_name] = bucket
            return bucket

        for member_data in members:
            member_track_name = str(member_data.get("rowTrackName") or "").strip()
            effective_track_name = normalized_track_name
            if normalized_track_name == "빌더 라이트 트랙":
                effective_track_name = member_track_name
            elif normalized_track_name == "크리에이터 라이트 트랙":
                # rowTrackName 이 (숏폼)/(롱폼) 으로 표기돼 있으면 sub-form 별로 bucket 분리.
                # 없으면 부모 라이트 트랙으로 fallback (legacy payload 호환).
                if member_track_name in _LIGHT_TRACK_SUB_FORM:
                    effective_track_name = member_track_name
                else:
                    effective_track_name = "크리에이터 라이트 트랙"

            bucket = _ensure_bucket(effective_track_name)
            if not bucket:
                logger.warning(
                    "[Light Sync] Unsupported light track member payload: track=%s rowTrackName=%s member=%s",
                    normalized_track_name,
                    member_track_name,
                    member_data.get("name", "?"),
                )
                continue
            bucket["members"].append(member_data)

        return [bucket for bucket in buckets.values() if bucket.get("members")]

    async def _resolve_discord_member(self, guild: discord.Guild, identifier: str) -> Optional[discord.Member]:
        raw = str(identifier or "").strip()
        if not raw:
            return None

        if raw.isdigit():
            try:
                return await guild.fetch_member(int(raw))
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"[Role] Failed fetch_member({raw}): {e}")

        query = raw.lstrip("@").strip()
        if not query:
            return None

        try:
            results = await guild.query_members(query=query, limit=10)
        except Exception as e:
            logger.warning(f"[Role] Failed query_members({query}): {e}")
            results = []

        lowered_targets = {raw.lower(), query.lower(), f"@{query.lower()}"}
        for member in results:
            possible_names = {
                str(getattr(member, "name", "")).lower(),
                str(getattr(member, "display_name", "")).lower(),
                str(getattr(member, "global_name", "") or "").lower(),
                f"@{str(getattr(member, 'name', '')).lower()}",
                f"@{str(getattr(member, 'display_name', '')).lower()}",
            }
            if lowered_targets & possible_names:
                return member

        for member in guild.members:
            possible_names = {
                str(getattr(member, "name", "")).lower(),
                str(getattr(member, "display_name", "")).lower(),
                str(getattr(member, "global_name", "") or "").lower(),
            }
            if query.lower() in possible_names or raw.lower() in possible_names:
                return member

        return None

    def _should_skip_mock_member_sync(self, member_data: Dict[str, Any]) -> bool:
        """
        Track-application mock members use anon handles / non-numeric ids.
        They should stay in the preview and Notion output, but Discord role assignment
        should only target real guild members.
        """
        discord_id = str(
            member_data.get("discordId")
            or member_data.get("discord_id")
            or member_data.get("userId")
            or member_data.get("user_id")
            or ""
        ).strip()
        handle = str(member_data.get("handle") or "").strip().lower()
        name = str(member_data.get("name") or "").strip().lower()

        if discord_id.isdigit():
            return False

        if handle.startswith("@anon_"):
            return True

        if name.startswith("anon_"):
            return True

        return not discord_id and not handle and not name

    def _extract_group_number(self, group_value: Any, fallback: int) -> int:
        if isinstance(group_value, int):
            return group_value

        raw = str(group_value or "").strip()
        match = re.search(r"(\d+)\s*조", raw)
        if match:
            return int(match.group(1))

        digit_matches = re.findall(r"(\d+)", raw)
        if digit_matches:
            return int(digit_matches[-1])
        return fallback

    async def _sync_discord_resources_for_groups(
        self,
        guild: discord.Guild,
        cohort: str,
        track_groups: List[Dict[str, Any]],
        *,
        reason_label: str,
        include_channels: bool = True,
    ) -> Dict[str, Any]:
        cohort_display = _cohort_display_label(cohort)
        clean_cohort = _cohort_name_fragment(cohort_display)
        runtime_updates: Dict[str, Any] = {}
        assignment_channel_updates: Dict[str, str] = {}
        summary = {
            "tracks_processed": 0,
            "roles_created": 0,
            "roles_assigned": 0,
            "role_failures": 0,
            "member_lookup_failures": 0,
            "mock_members_skipped": 0,
            "categories_created": 0,
            "announcement_channels_created": 0,
            "assignment_channels_created": 0,
            "mentoring_channels_created": 0,
            "networking_channels_created": 0,
            "group_channels_created": 0,
            "leader_channels_created": 0,
            "voice_channels_created": 0,
        }

        for track_data in track_groups:
            track_name = str(track_data.get("trackName") or "").strip()
            groups = track_data.get("groups") or []
            if not track_name or not groups:
                continue

            summary["tracks_processed"] += 1
            track_short = _track_short_name(track_name)
            reason = f"{reason_label} ({cohort_display})"

            # 역할 — 일반 트랙은 단일 역할(예: '앱개발-10기').
            # 크리에이터는 숏폼/롱폼 2개로 분리: 숏폼만→숏폼 역할, 숏폼+롱폼→롱폼 역할.
            # (조장/조별 역할은 만들지 않음)
            creator_roles = None
            if track_short == "크리에이터":
                short_role, created = await self._ensure_role(guild, f"{track_short}-{clean_cohort}기-숏폼", reason)
                if created:
                    summary["roles_created"] += 1
                long_role, created = await self._ensure_role(guild, f"{track_short}-{clean_cohort}기-롱폼", reason)
                if created:
                    summary["roles_created"] += 1
                _persist_role_resource(runtime_updates, track_name, track_short, "short", short_role)
                _persist_role_resource(runtime_updates, track_name, track_short, "long", long_role)
                creator_roles = (short_role, long_role)
                track_role = long_role or short_role  # 채널 가드/대표용
            else:
                track_role_name = f"{track_short}-{clean_cohort}기"
                track_role, created = await self._ensure_role(guild, track_role_name, reason)
                if created:
                    summary["roles_created"] += 1
                _persist_role_resource(runtime_updates, track_name, track_short, "track", track_role)

            for group_data in groups:
                for member_data in group_data.get("members", []):
                    if self._should_skip_mock_member_sync(member_data):
                        summary["mock_members_skipped"] += 1
                        logger.info(
                            "[Role] Skipping mock preview member: %s (%s)",
                            member_data.get("name", "?"),
                            member_data.get("handle") or member_data.get("discordId") or "?",
                        )
                        continue

                    member_identifier = (
                        member_data.get("discordId")
                        or member_data.get("discord_id")
                        or member_data.get("userId")
                        or member_data.get("user_id")
                        or member_data.get("handle")
                        or member_data.get("name")
                    )
                    discord_member = await self._resolve_discord_member(guild, str(member_identifier or ""))
                    if not discord_member:
                        logger.warning(f"[Role] Member not found: {member_data.get('name', '?')} ({member_identifier})")
                        summary["role_failures"] += 1
                        summary["member_lookup_failures"] += 1
                        continue

                    # 크리에이터: 멤버의 숏폼/롱폼 선택에 따라 역할 부여.
                    if creator_roles:
                        sub = _creator_member_subform(member_data)
                        role_to_add = creator_roles[1] if (sub == "롱폼" and creator_roles[1]) else creator_roles[0]
                    else:
                        role_to_add = track_role
                    if not role_to_add:
                        continue

                    try:
                        await discord_member.add_roles(role_to_add, reason=reason)
                        summary["roles_assigned"] += 1
                    except Exception as e:
                        logger.warning(f"[Role] Failed for {member_data.get('name', '?')}: {e}")
                        summary["role_failures"] += 1

            if not include_channels:
                continue

            # 카테고리명은 운영 서버 기존 형식 (`=====세일즈 실전 트랙=====`) 을 그대로 사용한다.
            # 같은 트랙의 모든 기수 채널이 동일 카테고리 안에 누적되도록 보장.
            category_name = _track_category_name(track_name)
            category, created = await self._ensure_category_channel(guild, category_name, reason)
            if created:
                summary["categories_created"] += 1
            _persist_channel_resource(runtime_updates, track_name, track_short, "category", category)

            if not category or not track_role:
                continue

            # 채널 접근 역할 — 일반 트랙은 단일 역할.
            # 크리에이터는 숏폼+롱폼 역할 모두 공용 채널 접근, 단 롱폼-과제-인증은 롱폼 역할만.
            # (공지는 읽기 전용, 나머지 텍스트는 읽기/쓰기, 라운지는 음성)
            if creator_roles:
                allowed_roles = [r for r in creator_roles if r]
                creator_long_roles = [r for r in (creator_roles[1],) if r]
            else:
                allowed_roles = [track_role]
                creator_long_roles = []

            announcement_name = f"{track_short}-{clean_cohort}기-공지"
            announcement_channel, created = await self._ensure_text_channel(
                guild,
                category,
                announcement_name,
                reason,
                overwrites=self._build_channel_overwrites(guild, allowed_roles, read_only=True),
                topic=f"{cohort_display} {track_name} 공지 채널",
            )
            if created:
                summary["announcement_channels_created"] += 1
            _persist_channel_resource(runtime_updates, track_name, track_short, "announcement", announcement_channel)

            # 과제-인증 — 운영 서버 형식: '{prefix}-{cohort}기-과제-인증'.
            # 크리에이터 트랙만 숏폼/롱폼 두 채널로 분리.
            #   - 숏폼-과제-인증: 숏폼+롱폼 역할 모두 (롱폼도 숏폼 과제 제출)
            #   - 롱폼-과제-인증: 롱폼 역할만 (숏폼만 신청자는 초대 X)
            if track_short == "크리에이터":
                assignment_specs = [
                    (f"{track_short}-{clean_cohort}기-숏폼-과제-인증", "숏폼 과제 인증", allowed_roles),
                    (f"{track_short}-{clean_cohort}기-롱폼-과제-인증", "롱폼 과제 인증", creator_long_roles),
                ]
            else:
                assignment_specs = [
                    (f"{track_short}-{clean_cohort}기-과제-인증", "과제 인증", allowed_roles),
                ]

            assignment_channel = None
            for assign_name, assign_label, assign_roles in assignment_specs:
                a_channel, created = await self._ensure_text_channel(
                    guild,
                    category,
                    assign_name,
                    reason,
                    overwrites=self._build_channel_overwrites(guild, assign_roles, read_only=False),
                    topic=f"{cohort_display} {track_name} {assign_label} 채널",
                )
                if created:
                    summary["assignment_channels_created"] += 1
                # 첫 번째(또는 단일) 채널을 대표 assignment 채널로 등록 (config 매핑 호환)
                if assignment_channel is None and a_channel:
                    assignment_channel = a_channel
                    _persist_channel_resource(runtime_updates, track_name, track_short, "assignment", a_channel)

            if assignment_channel:
                for config_key in _config_channel_keys_for_track(track_name):
                    assignment_channel_updates[config_key] = str(assignment_channel.id)

            # 과제-멘토링
            mentoring_name = f"{track_short}-{clean_cohort}기-과제-멘토링"
            mentoring_channel, created = await self._ensure_text_channel(
                guild,
                category,
                mentoring_name,
                reason,
                overwrites=self._build_channel_overwrites(guild, allowed_roles, read_only=False),
                topic=f"{cohort_display} {track_name} 과제 멘토링 채널",
            )
            if created:
                summary["mentoring_channels_created"] += 1
            _persist_channel_resource(runtime_updates, track_name, track_short, "mentoring", mentoring_channel)

            # 네트워킹
            networking_name = f"{track_short}-{clean_cohort}기-네트워킹"
            networking_channel, created = await self._ensure_text_channel(
                guild,
                category,
                networking_name,
                reason,
                overwrites=self._build_channel_overwrites(guild, allowed_roles, read_only=False),
                topic=f"{cohort_display} {track_name} 네트워킹 채널",
            )
            if created:
                summary["networking_channels_created"] += 1
            _persist_channel_resource(runtime_updates, track_name, track_short, "networking", networking_channel)

            # 라운지 (음성 채널) — 트랙 역할 보유자만 입장.
            # 운영 서버 '비공개 채널' 토글과 동일하게 view + connect 모두 부여,
            # 비디오/Go Live(stream) 권한도 명시 부여해야 화상 모임이 정상 동작.
            lounge_name = f"{track_short}-{clean_cohort}기-라운지"
            lounge_channel, created = await self._ensure_voice_channel(
                guild,
                category,
                lounge_name,
                reason,
                overwrites=self._build_voice_channel_overwrites(guild, allowed_roles),
            )
            if created:
                summary["voice_channels_created"] += 1
            _persist_channel_resource(runtime_updates, track_name, track_short, "lounge", lounge_channel)

        if assignment_channel_updates:
            def _mutate(config: Dict[str, Any]) -> None:
                channels = config.setdefault("discord_channels", {})
                for key, value in assignment_channel_updates.items():
                    channels[key] = value

            update_bot_config(_mutate)

        if runtime_updates and cohort_display:
            merge_discord_runtime_resources(
                cohort_display,
                {"tracks": runtime_updates},
                guild_id=guild.id,
            )

        return summary

    async def _sync_discord_resources_for_light_tracks(
        self,
        guild: discord.Guild,
        cohort: str,
        light_tracks: List[Dict[str, Any]],
        *,
        reason_label: str,
    ) -> Dict[str, Any]:
        cohort_display = _cohort_display_label(cohort)
        clean_cohort = _cohort_name_fragment(cohort_display)
        runtime_updates: Dict[str, Any] = {}
        assignment_channel_updates: Dict[str, str] = {}
        summary = _empty_discord_sync_summary()

        for track_data in light_tracks:
            track_name = str(track_data.get("trackName") or "").strip()
            members = track_data.get("members") or []
            if not track_name or not members:
                continue

            for target in self._build_light_track_targets(track_name, members):
                effective_track_name = str(target.get("trackName") or "").strip()
                parent_track_name = str(target.get("parentTrackName") or "").strip()
                role_prefix = str(target.get("rolePrefix") or "").strip()
                target_members = target.get("members") or []
                if not effective_track_name or not parent_track_name or not role_prefix or not target_members:
                    continue

                summary["tracks_processed"] += 1
                reason = f"{reason_label} ({cohort_display})"
                # sub_form: 크리에이터 라이트일 때만 '숏폼'/'롱폼' (빌더 라이트는 None).
                sub_form = _LIGHT_TRACK_SUB_FORM.get(effective_track_name)
                if sub_form:
                    track_short = f"{role_prefix}-라이트-{sub_form}"
                    light_role_name = f"{role_prefix}-{clean_cohort}기-라이트-{sub_form}"
                else:
                    track_short = f"{role_prefix}-라이트"
                    light_role_name = f"{role_prefix}-{clean_cohort}기-라이트"

                light_role, created = await self._ensure_role(guild, light_role_name, reason)
                if created:
                    summary["roles_created"] += 1
                _persist_role_resource(runtime_updates, effective_track_name, track_short, "light", light_role)

                if light_role:
                    for member_data in target_members:
                        if self._should_skip_mock_member_sync(member_data):
                            summary["mock_members_skipped"] += 1
                            continue

                        member_identifier = (
                            member_data.get("discordId")
                            or member_data.get("discord_id")
                            or member_data.get("userId")
                            or member_data.get("user_id")
                            or member_data.get("handle")
                            or member_data.get("name")
                        )
                        discord_member = await self._resolve_discord_member(guild, str(member_identifier or ""))
                        if not discord_member:
                            logger.warning(
                                f"[Role] Light member not found: {member_data.get('name', '?')} ({member_identifier})"
                            )
                            summary["role_failures"] += 1
                            summary["member_lookup_failures"] += 1
                            continue

                        try:
                            await discord_member.add_roles(light_role, reason=reason)
                            summary["roles_assigned"] += 1
                        except Exception as e:
                            logger.warning(f"[Role] Failed light role for {member_data.get('name', '?')}: {e}")
                            summary["role_failures"] += 1

                parent_track_short = _track_short_name(parent_track_name)
                runtime_track = _runtime_track_resources_for_cohort(cohort_display, parent_track_name)
                allowed_roles = self._resolve_runtime_track_roles(guild, runtime_track)
                if light_role and light_role not in allowed_roles:
                    allowed_roles.append(light_role)

                category = self._resolve_runtime_category(guild, runtime_track)
                if not category:
                    # 라이트 트랙도 부모 트랙(예: 크리에이터 트랙) 카테고리 형식 따름
                    category_name = _track_category_name(parent_track_name)
                    category, created = await self._ensure_category_channel(guild, category_name, reason)
                    if created:
                        summary["categories_created"] += 1
                    _persist_channel_resource(runtime_updates, parent_track_name, parent_track_short, "category", category)

                if not category or not light_role:
                    continue

                # 라이트 트랙 멤버에게 부모 트랙 전체 채널 세트 접근 부여 (additive).
                # 채널이 없으면 생성(라이트만 있어도 전체 세트 보장), 있으면 overwrite 보존 + 라이트 역할만 추가.
                try:
                    await self._ensure_light_track_channel_access(
                        guild,
                        category,
                        parent_track_name,
                        parent_track_short,
                        clean_cohort,
                        cohort_display,
                        light_role,
                        sub_form,
                        allowed_roles,
                        reason,
                        summary,
                        runtime_updates,
                        assignment_channel_updates,
                    )
                except Exception as e:
                    logger.warning(f"[Channel] Failed light channel-set for {parent_track_name}: {e}")

        if assignment_channel_updates:
            def _mutate(config: Dict[str, Any]) -> None:
                channels = config.setdefault("discord_channels", {})
                for key, value in assignment_channel_updates.items():
                    channels[key] = value

            update_bot_config(_mutate)

        if runtime_updates and cohort_display:
            merge_discord_runtime_resources(
                cohort_display,
                {"tracks": runtime_updates},
                guild_id=guild.id,
            )

        return summary

    async def sync_discord_group_preview(self, cohort: str, tracks: List[Dict[str, Any]]) -> Dict[str, Any]:
        # 안전장치: test 모드면 반드시 TEST_DISCORD_GUILD_ID 일치 길드만 사용.
        # prod/dev/legacy 는 bot.guilds[0] 폴백.
        guild = resolve_active_guild(self.bot)
        if not guild:
            raise RuntimeError("Bot is not connected to any guild.")

        track_groups: List[Dict[str, Any]] = []
        light_tracks: List[Dict[str, Any]] = []
        for track in tracks:
            track_name = str(track.get("groupDbName") or track.get("tabLabel") or track.get("trackName") or "").strip()
            tab_id = str(track.get("tabId") or "").strip()
            groups_payload = []
            for fallback_index, group in enumerate(track.get("groups", []), start=1):
                members_payload = []
                for member in group.get("members", []):
                    members_payload.append({
                        "name": str(member.get("id") or member.get("name") or member.get("handle") or "?").strip(),
                        "discordId": str(
                            member.get("discordId")
                            or member.get("userId")
                            or member.get("discord_id")
                            or member.get("handle")
                            or member.get("id")
                            or ""
                        ).strip(),
                        "userId": str(
                            member.get("userId")
                            or member.get("discordId")
                            or member.get("discord_id")
                            or member.get("id")
                            or ""
                        ).strip(),
                        "handle": str(member.get("handle") or "").strip(),
                        "rowTrackName": str(member.get("rowTrackName") or "").strip(),
                        "creatorSub": str(member.get("creatorSub") or "").strip(),
                        "isLeader": bool(member.get("leader") or member.get("isLeader")),
                    })

                # Empty 그룹도 forceCreateEmptyGroups 플래그가 있으면 구조만 생성
                # (역할/채널은 만들고 멤버 배정만 skip). 인프라 수동 부트스트랩용.
                if not members_payload and not bool(group.get("forceCreateEmpty")):
                    continue

                groups_payload.append({
                    "groupNumber": self._extract_group_number(group.get("groupNumber") or group.get("name"), fallback_index),
                    "groupName": group.get("name"),
                    "members": members_payload,
                })

            if groups_payload:
                if _is_light_track_payload(track_name, tab_id):
                    light_members: Dict[str, Dict[str, Any]] = {}
                    for group_payload in groups_payload:
                        for member_payload in group_payload.get("members", []):
                            light_member_key = str(
                                member_payload.get("discordId")
                                or member_payload.get("userId")
                                or member_payload.get("handle")
                                or member_payload.get("name")
                                or ""
                            ).strip()
                            if not light_member_key:
                                continue
                            existing_member = light_members.get(light_member_key)
                            if not existing_member:
                                light_members[light_member_key] = dict(member_payload)
                                continue
                            if member_payload.get("rowTrackName"):
                                existing_member["rowTrackName"] = member_payload.get("rowTrackName")
                            if member_payload.get("isLeader"):
                                existing_member["isLeader"] = True
                    if light_members:
                        light_tracks.append({
                            "trackName": track_name,
                            "tabId": tab_id,
                            "members": list(light_members.values()),
                        })
                else:
                    track_groups.append({
                        "trackName": track_name,
                        "tabId": tab_id,
                        "groups": groups_payload,
                    })

        summary = _empty_discord_sync_summary()
        if track_groups:
            summary = _merge_discord_sync_summary(
                summary,
                await self._sync_discord_resources_for_groups(
                    guild,
                    cohort,
                    track_groups,
                    reason_label="대시보드 조배정 확정",
                    include_channels=True,
                ),
            )
        if light_tracks:
            summary = _merge_discord_sync_summary(
                summary,
                await self._sync_discord_resources_for_light_tracks(
                    guild,
                    cohort,
                    light_tracks,
                    reason_label="대시보드 라이트 트랙 확정",
                ),
            )

        return summary

    @commands.command(name='채널삭제', aliases=['cleanup_cohort_channels'])
    @commands.has_permissions(administrator=True)
    async def cleanup_cohort_channels(self, ctx, cohort: Optional[str] = None):
        """
        지난 기수의 트랙 채널 + 트랙 역할 삭제. 공지 채널은 보존.

        사용법: `!채널삭제 8`  → 8기 트랙 채널·역할 정리

        삭제 대상:
          [채널] 모두 만족
            1) 부모 카테고리가 `=====...=====` 트랙 카테고리
            2) 이름이 `{트랙 prefix}-{기수}기-...` 형식
            3) 이름이 `-공지` 로 끝나지 않음
          [역할] 모두 만족
            1) 이름이 `{트랙 prefix}-{기수}기-...` 형식 또는 `{트랙 prefix}-{기수}기`
            2) 봇/통합 관리 역할 아님 (`role.managed=False`)
            3) `@everyone` 아님

        보존 대상:
          - 트랙 카테고리 자체 (다음 기수가 같은 카테고리 누적)
          - `-공지` 채널 (역사 보존)
          - 카테고리 밖 채널 (자유게시판, 전체-공지 등)
          - 다른 기수의 트랙 역할 / 시스템·봇 역할 / @everyone

        안전장치:
          - 관리자 권한 필요
          - 삭제 대상 (채널 + 역할) 미리 표시 → 30초 내 `확인` 입력 시에만 진행
        """
        if not ctx.guild:
            await ctx.reply("❌ 길드 채널에서만 실행 가능합니다.")
            return

        raw_cohort = (cohort or '').strip().replace('기', '')
        if not raw_cohort.isdigit():
            await ctx.reply(
                "❌ 기수를 숫자로 지정해주세요. 예: `!채널삭제 8` (8기 트랙 채널·역할 삭제)"
            )
            return
        cohort_num = raw_cohort  # 문자열로 유지 — `-{cohort}기-` 매칭에 그대로 사용

        # 현재 매핑된 트랙 prefix + 과거 사용된 레거시 prefix
        _LEGACY_TRACK_PREFIXES = [
            "빌더-라이트",
            "크리에이터-라이트",
            "AI에이전트",       # 'AI에이전트-실전' 의 짧은 변형
        ]
        track_short_prefixes = sorted(
            set(list(_TRACK_DISCORD_PREFIX.values()) + _LEGACY_TRACK_PREFIXES),
            key=len,
            reverse=True,
        )

        cohort_marker = f"-{cohort_num}기"

        def _is_track_category(category: Optional[discord.CategoryChannel]) -> bool:
            if not category or not category.name:
                return False
            return category.name.startswith('=====') and category.name.endswith('=====')

        def _name_matches_cohort_track(name: str) -> bool:
            """이름이 `{prefix}-{cohort}기` 또는 `{prefix}-{cohort}기-...` 인지."""
            if not name:
                return False
            for prefix in track_short_prefixes:
                head = f"{prefix}{cohort_marker}"
                if name == head or name.startswith(f"{head}-"):
                    return True
            return False

        def _is_cohort_track_channel(channel: discord.abc.GuildChannel) -> bool:
            """채널 삭제 후보:
              - 부모가 `=====...=====` 트랙 카테고리
              - 이름이 `{prefix}-{cohort}기` 패턴
              - `-공지` 로 끝나지 않음
            """
            if isinstance(channel, discord.CategoryChannel):
                return False
            if not _is_track_category(getattr(channel, 'category', None)):
                return False
            name = channel.name or ''
            if name.endswith('-공지'):
                return False
            return _name_matches_cohort_track(name)

        def _is_cohort_track_role(role: discord.Role) -> bool:
            """역할 삭제 후보:
              - 시스템/봇 통합 역할 / @everyone 아님
              - 이름이 `{prefix}-{cohort}기` 패턴
            """
            if role.managed or role.is_default():
                return False
            return _name_matches_cohort_track(role.name or '')

        guild = ctx.guild
        channel_candidates: List[discord.abc.GuildChannel] = [
            ch for ch in guild.channels if _is_cohort_track_channel(ch)
        ]
        role_candidates: List[discord.Role] = [
            r for r in guild.roles if _is_cohort_track_role(r)
        ]

        if not channel_candidates and not role_candidates:
            await ctx.reply(
                f"ℹ️ {cohort_num}기 트랙 채널·역할 중 삭제 대상이 없습니다. "
                f"(공지 채널 제외, `=====...=====` 카테고리 내 `{{prefix}}-{cohort_num}기-...` 채널 + "
                f"`{{prefix}}-{cohort_num}기-...` 역할 기준)"
            )
            return

        preview_lines = []
        if channel_candidates:
            preview_lines.append(f"**채널 ({len(channel_candidates)}개)**")
            for ch in channel_candidates[:15]:
                kind = '음성' if isinstance(ch, discord.VoiceChannel) else '텍스트'
                cat_name = ch.category.name if ch.category else '(없음)'
                preview_lines.append(f"  • [{kind}] `{ch.name}` — `{cat_name}`")
            ch_more = len(channel_candidates) - min(15, len(channel_candidates))
            if ch_more > 0:
                preview_lines.append(f"  … 외 {ch_more}개")
        if role_candidates:
            preview_lines.append(f"**역할 ({len(role_candidates)}개)**")
            for r in role_candidates[:15]:
                preview_lines.append(f"  • `{r.name}` (멤버 {len(r.members)}명)")
            r_more = len(role_candidates) - min(15, len(role_candidates))
            if r_more > 0:
                preview_lines.append(f"  … 외 {r_more}개")

        await ctx.reply(
            f"⚠️ **{cohort_num}기 트랙 정리** — 채널 **{len(channel_candidates)}개** + "
            f"역할 **{len(role_candidates)}개** 삭제 예정\n"
            f"공지(`-공지`) 채널 · 트랙 카테고리(`=====...=====`) · 카테고리 밖 채널 · "
            f"시스템 역할은 보존됩니다.\n\n"
            + "\n".join(preview_lines)
            + f"\n\n**30초 안에 `확인`** 이라고 답글 주시면 진행, 아니면 취소됩니다."
        )

        def _confirm_check(m):
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.strip() == '확인'
            )

        try:
            await self.bot.wait_for('message', check=_confirm_check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.reply("❌ 30초 초과. 삭제 취소됨.")
            return

        status = await ctx.reply(f"🗑️ {cohort_num}기 트랙 채널·역할 삭제 진행 중...")

        deleted_channels = 0
        deleted_roles = 0
        errors = 0
        reason = f"{cohort_num}기 트랙 정리 (공지·카테고리 보존)"

        # 1) 채널 삭제 — 직전 재검사 (동시 변경 방어)
        for channel in channel_candidates:
            if not _is_cohort_track_channel(channel):
                logger.warning(
                    f"[Cleanup] 채널 {channel.name} 재검사 실패 — 카테고리/이름 변경 감지, skip"
                )
                continue
            try:
                await channel.delete(reason=reason)
                deleted_channels += 1
            except Exception as e:
                logger.warning(f"[Cleanup] 채널 삭제 실패 {channel.name}: {e}")
                errors += 1

        # 2) 역할 삭제 — 직전 재검사
        for role in role_candidates:
            if not _is_cohort_track_role(role):
                logger.warning(
                    f"[Cleanup] 역할 {role.name} 재검사 실패 — 이름 변경/managed 전환, skip"
                )
                continue
            try:
                await role.delete(reason=reason)
                deleted_roles += 1
            except Exception as e:
                logger.warning(f"[Cleanup] 역할 삭제 실패 {role.name}: {e}")
                errors += 1

        await status.edit(content=(
            f"✅ {cohort_num}기 트랙 정리 완료\n"
            f"• 채널 삭제: **{deleted_channels}**개\n"
            f"• 역할 삭제: **{deleted_roles}**개\n"
            f"• 실패: {errors}개\n"
            f"• 보존: 공지 채널 / 카테고리 / 카테고리 밖 채널 / 시스템·다른 기수 역할"
        ))

    @commands.command(name='역할삭제', aliases=['cleanup_cohort_roles'])
    @commands.has_permissions(administrator=True)
    async def cleanup_cohort_roles(self, ctx, cohort: Optional[str] = None):
        """
        지난 기수의 트랙 역할만 삭제. 채널·카테고리는 일체 건드리지 않음.

        사용법: `!역할삭제 9`  → 9기 트랙 역할만 정리

        삭제 대상 (모두 만족):
          - 이름이 `{트랙 prefix}-{기수}기` 또는 `{트랙 prefix}-{기수}기-...` 형식
          - 봇/통합 관리 역할 아님 (`role.managed=False`)
          - `@everyone` 아님

        보존:
          - 모든 채널·카테고리 (이 명령어는 역할만 다룸)
          - 다른 기수 트랙 역할 / 시스템·봇 역할 / 운영자 / @everyone

        안전장치:
          - 관리자 권한 필요
          - 삭제 대상 미리 표시 → 30초 내 `확인` 답글 필요
        """
        if not ctx.guild:
            await ctx.reply("❌ 길드 채널에서만 실행 가능합니다.")
            return

        raw_cohort = (cohort or '').strip().replace('기', '')
        if not raw_cohort.isdigit():
            await ctx.reply(
                "❌ 기수를 숫자로 지정해주세요. 예: `!역할삭제 9` (9기 트랙 역할 삭제)"
            )
            return
        cohort_num = raw_cohort

        # 트랙 prefix 매칭 — cleanup_cohort_channels 와 동일한 룰 사용.
        _LEGACY_TRACK_PREFIXES = [
            "빌더-라이트",
            "크리에이터-라이트",
            "AI에이전트",
            "AI에이전트-실전",   # 구 prefix — 옛 '-실전' 채널/역할 정리용
        ]
        track_short_prefixes = sorted(
            set(list(_TRACK_DISCORD_PREFIX.values()) + _LEGACY_TRACK_PREFIXES),
            key=len,
            reverse=True,
        )

        cohort_marker = f"-{cohort_num}기"

        def _name_matches_cohort_track(name: str) -> bool:
            if not name:
                return False
            for prefix in track_short_prefixes:
                head = f"{prefix}{cohort_marker}"
                if name == head or name.startswith(f"{head}-"):
                    return True
            return False

        def _is_cohort_track_role(role: discord.Role) -> bool:
            if role.managed or role.is_default():
                return False
            return _name_matches_cohort_track(role.name or '')

        guild = ctx.guild
        role_candidates: List[discord.Role] = [
            r for r in guild.roles if _is_cohort_track_role(r)
        ]

        if not role_candidates:
            await ctx.reply(
                f"ℹ️ {cohort_num}기 트랙 역할 중 삭제 대상이 없습니다. "
                f"(`{{prefix}}-{cohort_num}기-...` 패턴 기준, 시스템·운영자 역할 제외)"
            )
            return

        preview_lines = [f"**역할 ({len(role_candidates)}개)**"]
        for r in role_candidates[:20]:
            preview_lines.append(f"  • `{r.name}` (멤버 {len(r.members)}명)")
        r_more = len(role_candidates) - min(20, len(role_candidates))
        if r_more > 0:
            preview_lines.append(f"  … 외 {r_more}개")

        await ctx.reply(
            f"⚠️ **{cohort_num}기 트랙 역할 정리** — 역할 **{len(role_candidates)}개** 삭제 예정\n"
            f"채널·카테고리는 일체 건드리지 않습니다. 시스템·운영자·다른 기수 역할은 보존됩니다.\n\n"
            + "\n".join(preview_lines)
            + f"\n\n**30초 안에 `확인`** 이라고 답글 주시면 진행, 아니면 취소됩니다."
        )

        def _confirm_check(m):
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.strip() == '확인'
            )

        try:
            await self.bot.wait_for('message', check=_confirm_check, timeout=30.0)
        except asyncio.TimeoutError:
            await ctx.reply("❌ 30초 초과. 삭제 취소됨.")
            return

        status = await ctx.reply(f"🗑️ {cohort_num}기 트랙 역할 삭제 진행 중...")

        deleted_roles = 0
        errors = 0
        error_details: List[str] = []
        reason = f"{cohort_num}기 트랙 역할 정리 (채널 보존)"

        for role in role_candidates:
            if not _is_cohort_track_role(role):
                logger.warning(
                    f"[CleanupRoles] 역할 {role.name} 재검사 실패 — 이름 변경/managed 전환, skip"
                )
                continue
            try:
                await role.delete(reason=reason)
                deleted_roles += 1
            except discord.Forbidden as e:
                # 봇 권한·위계 부족 — 진단에 유용한 정보라 details 에 기록.
                logger.warning(f"[CleanupRoles] 권한·위계 부족 {role.name}: {e}")
                errors += 1
                if len(error_details) < 5:
                    error_details.append(f"`{role.name}` (위계 부족)")
            except Exception as e:
                logger.warning(f"[CleanupRoles] 역할 삭제 실패 {role.name}: {e}")
                errors += 1
                if len(error_details) < 5:
                    error_details.append(f"`{role.name}` ({type(e).__name__})")

        # 실패 사유 첨부 — 사용자가 봇 위계 점검할 수 있게.
        error_section = ''
        if errors > 0:
            error_section = (
                f"\n• 실패: {errors}개"
                + (f" — {', '.join(error_details)}" if error_details else '')
                + ("…" if errors > len(error_details) else '')
                + "\n  └ 봇 역할이 해당 역할보다 길드 위계에서 **위에 있어야** 삭제 가능합니다."
            )

        await status.edit(content=(
            f"✅ {cohort_num}기 트랙 역할 정리 완료\n"
            f"• 역할 삭제: **{deleted_roles}**개"
            + error_section
            + f"\n• 보존: 채널 일체 / 시스템·운영자·다른 기수 역할"
        ))


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
