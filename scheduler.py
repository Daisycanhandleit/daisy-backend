import os
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logger = logging.getLogger(__name__)


def deserialize_datetime(dt_str):
    """Convert ISO string back to datetime object"""
    if isinstance(dt_str, str):
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    return dt_str

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Import WhatsApp functions
from whatsapp import (
    send_reminder_message, send_follow_up_message, is_twilio_configured,
    send_team_reminder_message, send_team_reminder_progress, send_whatsapp_message,
    send_smart_reminder, send_smart_followup
)

# Scheduler instance
scheduler = None


def serialize_datetime(dt):
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt


async def check_and_send_reminders():
    """
    SMART REMINDER SYSTEM - Send reminders with interactive completion options.
    Uses the new format with Done/Later/Skip buttons instead of plain text.
    
    Note: Reminders older than 60 days will be auto-skipped to avoid confusion.
    """
    if not is_twilio_configured():
        logger.warning("Twilio not configured, skipping reminder check")
        return
    
    now = datetime.now(timezone.utc)
    now_str = serialize_datetime(now)
    
    # Calculate cutoff date (60 days ago) - reminders older than this will be skipped
    max_reminder_age_days = 60
    cutoff_date = now - timedelta(days=max_reminder_age_days)
    
    logger.info(f"Checking for reminders to send at {now_str}")
    
    # Find all pending reminders where scheduled_time has passed
    pending_reminders = await db.reminders.find({
        "status": "pending",
        "scheduled_time": {"$lte": now_str}
    }, {"_id": 0}).to_list(100)
    
    logger.info(f"Found {len(pending_reminders)} reminders to send")
    
    for reminder in pending_reminders:
        try:
            # Check if reminder is too old (created more than 60 days ago)
            created_at = reminder.get('created_at')
            if created_at:
                try:
                    if isinstance(created_at, str):
                        created_dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    else:
                        created_dt = created_at
                    
                    if created_dt < cutoff_date:
                        # Auto-expire very old reminders
                        logger.info(f"Auto-expiring old reminder {reminder['id']} (created {(now - created_dt).days} days ago)")
                        await db.reminders.update_one(
                            {"id": reminder['id']},
                            {"$set": {
                                "status": "expired",
                                "expired_reason": f"Auto-expired: created more than {max_reminder_age_days} days ago",
                                "updated_at": now_str
                            }}
                        )
                        continue
                except Exception as e:
                    logger.warning(f"Could not parse created_at for reminder {reminder['id']}: {e}")
            
            # Get creator info for the reminder
            creator = await db.users.find_one({"id": reminder['creator_id']}, {"_id": 0})
            if not creator:
                # Try whatsapp_users for WhatsApp-created reminders
                creator = await db.whatsapp_users.find_one(
                    {"phone": reminder.get('creator_phone')}, {"_id": 0}
                )
            
            creator_name = creator.get('name', 'Someone') if creator else "Someone"
            recipient_relationship = reminder.get('recipient_relationship')
            
            # For self-reminders
            is_self_reminder = reminder.get('recipient_name') == 'self' or reminder.get('recipient_phone') == reminder.get('creator_phone')
            
            # Send the reminder with SMART format (interactive buttons)
            logger.info(f"Sending smart reminder {reminder['id']} to {reminder['recipient_phone']}")
            
            # Parse scheduled_time for template fallback
            scheduled_dt = None
            if reminder.get('scheduled_time'):
                try:
                    sched_time = reminder['scheduled_time']
                    if isinstance(sched_time, str):
                        scheduled_dt = datetime.fromisoformat(sched_time.replace('Z', '+00:00'))
                    else:
                        scheduled_dt = sched_time
                except Exception:
                    scheduled_dt = now
            
            # Parse created_at for context
            created_at = None
            if reminder.get('created_at'):
                try:
                    created_time = reminder['created_at']
                    if isinstance(created_time, str):
                        created_at = datetime.fromisoformat(created_time.replace('Z', '+00:00'))
                    else:
                        created_at = created_time
                except Exception:
                    pass
            
            # Use the new send_smart_reminder from whatsapp.py with clickable buttons
            message_sid = await send_smart_reminder(
                to_phone=reminder['recipient_phone'],
                message=reminder['message'],
                requester_name=None if is_self_reminder else creator_name,
                recipient_relationship=recipient_relationship,
                reminder_id=reminder['id'],
                is_self_reminder=is_self_reminder,
                scheduled_time=scheduled_dt,
                created_at=created_at
            )
            
            if message_sid:
                # Update reminder status
                await db.reminders.update_one(
                    {"id": reminder['id']},
                    {"$set": {
                        "status": "sent",
                        "last_sent_at": now_str,
                        "follow_up_count": 0  # Reset follow-up count
                    }}
                )
                logger.info(f"Smart reminder {reminder['id']} sent successfully")
                
                # Handle recurring reminders - create next occurrence
                if reminder.get('recurrence') and reminder['recurrence'] != 'once':
                    await schedule_next_occurrence(reminder)
            else:
                logger.error(f"Failed to send reminder {reminder['id']}")
                
        except Exception as e:
            logger.error(f"Error sending reminder {reminder['id']}: {e}")


