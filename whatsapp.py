import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

logger = logging.getLogger(__name__)


def get_twilio_client():
    """Get or create Twilio client"""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    
    if account_sid and auth_token:
        try:
            return Client(account_sid, auth_token)
        except Exception as e:
            logger.error(f"Failed to initialize Twilio client: {e}")
    return None


def is_twilio_configured() -> bool:
    """Check if Twilio credentials are configured"""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    whatsapp_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
    return all([account_sid, auth_token, whatsapp_number])


def get_messaging_service_sid() -> Optional[str]:
    """Get the Messaging Service SID for Content API"""
    return os.environ.get("TWILIO_MESSAGING_SERVICE_SID")


def get_reminder_content_sid() -> Optional[str]:
    """Get the Content SID for reminder quick reply buttons"""
    return os.environ.get("TWILIO_REMINDER_CONTENT_SID")


def format_whatsapp_number(phone: str) -> str:
    """Format phone number for WhatsApp (add whatsapp: prefix)"""
    # Remove any existing whatsapp: prefix
    phone = phone.replace("whatsapp:", "")
    # Ensure it starts with +
    if not phone.startswith("+"):
        phone = f"+{phone}"
    return f"whatsapp:{phone}"


async def send_whatsapp_message(to_phone: str, message: str) -> Optional[str]:
    """
    Send a WhatsApp message via Twilio
    Returns the message SID if successful, None otherwise
    """
    if not is_twilio_configured():
        logger.warning("Twilio is not configured. Message not sent.")
        return None
    
    twilio_client = get_twilio_client()
    if not twilio_client:
        logger.error("Twilio client is not initialized")
        return None
    
    try:
        whatsapp_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
        from_number = format_whatsapp_number(whatsapp_number)
        to_number = format_whatsapp_number(to_phone)
        
        message_response = twilio_client.messages.create(
            body=message,
            from_=from_number,
            to=to_number
        )
        
        logger.info(f"WhatsApp message sent successfully. SID: {message_response.sid}")
        return message_response.sid
    
    except TwilioRestException as e:
        logger.error(f"Twilio error sending message: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error sending WhatsApp message: {e}")
        return None


