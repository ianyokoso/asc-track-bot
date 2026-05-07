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
from env_utils import resolve_active_guild, is_prod_guild_blacklisted

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
    "AI 에이전트 트랙":              "AI에이전트-실전",
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

    if role_kind == "leader":
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
        channel = discord.utils.get(guild.text_channels, name=channel_name)
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
        channel = discord.utils.get(guild.voice_channels, name=channel_name)
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
            leader_role_name = f"{track_short}-{clean_cohort}기-조장"

            leader_role, created = await self._ensure_role(guild, leader_role_name, reason)
            if created:
                summary["roles_created"] += 1
            _persist_role_resource(runtime_updates, track_name, track_short, "leader", leader_role)

            track_group_roles: Dict[str, Optional[discord.Role]] = {}
            for fallback_index, group_data in enumerate(groups, start=1):
                group_number = self._extract_group_number(
                    group_data.get("groupNumber") or group_data.get("groupName") or group_data.get("name"),
                    fallback_index,
                )
                group_role_name = f"{track_short}-{clean_cohort}기-{group_number}조"
                group_role, created = await self._ensure_role(guild, group_role_name, reason)
                if created:
                    summary["roles_created"] += 1
                track_group_roles[str(group_number)] = group_role
                _persist_role_resource(
                    runtime_updates,
                    track_name,
                    track_short,
                    "group",
                    group_role,
                    group_number=group_number,
                )

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

                    roles_to_add = [role for role in [group_role] if role]
                    if member_data.get("isLeader") and leader_role:
                        roles_to_add.append(leader_role)

                    if not roles_to_add:
                        continue

                    try:
                        await discord_member.add_roles(*roles_to_add, reason=reason)
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

            shared_roles = [role for role in track_group_roles.values() if role]
            if not category or not shared_roles:
                continue

            announcement_name = f"{track_short}-{clean_cohort}기-공지"
            announcement_channel, created = await self._ensure_text_channel(
                guild,
                category,
                announcement_name,
                reason,
                overwrites=self._build_channel_overwrites(guild, shared_roles, read_only=True),
                topic=f"{cohort_display} {track_name} 공지 채널",
            )
            if created:
                summary["announcement_channels_created"] += 1
            _persist_channel_resource(runtime_updates, track_name, track_short, "announcement", announcement_channel)

            # 과제 채널 — 운영 서버 형식: '{prefix}-{cohort}기-과제-인증'.
            # 크리에이터 트랙은 숏폼/롱폼 두 채널로 분리.
            if track_short == "크리에이터":
                assignment_specs = [
                    (f"{track_short}-{clean_cohort}기-숏폼-과제-인증", "숏폼 과제 인증"),
                    (f"{track_short}-{clean_cohort}기-롱폼-과제-인증", "롱폼 과제 인증"),
                ]
            else:
                assignment_specs = [
                    (f"{track_short}-{clean_cohort}기-과제-인증", "과제 인증"),
                ]

            assignment_channel = None
            for assign_name, assign_label in assignment_specs:
                a_channel, created = await self._ensure_text_channel(
                    guild,
                    category,
                    assign_name,
                    reason,
                    overwrites=self._build_channel_overwrites(guild, shared_roles, read_only=False),
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

            # 조장 전용 텍스트 채널 — 조장 역할만 view 가능
            if leader_role:
                leader_channel_name = f"{track_short}-{clean_cohort}기-조장"
                leader_channel, created = await self._ensure_text_channel(
                    guild,
                    category,
                    leader_channel_name,
                    reason,
                    overwrites=self._build_channel_overwrites(guild, [leader_role], read_only=False),
                    topic=f"{cohort_display} {track_name} 조장 전용 채널",
                )
                if created:
                    summary["leader_channels_created"] += 1
                _persist_channel_resource(runtime_updates, track_name, track_short, "leader", leader_channel)

            for fallback_index, group_data in enumerate(groups, start=1):
                group_number = self._extract_group_number(
                    group_data.get("groupNumber") or group_data.get("groupName") or group_data.get("name"),
                    fallback_index,
                )
                group_role = track_group_roles.get(str(group_number))
                if not group_role:
                    continue

                group_channel_name = f"{track_short}-{clean_cohort}기-{group_number}조"
                group_channel, created = await self._ensure_text_channel(
                    guild,
                    category,
                    group_channel_name,
                    reason,
                    overwrites=self._build_channel_overwrites(guild, [group_role], read_only=False),
                    topic=f"{cohort_display} {track_name} {group_number}조 전용 채널",
                )
                if created:
                    summary["group_channels_created"] += 1
                _persist_channel_resource(
                    runtime_updates,
                    track_name,
                    track_short,
                    "group",
                    group_channel,
                    group_number=group_number,
                )

                # 조별 화상미팅 (음성 채널) — 같은 조 역할만 view 가능
                voice_channel_name = f"{track_short}-{clean_cohort}기-{group_number}조-화상미팅"
                voice_channel, created = await self._ensure_voice_channel(
                    guild,
                    category,
                    voice_channel_name,
                    reason,
                    overwrites=self._build_channel_overwrites(guild, [group_role], read_only=False),
                )
                if created:
                    summary["voice_channels_created"] += 1
                _persist_channel_resource(
                    runtime_updates,
                    track_name,
                    track_short,
                    "voice",
                    voice_channel,
                    group_number=group_number,
                )

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

                announcement_channel = self._resolve_runtime_text_channel(guild, runtime_track, "announcement")
                if announcement_channel:
                    try:
                        await self._grant_role_access_to_channel(
                            announcement_channel,
                            light_role,
                            read_only=True,
                            reason=reason,
                        )
                    except Exception as e:
                        logger.warning(f"[Channel] Failed to grant light access to announcement channel: {e}")
                else:
                    announcement_name = f"{parent_track_short}-{clean_cohort}기-공지"
                    announcement_channel, created = await self._ensure_text_channel(
                        guild,
                        category,
                        announcement_name,
                        reason,
                        overwrites=self._build_channel_overwrites(guild, allowed_roles, read_only=True),
                        topic=f"{cohort_display} {parent_track_name} 공지 채널",
                    )
                    if created:
                        summary["announcement_channels_created"] += 1
                    _persist_channel_resource(
                        runtime_updates,
                        parent_track_name,
                        parent_track_short,
                        "announcement",
                        announcement_channel,
                    )

                # 라이트 트랙의 과제-인증 채널 매칭.
                # - sub_form 있음 (크리에이터 숏폼/롱폼): 해당 sub-form 채널 1개만 매칭
                #   예) sub_form='숏폼' → '크리에이터-9기-숏폼-과제-인증' 만 매칭
                # - sub_form 없음 (빌더 라이트, legacy 크리에이터 라이트): 부모 카테고리 안의
                #   '{parent_short}-{cohort}기-...과제-인증' 모두 매칭 (기존 동작)
                parent_cohort_prefix = f"{parent_track_short}-{clean_cohort}기-"
                if sub_form:
                    target_channel_name = f"{parent_track_short}-{clean_cohort}기-{sub_form}-과제-인증"
                    assignment_channels = [
                        ch for ch in (category.text_channels if category else [])
                        if ch.name == target_channel_name
                    ]
                else:
                    assignment_channels = [
                        ch for ch in (category.text_channels if category else [])
                        if ch.name.startswith(parent_cohort_prefix) and ch.name.endswith("-과제-인증")
                    ]

                # 매칭된 채널이 하나도 없으면 새로 생성.
                # sub_form 있으면 sub-form 전용 채널, 없으면 단일 과제-인증 채널.
                if not assignment_channels:
                    if sub_form:
                        assignment_name = f"{parent_track_short}-{clean_cohort}기-{sub_form}-과제-인증"
                        topic = f"{cohort_display} {parent_track_name} {sub_form} 과제 인증 채널"
                    else:
                        assignment_name = f"{parent_track_short}-{clean_cohort}기-과제-인증"
                        topic = f"{cohort_display} {parent_track_name} 과제 인증 채널"
                    new_channel, created = await self._ensure_text_channel(
                        guild,
                        category,
                        assignment_name,
                        reason,
                        overwrites=self._build_channel_overwrites(guild, allowed_roles, read_only=False),
                        topic=topic,
                    )
                    if created:
                        summary["assignment_channels_created"] += 1
                    if new_channel:
                        assignment_channels.append(new_channel)
                        _persist_channel_resource(
                            runtime_updates,
                            parent_track_name,
                            parent_track_short,
                            "assignment",
                            new_channel,
                        )

                # 모든 과제-인증 채널에 라이트 역할 부여 (write 가능)
                for ch in assignment_channels:
                    try:
                        await self._grant_role_access_to_channel(
                            ch,
                            light_role,
                            read_only=False,
                            reason=reason,
                        )
                    except Exception as e:
                        logger.warning(f"[Channel] Failed to grant light access to {ch.name}: {e}")

                # config 매핑은 첫 번째 채널 ID 사용 (호환)
                if assignment_channels:
                    for config_key in _config_channel_keys_for_track(parent_track_name):
                        assignment_channel_updates[config_key] = str(assignment_channels[0].id)

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
                        "isLeader": bool(member.get("leader") or member.get("isLeader")),
                    })

                if not members_payload:
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

    @commands.command(name='테스트초기화', aliases=['cleanup_test'])
    @commands.has_permissions(administrator=True)
    async def cleanup_test_server(self, ctx):
        """
        [테스트 전용] 봇이 test 서버에 만든 트랙 카테고리/채널/역할 모두 삭제.

        3중 안전장치:
          1) ASC_ENV=test 모드에서만 동작
          2) 명령 실행 길드가 TEST_DISCORD_GUILD_ID 와 일치해야 함
          3) 사용자가 30초 안에 '확인' 메시지를 보내야 진행

        삭제 대상:
          - 이름이 '====='로 시작하고 '====='로 끝나는 카테고리 + 그 안의 모든 채널
          - 트랙 short name 으로 시작하는 역할 (예: '세일즈-실전-9기-조장')
        """
        env_name = (os.getenv('ASC_ENV') or os.getenv('RUN_MODE') or '').strip().lower()
        if env_name != 'test':
            await ctx.reply("❌ test 모드에서만 실행 가능합니다. (`ASC_ENV=test`)")
            return

        test_guild_raw = (os.getenv('TEST_DISCORD_GUILD_ID') or '').strip()
        if not test_guild_raw:
            await ctx.reply("❌ `TEST_DISCORD_GUILD_ID` 환경변수가 설정되지 않았습니다.")
            return
        try:
            test_guild_id = int(test_guild_raw)
        except ValueError:
            await ctx.reply(f"❌ `TEST_DISCORD_GUILD_ID` 값이 숫자가 아닙니다: `{test_guild_raw}`")
            return

        if ctx.guild is None or ctx.guild.id != test_guild_id:
            await ctx.reply(
                f"❌ 이 서버({ctx.guild.id if ctx.guild else 'DM'}) 는 "
                f"TEST_DISCORD_GUILD_ID(`{test_guild_id}`) 와 일치하지 않습니다. 안전을 위해 거부합니다."
            )
            return

        # 🛑 절대 가드: 길드 ID 가 prod 블랙리스트면 무조건 거부
        if is_prod_guild_blacklisted(ctx.guild.id):
            await ctx.reply(
                f"⛔ **거부됨** — 길드 `{ctx.guild.id}` 는 PROD 블랙리스트입니다. "
                "이 명령은 prod 서버에서 절대 실행될 수 없습니다."
            )
            return

        await ctx.reply(
            "⚠️ **테스트 서버 초기화** — 다음을 영구 삭제합니다:\n"
            "• 이름이 `=====...=====` 인 카테고리\n"
            "• 그 카테고리 안의 모든 채널\n"
            "• 트랙 관련 역할 (예: `세일즈-실전-9기-조장`, `빌더-기초-9기-1조`)\n\n"
            "**30초 안에 `확인`** 이라고 답글 주시면 진행, 아니면 취소됩니다."
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
            await ctx.reply("❌ 30초 초과. 초기화 취소됨.")
            return

        status = await ctx.reply("🗑️ 초기화 진행 중...")

        guild = ctx.guild
        deleted_channels = 0
        deleted_categories = 0
        deleted_roles = 0
        errors = 0

        # 현재 매핑된 트랙 prefix + 과거 사용된 레거시 prefix 까지 모두 포함
        _LEGACY_TRACK_PREFIXES = [
            "나-다움",          # 옛 이름 (현재: 나탐구)
            "빌더-라이트",      # 라이트 트랙 (현재: 부모 트랙 카테고리에 통합)
            "크리에이터-라이트",
            "AI에이전트",       # 'AI에이전트-실전' 의 짧은 변형
        ]
        track_short_prefixes = sorted(
            set(list(_TRACK_DISCORD_PREFIX.values()) + _LEGACY_TRACK_PREFIXES),
            key=len,
            reverse=True,
        )

        def _matches_track_resource(name: str) -> bool:
            """트랙 카테고리/채널/역할 이름인지 판별.
            매칭:
              - `=====...=====` 형식 (신 명명 규칙)
              - `{track_short}-...` 형식 (구 per-기수 명명 규칙: '세일즈-실전-9기-공지' 등)
              - `{track_short}` 단독 (정확히 일치)
              - 레거시 prefix (`나-다움-`, `빌더-라이트-`, `크리에이터-라이트-`, `AI에이전트-`)
            """
            if not name:
                return False
            if name.startswith('=====') and name.endswith('====='):
                return True
            for prefix in track_short_prefixes:
                if name == prefix or name.startswith(f"{prefix}-"):
                    return True
            return False

        # 1) 매칭되는 모든 채널 (텍스트/보이스/스레드 부모 등) 삭제 — 카테고리는 제외하고 먼저
        for channel in list(guild.channels):
            if isinstance(channel, discord.CategoryChannel):
                continue
            # 채널 이름 자체가 매칭되거나, 매칭되는 카테고리 안에 있으면 삭제
            in_track_category = bool(channel.category and _matches_track_resource(channel.category.name))
            if _matches_track_resource(channel.name) or in_track_category:
                try:
                    await channel.delete(reason='테스트 서버 초기화')
                    deleted_channels += 1
                except Exception as e:
                    logger.warning(f"[Cleanup] 채널 삭제 실패 {channel.name}: {e}")
                    errors += 1

        # 2) 카테고리 삭제 (안의 채널이 다 빠진 후)
        for category in list(guild.categories):
            if _matches_track_resource(category.name):
                try:
                    await category.delete(reason='테스트 서버 초기화')
                    deleted_categories += 1
                except Exception as e:
                    logger.warning(f"[Cleanup] 카테고리 삭제 실패 {category.name}: {e}")
                    errors += 1

        # 3) 트랙 관련 역할 삭제
        for role in list(guild.roles):
            # 시스템 역할 / 봇 자동 생성 역할 / @everyone 보호
            if role.managed or role.is_default():
                continue
            if _matches_track_resource(role.name):
                try:
                    await role.delete(reason='테스트 서버 초기화')
                    deleted_roles += 1
                except Exception as e:
                    logger.warning(f"[Cleanup] 역할 삭제 실패 {role.name}: {e}")
                    errors += 1

        await status.edit(content=(
            f"✅ 테스트 서버 초기화 완료\n"
            f"• 카테고리 삭제: **{deleted_categories}**\n"
            f"• 채널 삭제: **{deleted_channels}**\n"
            f"• 역할 삭제: **{deleted_roles}**\n"
            f"• 실패: {errors}"
        ))


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