async def check_and_send_team_reminders():
    """Check for pending team reminders and send them"""
    if not is_twilio_configured():
        return
    
    now = datetime.now(timezone.utc)
    now_str = serialize_datetime(now)
    
    # Find pending team reminders where scheduled_time has passed
    pending_team_reminders = await db.team_reminders.find({
        "status": "pending",
        "scheduled_time": {"$lte": now_str}
    }, {"_id": 0}).to_list(50)
    
    logger.info(f"Found {len(pending_team_reminders)} team reminders to send")
    
    for team_reminder in pending_team_reminders:
        try:
            # Get creator name
            creator = await db.users.find_one({"id": team_reminder['creator_id']}, {"_id": 0})
            if not creator:
                # Try whatsapp_users
                creator = await db.whatsapp_users.find_one({"phone": team_reminder['creator_phone']}, {"_id": 0})
            creator_name = creator.get('name', 'Someone') if creator else 'Someone'
            
            # Get all pending acknowledgments for this reminder
            pending_acks = await db.team_reminder_acks.find({
                "team_reminder_id": team_reminder['id'],
                "status": "pending"
            }, {"_id": 0}).to_list(500)
            
            sent_count = 0
            for ack in pending_acks:
                try:
                    message_sid = await send_team_reminder_message(
                        ack['member_phone'],
                        team_reminder['team_name'],
                        team_reminder['message'],
                        creator_name
                    )
                    
                    if message_sid:
                        await db.team_reminder_acks.update_one(
                            {"id": ack['id']},
                            {"$set": {
                                "status": "sent",
                                "last_sent_at": now_str
                            }}
                        )
                        sent_count += 1
                except Exception as e:
                    logger.error(f"Error sending team reminder to {ack['member_phone']}: {e}")
            
            # Update team reminder status
            await db.team_reminders.update_one(
                {"id": team_reminder['id']},
                {"$set": {
                    "status": "in_progress",
                    "updated_at": now_str
                }}
            )
            
            logger.info(f"Team reminder {team_reminder['id']} sent to {sent_count} members")
            
        except Exception as e:
            logger.error(f"Error processing team reminder {team_reminder['id']}: {e}")


async def check_and_send_team_followups():
    """Check for team reminders that need follow-ups (persist until all acknowledge)"""
    if not is_twilio_configured():
        return
    
    now = datetime.now(timezone.utc)
    now_str = serialize_datetime(now)
    fifteen_min_ago = serialize_datetime(now - timedelta(minutes=15))
    
    # Find in-progress team reminders with persist_until_all_acknowledge
    active_team_reminders = await db.team_reminders.find({
        "status": "in_progress",
        "persist_until_all_acknowledge": True
    }, {"_id": 0}).to_list(50)
    
    for team_reminder in active_team_reminders:
        try:
            # Find unacknowledged members who were sent reminder > 15 min ago
            pending_acks = await db.team_reminder_acks.find({
                "team_reminder_id": team_reminder['id'],
                "status": "sent",
                "last_sent_at": {"$lte": fifteen_min_ago},
                "follow_up_count": {"$lt": 10}  # Allow more follow-ups for team reminders
            }, {"_id": 0}).to_list(100)
            
            for ack in pending_acks:
                try:
                    follow_up_count = ack.get('follow_up_count', 0) + 1
                    
                    # Send follow-up
                    from whatsapp import send_follow_up_message
                    message_sid = await send_follow_up_message(
                        ack['member_phone'],
                        f"[Team: {team_reminder['team_name']}] {team_reminder['message']}",
                        follow_up_count
                    )
                    
                    if message_sid:
                        await db.team_reminder_acks.update_one(
                            {"id": ack['id']},
                            {"$set": {
                                "follow_up_count": follow_up_count,
                                "last_sent_at": now_str
                            }}
                        )
                        logger.info(f"Team follow-up #{follow_up_count} sent to {ack['member_phone']}")
                        
                except Exception as e:
                    logger.error(f"Error sending team follow-up to {ack['member_phone']}: {e}")
            
            # Check if all acknowledged - update status and notify creator
            total_acks = await db.team_reminder_acks.count_documents({
                "team_reminder_id": team_reminder['id']
            })
            acknowledged_count = await db.team_reminder_acks.count_documents({
                "team_reminder_id": team_reminder['id'],
                "status": "acknowledged"
            })
            
            # Update counts
            await db.team_reminders.update_one(
                {"id": team_reminder['id']},
                {"$set": {"acknowledged_count": acknowledged_count}}
            )
            
            # If all acknowledged, mark as completed
            if acknowledged_count >= total_acks and total_acks > 0:
                await db.team_reminders.update_one(
                    {"id": team_reminder['id']},
                    {"$set": {"status": "completed", "updated_at": now_str}}
                )
                
                # Notify creator
                await send_team_reminder_progress(
                    team_reminder['creator_phone'],
                    team_reminder['team_name'],
                    team_reminder['message'],
                    acknowledged_count,
                    total_acks
                )
                logger.info(f"Team reminder {team_reminder['id']} completed - all {total_acks} acknowledged")
                
        except Exception as e:
            logger.error(f"Error checking team follow-ups for {team_reminder['id']}: {e}")