async def send_interactive_button_message(
    to_phone: str,
    body_text: str,
    buttons: List[Dict[str, str]],
    header: str = None,
    footer: str = None,
    scheduled_time: datetime = None
) -> Optional[str]:
    """
    Send a WhatsApp message with interactive quick reply buttons.
    
    Uses pre-approved Twilio/WhatsApp templates for NATIVE tap buttons.
    These work even outside the 24-hour messaging window.
    Falls back to text-based buttons if templates fail.
    
    Args:
        to_phone: Recipient's phone number
        body_text: Main message body
        buttons: List of buttons (used for fallback text buttons)
        header: Optional header text
        footer: Optional footer text (like "- Daisy")
        scheduled_time: Optional scheduled time for using appointment template
    
    Returns:
        Message SID if successful, None otherwise
    """
    if not is_twilio_configured():
        logger.warning("Twilio is not configured. Message not sent.")
        return None
    
    twilio_client = get_twilio_client()
    if not twilio_client:
        logger.error("Twilio client is not initialized")
        return None
    
    messaging_service_sid = get_messaging_service_sid()
    reminder_content_sid = get_reminder_content_sid()  # Custom template (may not be approved)
    
    # Pre-approved Twilio template that works outside 24-hour window
    APPROVED_APPOINTMENT_TEMPLATE_SID = "HX80a1ea89a96902e71eb15bff8f1a43c0"
    
    try:
        to_number = format_whatsapp_number(to_phone)
        whatsapp_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
        from_number = format_whatsapp_number(whatsapp_number)
        
        # Build full message body with footer
        full_body = body_text
        if footer:
            full_body = f"{body_text}\n\n{footer}"
        
        # Method 1: Try the custom Content Template first (works within 24-hr window)
        if messaging_service_sid and reminder_content_sid:
            try:
                message_response = twilio_client.messages.create(
                    messaging_service_sid=messaging_service_sid,
                    content_sid=reminder_content_sid,
                    content_variables=json.dumps({"1": full_body[:1000]}),
                    to=to_number
                )
                
                logger.info(f"Native button message sent via Content Template. SID: {message_response.sid}")
                return message_response.sid
                
            except TwilioRestException as e:
                # Error 63016 means outside 24-hour window - try approved template
                if "63016" in str(e) or "freeform" in str(e).lower():
                    logger.info("Outside 24-hr window, trying pre-approved template...")
                else:
                    logger.warning(f"Content Template failed: {e}")
        
        # Method 2: Use pre-approved appointment reminder template (works anytime)
        if messaging_service_sid and scheduled_time:
            try:
                # Format date and time for the template
                if isinstance(scheduled_time, str):
                    scheduled_time = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00'))
                
                date_str = scheduled_time.strftime("%d %B %Y")  # e.g., "06 April 2026"
                time_str = scheduled_time.strftime("%I:%M %p")  # e.g., "11:30 AM"
                
                message_response = twilio_client.messages.create(
                    messaging_service_sid=messaging_service_sid,
                    content_sid=APPROVED_APPOINTMENT_TEMPLATE_SID,
                    content_variables=json.dumps({
                        "date": date_str,
                        "time": time_str
                    }),
                    to=to_number
                )
                
                logger.info(f"Pre-approved template message sent. SID: {message_response.sid}")
                return message_response.sid
                
            except Exception as e:
                logger.warning(f"Pre-approved template also failed: {e}")
        
        # Method 3: Fallback to regular text message with button-like formatting
        button_text = "\n\n*Reply:*\n"
        button_emojis = ["1️⃣", "2️⃣", "3️⃣"]
        for i, btn in enumerate(buttons[:3]):
            title = btn.get("title", "Option")
            button_text += f"{button_emojis[i]} *{title}*\n"
        
        full_message = body_text + button_text
        if footer:
            full_message += f"\n{footer}"
        
        message_response = twilio_client.messages.create(
            body=full_message,
            from_=from_number,
            to=to_number
        )
        
        logger.info(f"Text button message sent. SID: {message_response.sid}")
        return message_response.sid
        
    except TwilioRestException as e:
        logger.error(f"Twilio error sending interactive message: {e}")
        return None
    except Exception as e:
        logger.error(f"Error sending interactive message: {e}")
        return None


# ============== SMART REMINDER MESSAGES WITH BUTTONS ==============

