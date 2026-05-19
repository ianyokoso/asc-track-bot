import os
from typing import Dict, List, Optional

from dotenv import load_dotenv


# 🛑 PROD 길드 하드 블랙리스트 — 이 ID 들은 어떤 환경/설정 실수에도 절대 건드리지 않는다.
#    1383082575500677142 = AI Solopreneur Club (운영 디스코드)
PROD_DISCORD_GUILD_BLACKLIST = frozenset({
    1383082575500677142,
})


def is_prod_guild_blacklisted(guild_id) -> bool:
    """주어진 길드 ID 가 prod 블랙리스트에 속하는지 확인."""
    try:
        return int(guild_id) in PROD_DISCORD_GUILD_BLACKLIST
    except (TypeError, ValueError):
        return False


def normalize_env_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    raw = value.strip().lower()
    mapping = {
        "production": "prod",
        "live": "prod",
        "sandbox": "test",
        "staging": "test",
        "mock": "test",
        "development": "dev",
        "local": "dev",
    }
    return mapping.get(raw, raw)


def get_runtime_env(explicit: Optional[str] = None) -> Optional[str]:
    return normalize_env_name(explicit or os.getenv("ASC_ENV") or os.getenv("RUN_MODE"))


def _existing(path: str) -> bool:
    return os.path.exists(path)


def _load(path: str, override: bool) -> bool:
    if not _existing(path):
        return False
    load_dotenv(path, override=override)
    return True


def load_backend_env(base_dir: str, explicit: Optional[str] = None) -> Dict[str, object]:
    """
    Load env files for the backend.

    Modes:
    - prod: .env -> .env.prod
    - test: .env -> .env.prod -> .env.test
    - dev : .env -> .env.dev
    - none: preserve legacy behavior (.env -> .env.prod -> .env.dev[fill-only])
    """
    env_file = os.path.join(base_dir, ".env")
    prod_file = os.path.join(base_dir, ".env.prod")
    dev_file = os.path.join(base_dir, ".env.dev")
    test_file = os.path.join(base_dir, ".env.test")

    loaded: List[str] = []

    if _load(env_file, override=False):
        loaded.append(env_file)

    env_name = get_runtime_env(explicit)

    if env_name == "prod":
        if _load(prod_file, override=True):
            loaded.append(prod_file)
    elif env_name == "test":
        if _load(prod_file, override=True):
            loaded.append(prod_file)
        if _load(test_file, override=True):
            loaded.append(test_file)
    elif env_name == "dev":
        if _load(dev_file, override=True):
            loaded.append(dev_file)
    else:
        if _load(prod_file, override=True):
            loaded.append(prod_file)
        if _load(dev_file, override=False):
            loaded.append(dev_file)

    return {
        "env_name": env_name or "legacy",
        "loaded_files": loaded,
        "writable_env_file": get_writable_env_file(base_dir, explicit),
    }


def get_bot_config_file(base_dir: str, explicit: Optional[str] = None) -> str:
    env_name = get_runtime_env(explicit)
    if env_name == "test":
        return os.path.join(base_dir, "bot_config.test.json")
    if env_name == "dev":
        return os.path.join(base_dir, "bot_config.dev.json")
    return os.path.join(base_dir, "bot_config.json")


def get_bot_command_queue_file(base_dir: str, explicit: Optional[str] = None) -> str:
    env_name = get_runtime_env(explicit)
    if env_name == "test":
        return os.path.join(base_dir, "bot_command_queue.test.json")
    if env_name == "dev":
        return os.path.join(base_dir, "bot_command_queue.dev.json")
    return os.path.join(base_dir, "bot_command_queue.json")


def get_bot_heartbeat_file(base_dir: str, explicit: Optional[str] = None) -> str:
    env_name = get_runtime_env(explicit)
    if env_name == "test":
        return os.path.join(base_dir, "bot_heartbeat.test.json")
    if env_name == "dev":
        return os.path.join(base_dir, "bot_heartbeat.dev.json")
    return os.path.join(base_dir, "bot_heartbeat.json")