async def check_and_send_followups():
    """
    SMART FOLLOW-UP SYSTEM - Max 2 follow-ups with smart intervals.
    - 1st follow-up: After 10 minutes
    - 2nd follow-up: After 30 minutes (total 40 mins from initial)
    - After 2 follow-ups: Mark as missed and notify creator
    """
    if not is_twilio_configured():
        return
    
    now = datetime.now(timezone.utc)
    now_str = serialize_datetime(now)
    
    # Find reminders that were sent but not acknowledged
    sent_reminders = await db.reminders.find({
        "status": "sent",
        "follow_up_count": {"$lt": 2}  # Max 2 follow-ups (changed from 3)
    }, {"_id": 0}).to_list(50)
    
    for reminder in sent_reminders:
        try:
            follow_up_count = reminder.get('follow_up_count', 0)
            last_sent_at = reminder.get('last_sent_at')
            
            if not last_sent_at:
                continue
            
            last_sent_dt = deserialize_datetime(last_sent_at)
            minutes_since = (now - last_sent_dt).total_seconds() / 60
            
            # Smart follow-up intervals: [10 min, 30 min]
            follow_up_intervals = reminder.get('follow_up_intervals', [10, 30])
            
            # Determine if it's time for a follow-up
            required_interval = follow_up_intervals[follow_up_count] if follow_up_count < len(follow_up_intervals) else 30
            
            if minutes_since < required_interval:
                continue  # Not time yet
            
            new_follow_up_count = follow_up_count + 1
            
            # Get creator info for context
            creator = await db.users.find_one({"id": reminder['creator_id']}, {"_id": 0})
            if not creator:
                creator = await db.whatsapp_users.find_one(
                    {"phone": reminder.get('creator_phone')}, {"_id": 0}
                )
            creator_name = creator.get('name', 'Someone') if creator else None
            
            is_self_reminder = reminder.get('recipient_name') == 'self'
            
            logger.info(f"Sending gentle follow-up #{new_follow_up_count} for reminder {reminder['id']}")
            
            # Use the new smart follow-up with buttons from whatsapp.py
            message_sid = await send_smart_followup(
                to_phone=reminder['recipient_phone'],
                message=reminder['message'],
                follow_up_count=new_follow_up_count,
                requester_name=None if is_self_reminder else creator_name,
                reminder_id=reminder['id']
            )
            
            if message_sid:
                await db.reminders.update_one(
                    {"id": reminder['id']},
                    {"$set": {
                        "follow_up_count": new_follow_up_count,
                        "last_sent_at": now_str,
                        "last_follow_up_at": now_str
                    }}
                )
                
                # If this was the 2nd (final) follow-up, notify the creator
                if new_follow_up_count >= 2 and not is_self_reminder:
                    creator_phone = reminder.get('creator_phone')
                    if creator_phone and not reminder.get('creator_notified_of_pending'):
                        recipient_name = reminder.get('recipient_name', reminder['recipient_phone'])
                        recipient_relationship = reminder.get('recipient_relationship')
                        
                        await notify_creator_of_pending(
                            creator_phone=creator_phone,
                            recipient_name=recipient_name,
                            reminder_message=reminder['message'],
                            recipient_relationship=recipient_relationship
                        )
                        
                        # Mark as notified
                        await db.reminders.update_one(
                            {"id": reminder['id']},
                            {"$set": {"creator_notified_of_pending": True}}
                        )
                
        except Exception as e:
            logger.error(f"Error sending follow-up for {reminder['id']}: {e}")
    
    # Also check for reminders that have exhausted follow-ups - mark as missed
    expired_reminders = await db.reminders.find({
        "status": "sent",
        "follow_up_count": {"$gte": 2}
    }, {"_id": 0}).to_list(50)
    
    for reminder in expired_reminders:
        try:
            last_sent_at = reminder.get('last_sent_at')
            if not last_sent_at:
                continue
            
            last_sent_dt = deserialize_datetime(last_sent_at)
            minutes_since = (now - last_sent_dt).total_seconds() / 60
            
            # If 30+ minutes since final follow-up, mark as "missed" (for evening wrapup)
            if minutes_since >= 30:
                await db.reminders.update_one(
                    {"id": reminder['id']},
                    {"$set": {
                        "status": "missed",
                        "updated_at": now_str
                    }}
                )
                logger.info(f"Reminder {reminder['id']} marked as missed (no response after follow-ups)")
                
        except Exception as e:
            logger.error(f"Error marking reminder as missed: {e}")


async def schedule_next_occurrence(reminder):
    """Create the next occurrence for a recurring reminder"""
    try:
        current_time = datetime.fromisoformat(reminder['scheduled_time'].replace('Z', '+00:00'))
        
        if reminder['recurrence'] == 'daily':
            next_time = current_time + timedelta(days=1)
        elif reminder['recurrence'] == 'weekly':
            next_time = current_time + timedelta(weeks=1)
        elif reminder['recurrence'] == 'monthly':
            next_time = current_time + timedelta(days=30)
        else:
            return
        
        # Check if we're past the end date
        if reminder.get('end_date'):
            end_date = datetime.fromisoformat(reminder['end_date'].replace('Z', '+00:00'))
            if next_time > end_date:
                return
        
        # Update the reminder with next scheduled time and reset status
        await db.reminders.update_one(
            {"id": reminder['id']},
            {"$set": {
                "scheduled_time": serialize_datetime(next_time),
                "status": "pending"
            }}
        )
        
        logger.info(f"Scheduled next occurrence of {reminder['id']} for {next_time}")
        
    except Exception as e:
        logger.error(f"Error scheduling next occurrence: {e}")