async def send_smart_reminder(
    to_phone: str,
    message: str,
    requester_name: str = None,
    recipient_relationship: str = None,
    reminder_id: str = None,
    is_self_reminder: bool = False,
    scheduled_time: datetime = None,
    created_at: datetime = None
) -> Optional[str]:
    """
    Send a reminder with interactive completion buttons.
    
    Uses the Meta-approved template daisy_reminder_v1 with 3 variables:
    {{1}} = recipient name (e.g., "Dad")
    {{2}} = sender name (e.g., "Kush")  
    {{3}} = message (e.g., "Take your medicine")
    
    Buttons:
    - Done ✅ - Mark as completed
    - Later ⏰ - Snooze for 10 minutes
    - Skip ⏭️ - Skip this reminder
    """
    if not is_twilio_configured():
        logger.warning("Twilio is not configured. Message not sent.")
        return None
    
    twilio_client = get_twilio_client()
    if not twilio_client:
        logger.error("Twilio client is not initialized")
        return None
    
    messaging_service_sid = get_messaging_service_sid()
    reminder_content_sid = get_reminder_content_sid()  # daisy_reminder_v1 template
    
    # Determine recipient name for the greeting
    recipient_name = "there"
    if recipient_relationship:
        rel = recipient_relationship.lower()
        if rel in ['mom', 'mum', 'mother', 'mama']:
            recipient_name = "Mom"
        elif rel in ['dad', 'father', 'papa']:
            recipient_name = "Dad"
        elif rel in ['grandma', 'grandmother', 'nana']:
            recipient_name = "Grandma"
        elif rel in ['grandpa', 'grandfather']:
            recipient_name = "Grandpa"
        else:
            recipient_name = recipient_relationship.capitalize()
    
    # Determine sender name
    sender_name = requester_name if requester_name and not is_self_reminder else "Daisy"
    
    # Build the message with created_at context
    full_message = message
    if created_at:
        try:
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            
            now = datetime.now(timezone.utc)
            days_ago = (now - created_at).days
            
            if days_ago == 0:
                full_message += "\n\n📅 Set earlier today"
            elif days_ago == 1:
                full_message += "\n\n📅 Set yesterday"
            elif days_ago < 7:
                full_message += f"\n\n📅 Set {days_ago} days ago"
            elif days_ago < 30:
                weeks = days_ago // 7
                full_message += f"\n\n📅 Set {weeks} week{'s' if weeks > 1 else ''} ago"
            else:
                full_message += f"\n\n📅 Set on {created_at.strftime('%b %d')}"
        except Exception:
            pass
    
    # Add caring message for delegated reminders
    if not is_self_reminder and requester_name:
        full_message += f"\n\n{requester_name} cares about you 💛"
    
    try:
        to_number = format_whatsapp_number(to_phone)
        
        # Use the Meta-approved template with 3 variables
        if messaging_service_sid and reminder_content_sid:
            try:
                message_response = twilio_client.messages.create(
                    messaging_service_sid=messaging_service_sid,
                    content_sid=reminder_content_sid,
                    content_variables=json.dumps({
                        "1": recipient_name,
                        "2": sender_name,
                        "3": full_message[:900]  # Limit message length
                    }),
                    to=to_number
                )
                
                logger.info(f"Smart reminder sent via approved template. SID: {message_response.sid}")
                return message_response.sid
                
            except TwilioRestException as e:
                logger.warning(f"Template message failed: {e}")
                # Fall through to text fallback
        
        # Fallback: Send as regular text message with button-like formatting
        whatsapp_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
        from_number = format_whatsapp_number(whatsapp_number)
        
        fallback_body = f"""Hi {recipient_name}! ⏰ Reminder from {sender_name}:

{full_message}

*Reply:*
1️⃣ *Done ✅*
2️⃣ *Later ⏰*
3️⃣ *Skip ⏭️*

— Daisy"""
        
        message_response = twilio_client.messages.create(
            body=fallback_body,
            from_=from_number,
            to=to_number
        )
        
        logger.info(f"Smart reminder sent via text fallback. SID: {message_response.sid}")
        return message_response.sid
        
    except TwilioRestException as e:
        logger.error(f"Twilio error sending smart reminder: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error sending smart reminder: {e}")
        return None


async def send_smart_followup(
    to_phone: str,
    message: str,
    follow_up_count: int,
    requester_name: str = None,
    reminder_id: str = None
) -> Optional[str]:
    """
    Send a gentle follow-up with completion buttons.
    Max 2 follow-ups, then graceful stop.
    """
    if follow_up_count == 1:
        # First follow-up - gentle check-in
        body_text = f"""🔔 *Quick check-in*

Just making sure you saw this:
_{message}_"""
    else:
        # Final follow-up - graceful stop
        if requester_name:
            body_text = f"""💭 *Final reminder*

_{message}_

I couldn't confirm if this was completed. I'll let {requester_name} know I'm still waiting to hear from you."""
        else:
            body_text = f"""💭 *Final reminder*

_{message}_

I couldn't confirm if this was completed. I'll check again tomorrow if needed."""
    
    # Define the buttons
    buttons = [
        {"id": "done", "title": "Done ✅"},
        {"id": "later", "title": "Later ⏰"},
        {"id": "skip", "title": "Skip ⏭️"}
    ]
    
    return await send_interactive_button_message(
        to_phone=to_phone,
        body_text=body_text,
        buttons=buttons,
        footer="— Daisy 💛"
    )