def resolve_active_guild(bot, explicit: Optional[str] = None):
    """
    봇이 사용해야 할 길드를 환경에 맞게 안전하게 선택한다.

    우선순위:
      0) DISCORD_TARGET_GUILD_ID 명시적 오버라이드 — 운영자가 의도적으로 특정 길드
         (보통 prod) 를 대상으로 채널/역할 생성을 트리거할 때 사용. 봇이 해당 길드의
         멤버여야 하며, prod 블랙리스트 가드도 우회 (의도적 opt-in 이므로).
      1) test 모드 + TEST_DISCORD_GUILD_ID — 옛 동작. prod 블랙리스트 길드는 거부.
      2) 그 외 (dev/prod/legacy) — bot.guilds[0] 폴백.

    Returns: discord.Guild | None
    Raises:  RuntimeError (가드 위반)
    """
    # ── 0) 명시적 길드 오버라이드 ─────────────────────────────────
    target_override = (os.getenv("DISCORD_TARGET_GUILD_ID") or "").strip()
    if target_override:
        try:
            target_id = int(target_override)
        except ValueError:
            raise RuntimeError(
                f"DISCORD_TARGET_GUILD_ID 가 숫자가 아닙니다: {target_override!r}"
            )
        guild = next((g for g in bot.guilds if g.id == target_id), None)
        if not guild:
            raise RuntimeError(
                f"DISCORD_TARGET_GUILD_ID={target_id} 길드에 봇이 가입돼 있지 않습니다. "
                f"봇을 해당 서버에 초대했는지 확인하세요."
            )
        return guild

    # ── 1) test 모드 strict 가드 ───────────────────────────────────
    env_name = get_runtime_env(explicit)
    if env_name == "test":
        test_guild_raw = (os.getenv("TEST_DISCORD_GUILD_ID") or "").strip()
        if not test_guild_raw:
            raise RuntimeError(
                "TEST_DISCORD_GUILD_ID 가 비어있습니다. test 모드에서는 명시적인 테스트 길드 ID 가 필요합니다."
            )
        try:
            test_guild_id = int(test_guild_raw)
        except ValueError:
            raise RuntimeError(f"TEST_DISCORD_GUILD_ID 가 숫자가 아닙니다: {test_guild_raw!r}")

        # 🛑 prod 블랙리스트 길드를 TEST_DISCORD_GUILD_ID 로 잘못 설정한 경우 거부.
        if is_prod_guild_blacklisted(test_guild_id):
            raise RuntimeError(
                f"TEST_DISCORD_GUILD_ID={test_guild_id} 는 PROD 블랙리스트입니다. 거부합니다."
            )

        guild = next((g for g in bot.guilds if g.id == test_guild_id), None)
        if not guild:
            raise RuntimeError(
                f"테스트 길드 {test_guild_id} 를 봇 길드 목록에서 찾지 못했습니다. "
                f"봇이 해당 서버에 가입돼 있는지 확인하세요."
            )

        # 이중 검증: 매칭된 길드가 그래도 블랙리스트면 거부.
        if is_prod_guild_blacklisted(guild.id):
            raise RuntimeError(
                f"매칭된 길드 {guild.id} 가 PROD 블랙리스트입니다. 거부합니다."
            )
        return guild

    # ── 2) dev / prod / legacy 폴백 ───────────────────────────────
    return bot.guilds[0] if bot.guilds else None


def get_writable_env_file(base_dir: str, explicit: Optional[str] = None) -> str:
    env_name = get_runtime_env(explicit)
    prod_file = os.path.join(base_dir, ".env.prod")
    env_file = os.path.join(base_dir, ".env")
    dev_file = os.path.join(base_dir, ".env.dev")
    test_file = os.path.join(base_dir, ".env.test")

    if env_name == "test":
        return test_file
    if env_name == "dev":
        return dev_file
    if env_name == "prod":
        return prod_file if _existing(prod_file) else env_file

    if _existing(prod_file):
        return prod_file
    if _existing(env_file):
        return env_file
    return dev_file
