import os
import json
import logging
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, Optional, Any, Union, Callable

from env_utils import get_bot_config_file, load_backend_env

# Setup Logger
logger = logging.getLogger('config')

# Constants
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_INFO = load_backend_env(BASE_DIR)
CONFIG_FILE = get_bot_config_file(BASE_DIR, explicit=ENV_INFO["env_name"])

# Global Config Storage
CONFIG: Dict[str, Optional[str]] = {}
BOT_CONFIG: Dict[str, Any] = {}
_BOT_CONFIG_LOCK = threading.RLock()

def load_config() -> None:
    """
    Load environment variables from .env file into global CONFIG dictionary.
    Handles environment-specific loading (prod/dev) if needed.
    """
    global CONFIG, ENV_INFO, CONFIG_FILE

    ENV_INFO = load_backend_env(BASE_DIR)
    CONFIG_FILE = get_bot_config_file(BASE_DIR, explicit=ENV_INFO["env_name"])
    env_info = ENV_INFO
    logger.info(f"[Config] Loaded env mode={env_info['env_name']} files={env_info['loaded_files']}")
    logger.info(f"[Config] Using bot config file: {CONFIG_FILE}")
    
    # Core Environment Variables
    env_keys = [
        'DISCORD_BOT_TOKEN',
        'NOTION_TOKEN',
        'TRACK_JO_DB_ID',
        'SUBMISSIONS_DB_ID',
        'NOTION_ASSIGNMENTS_DB',
        'CURRENT_COHORT',
        'COHORT_START_DATE',
        'COHORT_END_DATE',
        'TRACK_APPLICATION_DB_ID',
        'GROUP_DB_ID',
        'ADMIN_DISCORD_ID',
        'GEMINI_API_KEY',
        'NOTION_PAGE_ID',
        'ANNOUNCEMENT_DB_ID',
        'ANNOUNCEMENT_ENABLED'
    ]
    
    missing_keys = []
    for key in env_keys:
        value = os.getenv(key)
        if value is None:
            missing_keys.append(key)
        CONFIG[key] = value

    if missing_keys:
        logger.warning(f"⚠️ Missing environment variables: {', '.join(missing_keys)}")
    else:
        logger.info("✅ Environment variables loaded successfully.")

def load_bot_config() -> Dict[str, Any]:
    """
    Load dynamic configuration from the environment-specific bot config file.
    Returns default structure if file prevents errors.
    """
    global BOT_CONFIG
    
    if not os.path.exists(CONFIG_FILE):
        logger.warning(f"⚠️ {CONFIG_FILE} not found. Using empty default.")
        BOT_CONFIG = {}
        return BOT_CONFIG

    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            BOT_CONFIG.clear()
            BOT_CONFIG.update(data)
            # logger.info("✅ bot_config.json loaded.")
    except json.JSONDecodeError as e:
        logger.error(f"❌ Failed to parse bot_config.json: {e}")
        BOT_CONFIG.clear()
    except Exception as e:
        logger.error(f"❌ Error loading bot_config.json: {e}")
        BOT_CONFIG.clear()
        
    return BOT_CONFIG

def save_bot_config(new_config: Dict[str, Any]) -> bool:
    """
    Save the provided configuration dictionary to the active bot config file.
    """
    global BOT_CONFIG
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, indent=4, ensure_ascii=False)
        BOT_CONFIG = new_config # Update in-memory
        logger.info("✅ bot_config.json saved.")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to save bot_config.json: {e}")
        return False

def update_bot_config(mutator: Callable[[Dict[str, Any]], None]) -> bool:
    """
    Atomically read-modify-write the active bot config file.
    """
    with _BOT_CONFIG_LOCK:
        current = deepcopy(load_bot_config() or {})
        mutator(current)
        return save_bot_config(current)

def merge_discord_runtime_resources(
    cohort_label: str,
    payload: Dict[str, Any],
    *,
    guild_id: Optional[Union[str, int]] = None,
) -> bool:
    """
    Persist generated Discord resources such as roles/channels/categories.
    Secrets stay in .env, generated runtime IDs stay in bot_config.*.json.
    """
    cohort_key = str(cohort_label or '').strip()
    if not cohort_key:
        return False

    timestamp = datetime.now(timezone.utc).isoformat()

    def _deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> None:
        for key, value in source.items():
            if isinstance(value, dict):
                existing = target.get(key)
                if not isinstance(existing, dict):
                    existing = {}
                    target[key] = existing
                _deep_merge(existing, value)
            else:
                target[key] = value

    def _mutate(config: Dict[str, Any]) -> None:
        resources = config.setdefault('discord_runtime_resources', {})
        cohort_bucket = resources.setdefault(cohort_key, {
            'guildId': None,
            'tracks': {},
            'updatedAt': timestamp,
        })
        if guild_id is not None:
            cohort_bucket['guildId'] = str(guild_id)
        _deep_merge(cohort_bucket, payload)
        cohort_bucket['updatedAt'] = timestamp

    return update_bot_config(_mutate)

def is_notification_enabled() -> bool:
    """
    Check if global notifications are enabled in bot_config.json.
    Default: True (if setting missing)
    """
    # Ensure fresh config is loaded (optional, depending on performance needs)
    # For now, relying on in-memory or reloading if critical
    # reload_bot_config() # Uncomment if realtime check is needed every time
    
    # Reloading specifically for this check as it's often changed via Dashboard
    load_bot_config() 
    return BOT_CONFIG.get('notifications_enabled', True)

def is_cohort_started() -> bool:
    """
    Check if current date is on or after COHORT_START_DATE.
    Returns False if cohort hasn't started yet (no user notifications should be sent).
    """
    from datetime import datetime, timezone, timedelta
    
    start_date_str = CONFIG.get('COHORT_START_DATE')
    if not start_date_str:
        logger.warning("COHORT_START_DATE not set. Assuming cohort has started.")
        return True
    
    try:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        # Get current date in KST
        kst = timezone(timedelta(hours=9))
        today = datetime.now(kst).date()
        
        if today < start_date:
            logger.info(f"[Cohort Check] Not started yet. Start: {start_date}, Today: {today}")
            return False
        return True
    except ValueError as e:
        logger.error(f"Invalid COHORT_START_DATE format: {e}")
        return True  # Default to allowing notifications on error

def is_test_mode() -> bool:
    """
    Check if test mode is enabled.
    When enabled, only admin_ids receive notifications instead of all users.
    """
    load_bot_config()
    return BOT_CONFIG.get('test_mode', False)

def get_admin_ids() -> list:
    """
    Get list of admin Discord IDs from bot_config.json.
    """
    load_bot_config()
    return BOT_CONFIG.get('admin_ids', [])

def get_channel_ids() -> Dict[str, Union[int, None]]:
    """
    Retrieve Discord Channel IDs from configuration.
    Returns a dictionary of Channel Name -> ID (int or None).
    """
    channels = BOT_CONFIG.get('discord_channels', {})
    
    # Convert config keys to what bot.py likely expects/uses
    # or return raw dict - The request asked for a dict return.
    
    # Ensure values are int if present
    processed_channels = {}
    for k, v in channels.items():
        try:
            processed_channels[k] = int(v) if v else None
        except (ValueError, TypeError):
            processed_channels[k] = None
            
    return processed_channels

def get_holiday_dates() -> Dict[str, str]:
    """
    Return holiday start and end dates.
    Returns: {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'}
    """
    holiday_settings = BOT_CONFIG.get('holiday_settings', {})
    return {
        'start': holiday_settings.get('start', ''),
        'end': holiday_settings.get('end', '')
    }

# Initialization
load_config()
load_bot_config()