async def send_consent_request(to_phone: str, requester_name: str, reminder_description: str) -> Optional[str]:
    """Send a consent request message to a new contact (recipient)"""
    message = f"""🌼 *Hello! I'm Daisy* — an AI-powered reminder assistant.

*Important:* I'm an artificial intelligence (AI), not a human. I'm operated by *Daisy Can Handle It Pty Ltd*, an Australian business.

*{requester_name}* would like me to send you gentle reminders, starting with:
📝 "{reminder_description}"

*What this means:*
• {requester_name} sets reminders for you through me
• I'll message you at the scheduled times
• This is completely *FREE* for you

*Your data:*
• I'll store your phone number and reminder details
• Your data may be processed by AI providers (USA)
• Privacy Policy: https://daisycanhandleit.com/privacy

*Your choices:*
Reply *YES* to receive reminders from {requester_name}
Reply *NO* if you'd prefer not to
Reply *STOP* anytime to opt out

Take care! 🌸"""

    return await send_whatsapp_message(to_phone, message)


async def send_reminder_message(
    to_phone: str, 
    message: str, 
    requester_name: str,
    recipient_relationship: str = None,
    recipient_name: str = None
) -> Optional[str]:
    """
    Send a personalized reminder message.
    - recipient_relationship: How the requester refers to them (mom, dad, brother, etc.)
    - recipient_name: The actual name of the recipient
    """
    # Use relationship name if available for a personal touch
    greeting = ""
    if recipient_relationship:
        # Capitalize first letter for greeting
        relationship = recipient_relationship.capitalize()
        if relationship.lower() in ['mom', 'mum', 'mother', 'mama']:
            greeting = "Hi Mom! 💛\n\n"
        elif relationship.lower() in ['dad', 'father', 'papa']:
            greeting = "Hi Dad! 💛\n\n"
        elif relationship.lower() in ['grandma', 'grandmother', 'nana', 'granny']:
            greeting = "Hi Grandma! 💛\n\n"
        elif relationship.lower() in ['grandpa', 'grandfather', 'granddad']:
            greeting = "Hi Grandpa! 💛\n\n"
        elif relationship.lower() in ['brother', 'bro']:
            greeting = "Hey Bro! 💛\n\n"
        elif relationship.lower() in ['sister', 'sis']:
            greeting = "Hey Sis! 💛\n\n"
        else:
            greeting = f"Hi {relationship}! 💛\n\n"
    elif recipient_name:
        greeting = f"Hi {recipient_name}! 💛\n\n"
    
    full_message = f"""{greeting}🌼 *{requester_name} wanted me to tell you:*

{message}

Please let me know when this is done - {requester_name} cares about you and wants to make sure you're taken care of! 

Just reply "Done" or "Got it" to confirm 💛

- Daisy"""

    return await send_whatsapp_message(to_phone, full_message)


async def send_follow_up_message(
    to_phone: str, 
    original_message: str, 
    follow_up_count: int,
    requester_name: str = None,
    recipient_relationship: str = None
) -> Optional[str]:
    """Send a follow-up message for unacknowledged reminders"""
    
    # Personal greeting
    greeting = ""
    if recipient_relationship:
        relationship = recipient_relationship.capitalize()
        if relationship.lower() in ['mom', 'mum', 'mother', 'mama']:
            greeting = "Hi Mom, "
        elif relationship.lower() in ['dad', 'father', 'papa']:
            greeting = "Hi Dad, "
        elif relationship.lower() in ['grandma', 'grandmother']:
            greeting = "Hi Grandma, "
        elif relationship.lower() in ['grandpa', 'grandfather']:
            greeting = "Hi Grandpa, "
        else:
            greeting = f"Hi {relationship}, "
    
    caring_note = f"{requester_name} is thinking of you and " if requester_name else "Someone who cares about you "
    
    if follow_up_count == 1:
        message = f"""🌼 *{greeting}just checking in...*

{original_message}

{caring_note}wanted me to make sure you saw this 💛

Just reply "Done" when you've got it!

- Daisy"""
    elif follow_up_count == 2:
        message = f"""🌼 *{greeting}a gentle nudge...*

{original_message}

I know life gets busy! {caring_note}wants to make sure this doesn't slip through 💛

- Daisy"""
    else:
        message = f"""🌼 *{greeting}one more time...*

{original_message}

This is my last reminder for now. {requester_name if requester_name else 'Your loved one'} will be happy to know you've seen this!

Reply anytime 💛

- Daisy"""

    return await send_whatsapp_message(to_phone, message)