async def check_and_send_multi_time_reminders():
    """Check for multi-time reminders that need to be sent at their scheduled times"""
    if not is_twilio_configured():
        return
    
    now = datetime.now(timezone.utc)
    now_str = serialize_datetime(now)
    
    # Find all active multi-time reminders
    active_reminders = await db.multi_time_reminders.find({
        "status": "active"
    }, {"_id": 0}).to_list(100)
    
    logger.info(f"Checking {len(active_reminders)} active multi-time reminders")
    
    for reminder in active_reminders:
        try:
            reminder_times = reminder.get('reminder_times', [])
            updated = False
            
            for rt in reminder_times:
                # Check if this time should be sent (time has passed and status is pending)
                if rt.get('status') == 'pending':
                    scheduled_time = rt.get('time')
                    if scheduled_time and scheduled_time <= now_str:
                        # Time to send this reminder
                        creator_name = reminder.get('creator_name', 'Someone')
                        message = reminder.get('message', '')
                        recipient_phone = reminder.get('recipient_phone')
                        label = rt.get('label', '')
                        
                        # Build the message
                        if label == 'deadline':
                            reminder_text = f"🌼 FINAL REMINDER from {creator_name}:\n\n{message}\n\n⏰ This is the deadline! Please reply \"Done\" when completed.\n\n- Daisy"
                        elif label == 'immediate':
                            reminder_text = f"🌼 Reminder from {creator_name}:\n\n{message}\n\nPlease reply \"Done\" or \"Sure\" when completed.\n\n- Daisy"
                        else:
                            reminder_text = f"🌼 Reminder from {creator_name}:\n\n{message}\n\n({label})\n\nPlease reply \"Done\" or \"Sure\" when completed.\n\n- Daisy"
                        
                        message_sid = await send_whatsapp_message(recipient_phone, reminder_text)
                        
                        if message_sid:
                            rt['status'] = 'sent'
                            rt['sent_at'] = now_str
                            updated = True
                            logger.info(f"Sent multi-time reminder to {recipient_phone} (label: {label})")
            
            if updated:
                # Update the reminder times in database
                await db.multi_time_reminders.update_one(
                    {"id": reminder['id']},
                    {"$set": {
                        "reminder_times": reminder_times,
                        "updated_at": now_str
                    }}
                )
                
        except Exception as e:
            logger.error(f"Error processing multi-time reminder {reminder['id']}: {e}")


async def check_and_send_multi_time_followups():
    """Check for multi-time reminders that need follow-ups (all scheduled times sent but not acknowledged)"""
    if not is_twilio_configured():
        return
    
    now = datetime.now(timezone.utc)
    now_str = serialize_datetime(now)
    
    # Find active multi-time reminders where all scheduled times have been sent
    active_reminders = await db.multi_time_reminders.find({
        "status": "active",
        "enable_followups": True
    }, {"_id": 0}).to_list(100)
    
    for reminder in active_reminders:
        try:
            reminder_times = reminder.get('reminder_times', [])
            
            # Check if all scheduled times have been sent
            all_sent = all(rt.get('status') == 'sent' for rt in reminder_times) if reminder_times else False
            
            if not all_sent:
                continue  # Still have scheduled times to send
            
            # Check if we should send a follow-up
            followup_count = reminder.get('followup_count', 0)
            max_followups = reminder.get('max_followups', 10)
            followup_interval = reminder.get('followup_interval_minutes', 30)
            last_followup = reminder.get('last_followup_at')
            
            if followup_count >= max_followups:
                continue  # Max follow-ups reached
            
            # Check time since last activity
            last_activity = last_followup
            if not last_activity:
                # Use the last sent reminder time
                sent_times = [rt.get('sent_at') for rt in reminder_times if rt.get('sent_at')]
                if sent_times:
                    last_activity = max(sent_times)
            
            if last_activity:
                last_activity_dt = datetime.fromisoformat(last_activity.replace('Z', '+00:00')) if isinstance(last_activity, str) else last_activity
                time_since = (now - last_activity_dt).total_seconds() / 60
                
                if time_since >= followup_interval:
                    # Send follow-up
                    creator_name = reminder.get('creator_name', 'Someone')
                    message = reminder.get('message', '')
                    recipient_phone = reminder.get('recipient_phone')
                    new_followup_count = followup_count + 1
                    
                    followup_text = f"🌼 Follow-up #{new_followup_count} from {creator_name}:\n\n{message}\n\nStill waiting for your confirmation. Please reply \"Done\" or \"Sure\".\n\n- Daisy"
                    
                    message_sid = await send_whatsapp_message(recipient_phone, followup_text)
                    
                    if message_sid:
                        await db.multi_time_reminders.update_one(
                            {"id": reminder['id']},
                            {"$set": {
                                "followup_count": new_followup_count,
                                "last_followup_at": now_str,
                                "updated_at": now_str
                            }}
                        )
                        logger.info(f"Sent multi-time follow-up #{new_followup_count} to {recipient_phone}")
                        
                        # Optionally notify creator about follow-up
                        if new_followup_count % 3 == 0:  # Notify every 3 follow-ups
                            creator_phone = reminder.get('creator_phone')
                            recipient_name = reminder.get('recipient_name', recipient_phone)
                            if creator_phone:
                                await send_whatsapp_message(
                                    creator_phone,
                                    f"🌼 Update: Still waiting for {recipient_name} to confirm \"{message}\". Sent {new_followup_count} follow-ups so far."
                                )
                        
        except Exception as e:
            logger.error(f"Error processing multi-time follow-up for {reminder['id']}: {e}")


