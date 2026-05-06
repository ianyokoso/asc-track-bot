import datetime
import discord
from typing import Optional, List, Union, Dict
import logging

# Setup Logger
logger = logging.getLogger('helpers')

def calculate_week_number(start_date: str, current_date: Optional[datetime.date] = None) -> int:
    """
    Calculate the cohort week number based on the start date.
    
    Args:
        start_date (str): The start date of the cohort in 'YYYY-MM-DD' format.
        current_date (Optional[datetime.date]): The date to calculate for. Defaults to today (local).
        
    Returns:
        int: The week number (1-based). Returns 0 or negative if before start date.
    """
    try:
        start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
        if current_date is None:
            current_date = datetime.datetime.now().date()
            
        delta = current_date - start_dt
        return (delta.days // 7) + 1
    except ValueError as e:
        logger.error(f"Date format error in calculate_week_number: {e}")
        return 0

def calculate_week_sunday(start_date: str, week_num: int) -> str:
    """
    Calculate the date of Sunday for a specific week number.
    Matches logic in notion_api.py.
    
    Args:
        start_date (str): 'YYYY-MM-DD'
        week_num (int): The week number.
        
    Returns:
        str: 'YYYY-MM-DD' (Sunday's date) or None if error.
    """
    try:
        start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        # Calculate days until the first Sunday
        # 0=Mon, 6=Sun. 
        # If Mon(0) -> +6 days. If Wed(2) -> +4 days. If Sun(6) -> +0 days.
        days_to_first_sunday = 6 - start_dt.weekday()
        
        # Week N Sunday = First Sunday + (N-1)*7 days
        days_to_add = days_to_first_sunday + (week_num - 1) * 7
        target_dt = start_dt + datetime.timedelta(days=days_to_add)
        return target_dt.strftime('%Y-%m-%d')
    except Exception as e:
        logger.error(f"Error in calculate_week_sunday: {e}")
        return ""

def calculate_first_weekday_on_or_after(start_date: str, target_weekday: int) -> str:
    """
    Calculate the first target weekday on or after the given start date.

    Args:
        start_date (str): 'YYYY-MM-DD'
        target_weekday (int): Monday=0 ... Sunday=6

    Returns:
        str: 'YYYY-MM-DD' or empty string on error.
    """
    try:
        start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        days_until_target = (target_weekday - start_dt.weekday()) % 7
        target_dt = start_dt + datetime.timedelta(days=days_until_target)
        return target_dt.strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"Error in calculate_first_weekday_on_or_after: {e}")
        return ""

def calculate_app_dev_due_date(start_date: str, week_num: int) -> str:
    """
    Calculate the App Development assignment due date for a given week.

    App Development week 1 is due on the Wednesday of the week after the
    cohort's first Sunday. Subsequent weeks are due every 7 days.

    Args:
        start_date (str): 'YYYY-MM-DD'
        week_num (int): 1-based App Dev week number

    Returns:
        str: 'YYYY-MM-DD' or empty string on error.
    """
    try:
        if week_num < 1:
            return ""

        first_sunday_str = calculate_first_weekday_on_or_after(start_date, 6)
        if not first_sunday_str:
            return ""

        first_sunday_dt = datetime.datetime.strptime(first_sunday_str, "%Y-%m-%d")
        due_dt = first_sunday_dt + datetime.timedelta(days=3 + ((week_num - 1) * 7))
        return due_dt.strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"Error in calculate_app_dev_due_date: {e}")
        return ""

def calculate_self_inquiry_due_date(start_date: str, week_num: int) -> str:
    """
    Calculate the Self-Inquiry (나 탐구) assignment due date for a given week.

    Self-Inquiry is due on the Saturday of each cohort week
    (i.e., the day before the weekly Sunday deadline).

    Args:
        start_date (str): 'YYYY-MM-DD'
        week_num (int): 1-based cohort week number

    Returns:
        str: 'YYYY-MM-DD' or empty string on error.
    """
    try:
        if week_num < 1:
            return ""
        sunday_str = calculate_week_sunday(start_date, week_num)
        if not sunday_str:
            return ""
        sunday_dt = datetime.datetime.strptime(sunday_str, "%Y-%m-%d")
        return (sunday_dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"Error in calculate_self_inquiry_due_date: {e}")
        return ""


def calculate_app_dev_week_number(start_date: str, reference_date: Optional[datetime.date] = None) -> int:
    """
    Calculate the App Development week number from the due-date sequence.

    Dates before the first App Dev due Wednesday return 0.
    """
    try:
        first_due_str = calculate_app_dev_due_date(start_date, 1)
        if not first_due_str:
            return 0

        first_due = datetime.datetime.strptime(first_due_str, "%Y-%m-%d").date()
        if reference_date is None:
            reference_date = datetime.datetime.now().date()

        if reference_date < first_due:
            return 0

        delta = reference_date - first_due
        return (delta.days // 7) + 1
    except Exception as e:
        logger.error(f"Error in calculate_app_dev_week_number: {e}")
        return 0

async def safe_send_dm(user: Union[discord.User, discord.Member], message: str, embed: Optional[discord.Embed] = None) -> bool:
    """
    Safely send a DM to a user, handling forbidden or other errors.
    
    Args:
        user: The discord.User or Member object.
        message (str): The message string.
        embed (Optional[discord.Embed]): Optional embed to attach.
        
    Returns:
        bool: True if successful, False otherwise.
    """
    if not user:
        return False
        
    try:
        await user.send(content=message, embed=embed)
        return True
    except discord.Forbidden:
        logger.warning(f"Draft DM failed: Forbidden (User {user.id} blocked DMs).")
        return False
    except Exception as e:
        logger.error(f"Failed to send DM to {user.id}: {e}")
        return False

def format_track_name(track: str) -> str:
    """
    Normalize track names by removing suffixes like " 트랙".
    
    Args:
        track (str): Raw track name.
        
    Returns:
        str: Normalized track name.
    """
    if not track:
        return ""
    return track.replace(" 트랙", "").strip()

def is_weekday(date_obj: Optional[datetime.date] = None) -> bool:
    """
    Check if the given date is a weekday (Monday to Friday).
    
    Args:
        date_obj (Optional[datetime.date]): Date to check. Defaults to today.
        
    Returns:
        bool: True if Mon-Fri, False if Sat-Sun.
    """
    if date_obj is None:
        date_obj = datetime.datetime.now().date()
    # 0 = Monday, 4 = Friday, 5 = Saturday, 6 = Sunday
    return date_obj.weekday() < 5

def is_in_holiday(date_str: str, holiday_start: str, holiday_end: str) -> bool:
    """
    Check if a date string falls within the holiday period (inclusive).
    
    Args:
        date_str (str): Date to check 'YYYY-MM-DD'.
        holiday_start (str): Start date 'YYYY-MM-DD'.
        holiday_end (str): End date 'YYYY-MM-DD'.
        
    Returns:
        bool: True if in holiday, False otherwise.
    """
    if not holiday_start or not holiday_end:
        return False
        
    return holiday_start <= date_str <= holiday_end

def format_submission_status(missing_tracks: List[str]) -> str:
    """
    Format a list of missing tracks into a string.
    
    Args:
        missing_tracks (List[str]): List of track names.
        
    Returns:
        str: Formatted string like "[빌더], [세일즈]".
    """
    if not missing_tracks:
        return ""
        
    # Format each track name
    formatted = [f"[{format_track_name(t)}]" for t in missing_tracks]
    return ", ".join(formatted)