async def send_acknowledgment_to_creator(to_phone: str, recipient_name: str, reminder_message: str) -> Optional[str]:
    """Notify the creator that their reminder was acknowledged"""
    message = f"""💛 *Great news!*

{recipient_name} has seen your reminder:
"{reminder_message}"

Your care made a difference today! 🌼

- Daisy"""

    return await send_whatsapp_message(to_phone, message)


# ============== TEAM-RELATED MESSAGES ==============

async def send_team_join_notification(to_phone: str, team_name: str, added_by_name: str, invite_type: str = "added") -> Optional[str]:
    """Notify someone they've been added to a team (pending approval)"""
    if invite_type == "added":
        message = f"""🌼 *Welcome to the team!*

You've been added to "{team_name}" by {added_by_name} 💛

You'll start receiving team reminders once everything is set up. Teamwork makes the dream work!

- Daisy"""
    else:  # invite link
        message = f"""🌼 *Welcome!* 

You've requested to join "{team_name}".

Your request is pending approval from a team admin. You'll be notified once approved.

- Daisy"""

    return await send_whatsapp_message(to_phone, message)


async def send_team_member_approved(to_phone: str, team_name: str, approved_by: str) -> Optional[str]:
    """Notify a member they've been approved"""
    message = f"""🌼 Great news! You've been approved to join "{team_name}" by {approved_by}.

You'll now receive team reminders. Reply to any reminder with "Done", "Sure", or "Okay" to acknowledge.

- Daisy"""

    return await send_whatsapp_message(to_phone, message)


async def send_team_reminder_message(to_phone: str, team_name: str, message: str, creator_name: str) -> Optional[str]:
    """Send a team reminder to a member"""
    full_message = f"""🌼 Team Reminder from {team_name}:

{message}

(Sent by {creator_name})

Please reply "Done", "Sure", or "Okay" to confirm.

- Daisy"""

    return await send_whatsapp_message(to_phone, full_message)


async def send_team_reminder_progress(to_phone: str, team_name: str, message: str, acknowledged: int, total: int) -> Optional[str]:
    """Update creator on team reminder progress"""
    if acknowledged == total:
        progress_message = f"""🌼 Team reminder completed!

Team: {team_name}
Reminder: "{message}"

✅ All {total} members have acknowledged!

- Daisy"""
    else:
        progress_message = f"""🌼 Team reminder update:

Team: {team_name}
Reminder: "{message}"

Progress: {acknowledged}/{total} members acknowledged

- Daisy"""

    return await send_whatsapp_message(to_phone, progress_message)


async def send_team_created_notification(to_phone: str, team_name: str, invite_code: str, invite_url: str) -> Optional[str]:
    """Notify owner that team was created with invite link"""
    message = f"""🌼 Team "{team_name}" created successfully!

📎 Invite Link: {invite_url}
🔑 Invite Code: {invite_code}

Share this link with your team members to let them join.

Commands:
• "Add [phone] to {team_name}" - Add a member
• "Show {team_name} members" - List members
• "Remind {team_name} to [task] at [time]" - Send team reminder

- Daisy"""

    return await send_whatsapp_message(to_phone, message)