def start_scheduler():
    """Start the background scheduler"""
    global scheduler
    
    if scheduler is not None:
        logger.info("Scheduler already running")
        return
    
    scheduler = AsyncIOScheduler()
    
    # Check for individual reminders every 30 seconds
    scheduler.add_job(
        check_and_send_reminders,
        IntervalTrigger(seconds=30),
        id='reminder_checker',
        replace_existing=True
    )
    
    # Check for individual follow-ups every 5 minutes
    scheduler.add_job(
        check_and_send_followups,
        IntervalTrigger(minutes=5),
        id='followup_checker',
        replace_existing=True
    )
    
    # Check for team reminders every 30 seconds
    scheduler.add_job(
        check_and_send_team_reminders,
        IntervalTrigger(seconds=30),
        id='team_reminder_checker',
        replace_existing=True
    )
    
    # Check for team follow-ups every 5 minutes
    scheduler.add_job(
        check_and_send_team_followups,
        IntervalTrigger(minutes=5),
        id='team_followup_checker',
        replace_existing=True
    )
    
    # Check for multi-time reminders every 30 seconds
    scheduler.add_job(
        check_and_send_multi_time_reminders,
        IntervalTrigger(seconds=30),
        id='multi_time_reminder_checker',
        replace_existing=True
    )
    
    # Check for multi-time follow-ups every 5 minutes
    scheduler.add_job(
        check_and_send_multi_time_followups,
        IntervalTrigger(minutes=5),
        id='multi_time_followup_checker',
        replace_existing=True
    )
    
    # Check for habit reminders every minute
    scheduler.add_job(
        check_and_send_habit_reminders,
        IntervalTrigger(minutes=1),
        id='habit_reminder_checker',
        replace_existing=True
    )
    
    # Check for habit follow-ups every 5 minutes
    scheduler.add_job(
        check_and_send_habit_followups,
        IntervalTrigger(minutes=5),
        id='habit_followup_checker',
        replace_existing=True
    )
    
    # Generate habit logs for the day (run once at midnight and at startup)
    scheduler.add_job(
        generate_daily_habit_logs,
        IntervalTrigger(hours=1),
        id='habit_log_generator',
        replace_existing=True
    )
    
    # Morning Agenda - check every minute to send at each user's preferred time
    scheduler.add_job(
        send_morning_agenda,
        IntervalTrigger(minutes=1),
        id='morning_agenda_sender',
        replace_existing=True
    )
    
    # Evening Wrapup - check every minute to send at each user's preferred time
    scheduler.add_job(
        send_evening_wrapup,
        IntervalTrigger(minutes=1),
        id='evening_wrapup_sender',
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("Reminder scheduler started - Smart Reminder System with Morning Agenda & Evening Wrapup enabled")


# ============== SMART REMINDER SYSTEM ==============

async def send_morning_agenda():
    """
    Send personalized morning agenda to all active users.
    Shows all tasks/reminders scheduled for today in ONE message.
    """
    logger.info("Running morning agenda job...")
    
    now = datetime.now(timezone.utc)
    today_str = now.strftime('%Y-%m-%d')
    
    # Get all active users who have agenda enabled
    users = await db.whatsapp_users.find({
        "user_type": "active_user",
        "agenda_enabled": {"$ne": False}
    }, {"_id": 0}).to_list(10000)
    
    for user in users:
        try:
            user_phone = user.get('phone')
            user_name = user.get('name', 'there')
            user_tz = user.get('timezone', 'UTC')
            agenda_time = user.get('morning_agenda_time', '07:00')
            
            # Check if it's time for this user's morning agenda (within 5 min window)
            try:
                from pytz import timezone as pytz_timezone
                user_timezone = pytz_timezone(user_tz)
                user_now = now.astimezone(user_timezone)
                
                # Parse agenda time
                agenda_hour, agenda_min = map(int, agenda_time.split(':'))
                current_hour, current_min = user_now.hour, user_now.minute
                
                # Check if within 2 minute window of agenda time
                if not (current_hour == agenda_hour and abs(current_min - agenda_min) <= 2):
                    continue
            except Exception as e:
                logger.error(f"Timezone error for {user_phone}: {e}")
                continue
            
            # Get today's reminders for this user (both self and for others)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)
            
            # Self reminders
            self_reminders = await db.reminders.find({
                "creator_id": {"$regex": user_phone},
                "recipient_phone": user_phone,
                "status": {"$in": ["pending", "sent"]},
                "scheduled_time": {
                    "$gte": serialize_datetime(today_start),
                    "$lt": serialize_datetime(today_end)
                }
            }, {"_id": 0}).sort("scheduled_time", 1).to_list(50)
            
            # Reminders for others
            others_reminders = await db.reminders.find({
                "creator_phone": user_phone,
                "recipient_phone": {"$ne": user_phone},
                "status": {"$in": ["pending", "sent", "awaiting_consent"]},
                "scheduled_time": {
                    "$gte": serialize_datetime(today_start),
                    "$lt": serialize_datetime(today_end)
                }
            }, {"_id": 0}).sort("scheduled_time", 1).to_list(50)
            
            # Get today's habits
            habits_due = await db.habit_logs.find({
                "user_phone": user_phone,
                "scheduled_date": today_str,
                "status": "pending"
            }, {"_id": 0}).to_list(20)
            
            # Build agenda message
            if not self_reminders and not others_reminders and not habits_due:
                # No tasks today
                agenda_msg = f"""☀️ *Good morning, {user_name}!*

You have a clear day ahead - no reminders or tasks scheduled.

Enjoy your day! If anything comes up, just let me know 💛

— Daisy"""
            else:
                agenda_msg = f"""☀️ *Good morning, {user_name}!*

Here's what's happening today:\n"""
                
                # Self reminders
                if self_reminders:
                    agenda_msg += "\n*Your Reminders:*\n"
                    for rem in self_reminders:
                        time_str = deserialize_datetime(rem['scheduled_time']).strftime('%I:%M %p')
                        agenda_msg += f"• {rem['message']} — {time_str}\n"
                
                # Reminders for others
                if others_reminders:
                    agenda_msg += "\n*Caring for Others:*\n"
                    for rem in others_reminders:
                        time_str = deserialize_datetime(rem['scheduled_time']).strftime('%I:%M %p')
                        name = rem.get('recipient_name') or rem.get('recipient_relationship', 'someone')
                        agenda_msg += f"• {name}'s reminder: {rem['message'][:30]}... — {time_str}\n"
                
                # Habits
                if habits_due:
                    agenda_msg += "\n*Today's Habits:*\n"
                    for log in habits_due:
                        habit = await db.habits.find_one({"id": log['habit_id']}, {"_id": 0})
                        if habit:
                            agenda_msg += f"• {habit['name']} — {habit.get('time', 'anytime')}\n"
                
                agenda_msg += "\nI'll remind you when it's time. Have a wonderful day! 💛\n\n— Daisy"
            
            # Send the agenda
            if is_twilio_configured():
                await send_whatsapp_message(user_phone, agenda_msg)
                logger.info(f"Sent morning agenda to {user_phone}")
            
            # Mark reminders as included in agenda
            for rem in self_reminders + others_reminders:
                await db.reminders.update_one(
                    {"id": rem['id']},
                    {"$set": {"included_in_morning_agenda": True}}
                )
                
        except Exception as e:
            logger.error(f"Error sending morning agenda to {user.get('phone')}: {e}")


async def send_evening_wrapup():
    """
    Send personalized evening summary to all active users.
    Shows completed, missed, and pending tasks from today.
    """
    logger.info("Running evening wrapup job...")
    
    now = datetime.now(timezone.utc)
    today_str = now.strftime('%Y-%m-%d')
    
    users = await db.whatsapp_users.find({
        "user_type": "active_user",
        "agenda_enabled": {"$ne": False}
    }, {"_id": 0}).to_list(10000)
    
    for user in users:
        try:
            user_phone = user.get('phone')
            user_name = user.get('name', 'there')
            user_tz = user.get('timezone', 'UTC')
            wrapup_time = user.get('evening_wrapup_time', '21:00')
            
            # Check if it's time for this user's evening wrapup
            try:
                from pytz import timezone as pytz_timezone
                user_timezone = pytz_timezone(user_tz)
                user_now = now.astimezone(user_timezone)
                
                wrapup_hour, wrapup_min = map(int, wrapup_time.split(':'))
                current_hour, current_min = user_now.hour, user_now.minute
                
                if not (current_hour == wrapup_hour and abs(current_min - wrapup_min) <= 2):
                    continue
            except Exception as e:
                logger.error(f"Timezone error for {user_phone}: {e}")
                continue
            
            # Get today's reminders
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = today_start + timedelta(days=1)
            
            all_reminders = await db.reminders.find({
                "$or": [
                    {"creator_phone": user_phone},
                    {"recipient_phone": user_phone}
                ],
                "scheduled_time": {
                    "$gte": serialize_datetime(today_start),
                    "$lt": serialize_datetime(today_end)
                }
            }, {"_id": 0}).to_list(100)
            
            # Categorize
            completed = [r for r in all_reminders if r.get('status') == 'acknowledged' or r.get('completed')]
            missed = [r for r in all_reminders if r.get('status') == 'sent' and r.get('follow_up_count', 0) >= 2]
            pending = [r for r in all_reminders if r.get('status') in ['pending', 'sent'] and r not in missed]
            
            # Get habit stats
            habit_logs = await db.habit_logs.find({
                "user_phone": user_phone,
                "scheduled_date": today_str
            }, {"_id": 0}).to_list(50)
            
            habits_completed = [h for h in habit_logs if h.get('status') == 'completed']
            habits_missed = [h for h in habit_logs if h.get('status') == 'missed']
            habits_pending = [h for h in habit_logs if h.get('status') == 'pending']
            
            # Build wrapup message
            wrapup_msg = f"""🌙 *Evening Summary for {user_name}*\n"""
            
            # Completed section
            if completed or habits_completed:
                wrapup_msg += "\n✅ *Completed:*\n"
                for rem in completed[:5]:
                    wrapup_msg += f"• {rem['message'][:40]}\n"
                for h in habits_completed[:3]:
                    habit = await db.habits.find_one({"id": h['habit_id']}, {"_id": 0})
                    if habit:
                        wrapup_msg += f"• {habit['name']} (habit)\n"
            
            # Missed section
            if missed or habits_missed:
                wrapup_msg += "\n❌ *Missed:*\n"
                for rem in missed[:5]:
                    wrapup_msg += f"• {rem['message'][:40]}\n"
                for h in habits_missed[:3]:
                    habit = await db.habits.find_one({"id": h['habit_id']}, {"_id": 0})
                    if habit:
                        wrapup_msg += f"• {habit['name']} (habit)\n"
            
            # Pending/Waiting confirmation
            if pending or habits_pending:
                wrapup_msg += "\n⏳ *Pending Confirmation:*\n"
                for rem in pending[:5]:
                    name = rem.get('recipient_name') or rem.get('recipient_relationship', '')
                    if name and rem.get('recipient_phone') != user_phone:
                        wrapup_msg += f"• {name}: {rem['message'][:30]}...\n"
                    else:
                        wrapup_msg += f"• {rem['message'][:40]}\n"
            
            # Summary stats
            total_tasks = len(all_reminders) + len(habit_logs)
            completed_count = len(completed) + len(habits_completed)
            
            if total_tasks > 0:
                completion_rate = round((completed_count / total_tasks) * 100)
                wrapup_msg += f"\n📊 Today's Score: {completion_rate}% complete\n"
            
            wrapup_msg += "\nLet me know if you'd like to reschedule anything for tomorrow.\n\nRest well! 💛\n\n— Daisy"
            
            # Only send if there were any tasks
            if total_tasks > 0 and is_twilio_configured():
                await send_whatsapp_message(user_phone, wrapup_msg)
                logger.info(f"Sent evening wrapup to {user_phone}")
                
                # Mark reminders as included
                for rem in all_reminders:
                    await db.reminders.update_one(
                        {"id": rem['id']},
                        {"$set": {"included_in_evening_wrapup": True}}
                    )
                    
        except Exception as e:
            logger.error(f"Error sending evening wrapup to {user.get('phone')}: {e}")


async def send_smart_reminder_with_buttons(
    to_phone: str,
    message: str,
    requester_name: str = None,
    recipient_relationship: str = None,
    reminder_id: str = None
):
    """
    Send a reminder with interactive completion options.
    Format:
    ⏰ Reminder
    [Message]
    
    Reply:
    1️⃣ Done
    2️⃣ Remind me in 10 minutes
    3️⃣ Skip today
    """
    # Build greeting based on relationship
    greeting = ""
    if recipient_relationship:
        rel = recipient_relationship.lower()
        if rel in ['mom', 'mum', 'mother', 'mama']:
            greeting = "Hi Mom! "
        elif rel in ['dad', 'father', 'papa']:
            greeting = "Hi Dad! "
        elif rel in ['grandma', 'grandmother', 'nana']:
            greeting = "Hi Grandma! "
        elif rel in ['grandpa', 'grandfather']:
            greeting = "Hi Grandpa! "
        else:
            greeting = f"Hi {recipient_relationship.capitalize()}! "
    
    # Build the message with completion buttons
    if requester_name and requester_name != "self":
        full_message = f"""{greeting}⏰ *Reminder from {requester_name}*

{message}

{requester_name} cares about you and wants to make sure this gets done 💛

*Please reply:*
1️⃣ *Done* - Mark as completed
2️⃣ *Later* - Remind me in 10 minutes  
3️⃣ *Skip* - Skip this reminder

— Daisy"""
    else:
        full_message = f"""⏰ *Reminder*

{message}

*Reply:*
1️⃣ *Done* - Mark as completed
2️⃣ *Later* - Remind me in 10 minutes
3️⃣ *Skip* - Skip this reminder

— Daisy"""
    
    return await send_whatsapp_message(to_phone, full_message)


async def send_gentle_followup(
    to_phone: str,
    message: str,
    follow_up_count: int,
    requester_name: str = None,
    reminder_id: str = None
):
    """
    Send a gentle follow-up (max 2 times).
    After 2 follow-ups, send graceful stop message.
    """
    if follow_up_count == 1:
        # First follow-up after 10 minutes
        followup_msg = f"""🔔 *Quick check-in*

Just making sure you saw this:
_{message}_

Reply *Done* when complete, or *Later* for another reminder.

— Daisy"""
    elif follow_up_count == 2:
        # Final follow-up - graceful stop
        if requester_name:
            followup_msg = f"""💭 *Final reminder*

_{message}_

I couldn't confirm if this was completed. I'll let {requester_name} know I'm still waiting to hear from you.

Reply *Done* anytime to confirm 💛

— Daisy"""
        else:
            followup_msg = f"""💭 *Final reminder*

_{message}_

I couldn't confirm if this was completed. I'll check again tomorrow if needed.

Reply *Done* anytime to confirm 💛

— Daisy"""
    else:
        # Should not reach here, but graceful handling
        return None
    
    return await send_whatsapp_message(to_phone, followup_msg)


async def notify_creator_of_completion(
    creator_phone: str,
    recipient_name: str,
    reminder_message: str,
    recipient_relationship: str = None
):
    """Notify creator when their loved one completes a task"""
    
    name = recipient_relationship.capitalize() if recipient_relationship else recipient_name
    
    notification = f"""💛 *Great news!*

{name} confirmed: "{reminder_message[:50]}..."

Your care made a difference today! 🌼

— Daisy"""
    
    return await send_whatsapp_message(creator_phone, notification)


async def notify_creator_of_pending(
    creator_phone: str,
    recipient_name: str,
    reminder_message: str,
    recipient_relationship: str = None
):
    """Notify creator if their loved one hasn't responded after follow-ups"""
    
    name = recipient_relationship.capitalize() if recipient_relationship else recipient_name
    
    notification = f"""ℹ️ *Update on {name}*

{name} hasn't confirmed yet: "{reminder_message[:40]}..."

I've sent gentle reminders. You might want to check in directly.

— Daisy"""
    
    return await send_whatsapp_message(creator_phone, notification)


async def generate_daily_habit_logs():
    """Generate habit log entries for today for all active habits"""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    day_of_week = datetime.now(timezone.utc).strftime('%A')
    
    # Find all active habits
    active_habits = await db.habits.find({"status": "active"}, {"_id": 0}).to_list(1000)
    
    for habit in active_habits:
        try:
            # Check if habit should run today based on frequency
            should_run = False
            
            if habit['frequency'] == 'daily':
                should_run = True
            elif habit['frequency'] == 'weekly':
                # Assuming weekly means same day each week
                should_run = True  # For simplicity, run weekly habits every day user can customize
            elif habit['frequency'] == 'custom':
                custom_days = habit.get('custom_days', [])
                should_run = day_of_week in custom_days
            
            if should_run:
                # Check if log already exists for today
                existing_log = await db.habit_logs.find_one({
                    "habit_id": habit['id'],
                    "scheduled_date": today
                }, {"_id": 0})
                
                if not existing_log:
                    # Create log entry for today
                    from models import HabitLog
                    log = HabitLog(
                        habit_id=habit['id'],
                        user_id=habit['user_id'],
                        user_phone=habit['user_phone'],
                        scheduled_date=today,
                        scheduled_time=habit['time']
                    )
                    log_dict = log.model_dump()
                    log_dict['created_at'] = serialize_datetime(log_dict['created_at'])
                    log_dict['updated_at'] = serialize_datetime(log_dict['updated_at'])
                    
                    await db.habit_logs.insert_one(log_dict)
                    logger.info(f"Created habit log for {habit['name']} on {today}")
                    
        except Exception as e:
            logger.error(f"Error generating habit log for {habit['id']}: {e}")


async def check_and_send_habit_reminders():
    """Check for habit reminders that need to be sent"""
    if not is_twilio_configured():
        return
    
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    current_time = now.strftime('%H:%M')
    
    # Find habit logs that are pending and it's time to remind
    # We check within a 2-minute window to account for scheduler timing
    pending_logs = await db.habit_logs.find({
        "scheduled_date": today,
        "status": "pending",
        "reminder_sent": False
    }, {"_id": 0}).to_list(100)
    
    for log in pending_logs:
        try:
            # Get the habit
            habit = await db.habits.find_one({"id": log['habit_id']}, {"_id": 0})
            if not habit or habit['status'] != 'active':
                continue
            
            # Convert scheduled time to UTC for comparison
            # For now, simple string comparison (assumes UTC storage)
            scheduled_time = log['scheduled_time']
            
            # Check if it's time (within 2-minute window)
            # This is simplified - in production you'd want proper timezone handling
            if scheduled_time <= current_time:
                # Send the habit reminder
                streak = habit.get('current_streak', 0)
                
                if streak >= 7:
                    streak_msg = f"🔥 {streak} day streak!"
                elif streak > 0:
                    streak_msg = f"📊 {streak} day streak"
                else:
                    streak_msg = "Let's start a streak!"
                
                message = f"""🌼 Habit Reminder: **{habit['name']}**

{streak_msg}

• Reply **Done** when finished
• Reply **Snooze** for more time
• Reply **Skip** if you can't today

You've got this! 💪"""
                
                message_sid = await send_whatsapp_message(log['user_phone'], message)
                
                if message_sid:
                    await db.habit_logs.update_one(
                        {"id": log['id']},
                        {"$set": {
                            "status": "reminded",
                            "reminder_sent": True,
                            "reminder_sent_at": serialize_datetime(now),
                            "updated_at": serialize_datetime(now)
                        }}
                    )
                    logger.info(f"Sent habit reminder for {habit['name']} to {log['user_phone']}")
                    
        except Exception as e:
            logger.error(f"Error sending habit reminder for log {log['id']}: {e}")


async def check_and_send_habit_followups():
    """Check for habit reminders that need follow-ups based on intensity"""
    if not is_twilio_configured():
        return
    
    now = datetime.now(timezone.utc)
    today = now.strftime('%Y-%m-%d')
    
    # Find reminded habits that haven't been completed
    reminded_logs = await db.habit_logs.find({
        "scheduled_date": today,
        "status": "reminded"
    }, {"_id": 0}).to_list(100)
    
    for log in reminded_logs:
        try:
            habit = await db.habits.find_one({"id": log['habit_id']}, {"_id": 0})
            if not habit or habit['status'] != 'active':
                continue
            
            # Get intensity config
            from models import REMINDER_INTENSITY_CONFIG
            intensity = habit.get('reminder_intensity', 'standard')
            config = REMINDER_INTENSITY_CONFIG.get(intensity, REMINDER_INTENSITY_CONFIG['standard'])
            
            # Calculate time since last reminder/followup
            last_contact = log.get('last_followup_at') or log.get('reminder_sent_at')
            if not last_contact:
                continue
            
            last_contact_dt = datetime.fromisoformat(last_contact.replace('Z', '+00:00')) if isinstance(last_contact, str) else last_contact
            minutes_since = (now - last_contact_dt).total_seconds() / 60
            
            followup_count = log.get('followup_count', 0)
            
            # Determine if follow-up needed based on intensity
            should_followup = False
            followup_type = ""
            
            if followup_count == 0 and minutes_since >= config['first_followup']:
                should_followup = True
                followup_type = "gentle"
            elif followup_count == 1 and minutes_since >= config['second_followup']:
                should_followup = True
                followup_type = "reminder"
            elif followup_count == 2 and minutes_since >= config['final_warning']:
                should_followup = True
                followup_type = "final"
            
            if should_followup:
                # Send appropriate follow-up
                if followup_type == "gentle":
                    message = f"🌼 Gentle nudge: Have you done **{habit['name']}** yet? Reply Done when you're finished!"
                elif followup_type == "reminder":
                    message = f"🌼 Just checking in on **{habit['name']}**. Your streak is counting on you! 💪"
                else:
                    message = f"🌼 Last reminder for **{habit['name']}** today. It's okay if you need to skip - just let me know!"
                
                message_sid = await send_whatsapp_message(log['user_phone'], message)
                
                if message_sid:
                    await db.habit_logs.update_one(
                        {"id": log['id']},
                        {"$set": {
                            "followup_count": followup_count + 1,
                            "last_followup_at": serialize_datetime(now),
                            "updated_at": serialize_datetime(now)
                        }}
                    )
                    logger.info(f"Sent {followup_type} follow-up for {habit['name']}")
            
            # Mark as missed if past final warning and still no response
            if followup_count >= 3:
                await db.habit_logs.update_one(
                    {"id": log['id']},
                    {"$set": {
                        "status": "missed",
                        "updated_at": serialize_datetime(now)
                    }}
                )
                
                # Update habit missed count and reset streak
                await db.habits.update_one(
                    {"id": habit['id']},
                    {"$set": {
                        "current_streak": 0,
                        "total_missed": habit.get('total_missed', 0) + 1,
                        "updated_at": serialize_datetime(now)
                    }}
                )
                
                logger.info(f"Marked {habit['name']} as missed for {today}")
                
        except Exception as e:
            logger.error(f"Error sending habit follow-up for log {log['id']}: {e}")


def stop_scheduler():
    """Stop the background scheduler"""
    global scheduler
    if scheduler:
        scheduler.shutdown()
        scheduler = None
        logger.info("Reminder scheduler stopped")
