import os
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from dotenv import load_dotenv
import pytz
from openai import AsyncOpenAI

load_dotenv()

logger = logging.getLogger(__name__)


def extract_phone_number_regex(text: str) -> Optional[str]:
    """
    Extract phone number from text using regex patterns.
    Returns cleaned phone number with + prefix or None.
    """
    if not text:
        return None
    
    # Common phone number patterns (prioritized)
    patterns = [
        # International format with + and country code
        r'(\+\d{1,3}[\s\-]?\d{6,14})',
        # Numbers starting with + followed by digits (flexible)
        r'(\+[\d\s\-\(\)]{10,20})',
        # Parentheses format like (91) 9582790310
        r'\(?\d{2,3}\)?[\s\-]?\d{6,14}',
        # Simple digit sequences (10+ digits, likely phone)
        r'(?<!\d)(\d{10,15})(?!\d)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            phone = match.group(1) if match.lastindex else match.group(0)
            # Clean: keep only digits and leading +
            cleaned = re.sub(r'[^\d+]', '', phone)
            # Ensure + prefix
            if not cleaned.startswith('+'):
                # Try to infer country code based on length
                if len(cleaned) == 10:
                    # Likely missing country code, could be India or US
                    # Don't auto-add, return as is for now
                    pass
                cleaned = '+' + cleaned
            if len(cleaned) >= 10:
                return cleaned
    
    return None


def is_likely_phone_input(text: str) -> bool:
    """
    Check if the user's message is likely just a phone number input.
    """
    text = text.strip()
    # If text is mostly digits/phone chars and short, it's likely a phone
    if len(text) < 20:
        digit_count = sum(1 for c in text if c.isdigit())
        if digit_count >= 8 and digit_count / max(len(text), 1) > 0.5:
            return True
    return False


# Initialize OpenAI client
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Phone prefix to timezone mapping
PHONE_TIMEZONE_MAP = {
    '+61': 'Australia/Melbourne',  # Australia
    '+91': 'Asia/Kolkata',         # India
    '+1': 'America/New_York',      # USA (default to ET)
    '+44': 'Europe/London',        # UK
    '+64': 'Pacific/Auckland',     # New Zealand
    '+65': 'Asia/Singapore',       # Singapore
    '+971': 'Asia/Dubai',          # UAE
}

def detect_timezone_from_phone(phone: str) -> str:
    """Detect likely timezone from phone number country code"""
    if not phone:
        return 'UTC'
    
    # Clean phone number
    phone = phone.replace('whatsapp:', '').strip()
    
    for prefix, tz in PHONE_TIMEZONE_MAP.items():
        if phone.startswith(prefix):
            return tz
    
    return 'UTC'

def get_current_time_for_timezone(tz_name: str) -> Tuple[datetime, datetime, pytz.BaseTzInfo]:
    """Get current time in specified timezone"""
    try:
        tz = pytz.timezone(tz_name)
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(tz)
        return now_utc, now_local, tz
    except Exception:
        now_utc = datetime.now(timezone.utc)
        return now_utc, now_utc, pytz.UTC

# Get current time for context
def get_current_time_context(user_timezone: str = 'Australia/Melbourne'):
    """Get current time context for AI prompt"""
    now_utc, now_local, tz = get_current_time_for_timezone(user_timezone)
    
    tz_abbrev = now_local.strftime('%Z')
    utc_offset = now_local.strftime('%z')
    
    return f"""Current time information:
- User's local time: {now_local.strftime('%Y-%m-%d %H:%M:%S')} ({tz_abbrev}, UTC{utc_offset[:3]}:{utc_offset[3:]})
- User's timezone: {user_timezone}
- UTC time: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}
- Today is {now_local.strftime('%A, %B %d, %Y')}

CRITICAL: When the user says a time like "9 AM" or "in 2 minutes", they mean in THEIR LOCAL TIME ({user_timezone}).
You MUST convert their local time to UTC for the scheduled_time field.
For example, if user is in Melbourne (UTC+11) and says "9 AM", that's 9 AM Melbourne time = {(now_local.replace(hour=9, minute=0, second=0)).astimezone(pytz.UTC).strftime('%Y-%m-%dT%H:%M:%S+00:00')} UTC."""

# System prompt for Daisy - The Digital Caregiver for Families
DAISY_SYSTEM_PROMPT = """You are Daisy, a warm and caring AI family care companion. You're NOT just a reminder tool - you're a digital caregiver who helps people look after the ones they love. Think of yourself as the thoughtful family member who never forgets.

CRITICAL RULE - LANGUAGE:
You MUST ALWAYS respond in ENGLISH only. Even if the user speaks Hindi, Hinglish, Urdu, or any other language, your response must be in English. You can understand Hindi phrases like "ho gaya", "kar diya", "baad mein", "yaad dilao" but always reply in English.

YOUR CORE PURPOSE:
Daisy exists for ONE powerful reason: to help people CARE for others. Not just self-reminders, but building care loops between families. When someone sets a reminder for their mom, dad, or grandma - that's love in action. You facilitate that love.

YOUR PERSONALITY & VOICE:
- You're warm, nurturing, and speak like a caring friend - never robotic or formal
- You celebrate care: "That's wonderful that you're looking out for your dad! 💛"
- You show empathy: "I know it can be hard to remember everything when life gets busy"
- You're gently encouraging, not pushy
- You remember relationships and use them naturally: "How's your mom doing with her medicine routine?"
- You use phrases like: "I'm here for your family", "Let's make sure they're looked after", "I'll take care of that"
- Add warmth with occasional emojis: 🌼 💛 ✨ (but don't overdo it)

WHAT MAKES YOU DIFFERENT:
- You remember personal context: names, relationships, preferences
- You create "care loops": remind someone → wait for response → notify the sender
- You give feedback to the sender so they know their loved one is okay
- You feel more human - you use their names, remember their family

{current_time}

{user_context}

{user_memory}

YOUR CAPABILITIES:
1. **Caring Reminders (your STRONGEST feature)**: "Remind [mom/dad/grandma/etc] to..." - remind loved ones and report back
2. **Personal Reminders**: "Remind me to..." - self reminders
3. **Habit Tracking**: "Help me build a habit of..." or "Track my [exercise/meditation/etc]"
4. **Remembering People**: When user tells you about relationships ("John is my father", "She is my wife"), STORE that
5. **Checking In**: Answer questions, provide gentle encouragement
6. **Managing Reminders**: List, stop, or modify reminders

UNDERSTANDING USER INTENT - PHONE NUMBERS:
When the user provides a phone number, it could be in various formats:
- With country code: +919582790310, +61452502696
- With spaces/dashes: +91 9582 790310, +91-9582-790310
- Without country code: 9582790310 (assume India +91 if 10 digits)
- In parentheses: (91) 9582790310

When extracting a phone number:
- Always include the + prefix
- Remove spaces, dashes, parentheses
- If no country code provided and 10 digits, check context (Indian names suggest +91)

IMPORTANT - DETECTING INTENT:
- "Remind my dad" / "Tell my mom" / "Remind [person name]" = Need to remind SOMEONE ELSE (not the user)
- "Remind me" / "Set a reminder for me" = User wants to be reminded themselves
- "Stop my morning reminder" / "Cancel the medicine reminder" / "Remove the reminder for dad" = CANCEL intent
- "Show my reminders" / "What reminders do I have" / "List reminders" = LIST intent
- "[Person] is my [relationship]" / "My mom's name is [name]" / "He is my father" = STORE MEMORY intent
- Look for relationship words: mom, dad, mother, father, grandma, grandpa, brother, sister, wife, husband, son, daughter, friend + names

MEMORY STORAGE:
When a user shares personal information like:
- "John is my father" → store_memory: {{fact: "father's name is John"}}
- "My mom's number is +91..." → store_memory: {{fact: "mom's phone is +91..."}}
- "I live in Melbourne" → store_memory: {{fact: "lives in Melbourne"}}
- "I'm allergic to peanuts" → store_memory: {{fact: "allergic to peanuts"}}
- "My dad takes medicine at 9am" → store_memory: {{fact: "dad takes medicine at 9am"}}

RESPONSE FORMAT:
You must respond with a valid JSON object. Include these fields:
{{
    "intent": "create_reminder" | "create_reminder_for_other" | "request_phone" | "provide_phone" | "habit_create" | "habit_check" | "habit_list" | "button_response" | "general_chat" | "cancel" | "stop_service" | "set_name" | "snooze_reminder" | "skip_reminder" | "list_reminders" | "store_memory" | "stop_reminder",
    "message": "the task or reminder message",
    "recipient_name": "self" or the person's name/relationship (e.g., "mom", "Dad", "grandma"),
    "recipient_phone": "+1234567890" (if provided, otherwise null),
    "scheduled_time": "ISO 8601 UTC datetime" (e.g., "2024-03-15T14:00:00+00:00"),
    "recurrence": "once" | "daily" | "weekly" | "hourly",
    "confidence": 0.0 to 1.0,
    "friendly_response": "Your warm, caring response to the user",
    "memory_fact": "fact to store about user (only for store_memory intent)",
    "memory_type": "relationship" | "preference" | "personal_info" | "health" | "routine" (only for store_memory intent)
}}

CRITICAL TIME HANDLING:
- "in X minutes" = add X minutes to CURRENT TIME in user's timezone, then convert to UTC
- "at 9 AM" = 9 AM in USER'S LOCAL TIMEZONE, convert to UTC
- "tomorrow at 8 AM" = next day 8 AM in user's timezone, convert to UTC
- "every day at 9 AM" = recurring daily, first occurrence at 9 AM user's time
- Always output scheduled_time in UTC with +00:00 suffix

RELATIONSHIP TO PHONE NUMBER HANDLING:
When user says "remind my dad" or "remind mom", they're asking to remind someone else.
1. If you don't have the phone number yet, set intent to "request_phone" and ask warmly
2. If they provide a number (even just digits), extract it and set intent to "provide_phone"
3. Remember the relationship name (dad, mom, etc.) for personalized messages

CANCEL/STOP REMINDER HANDLING:
When user says "stop my medicine reminder" or "cancel the daily reminder for dad":
- Set intent to "cancel" or "stop_reminder" 
- Set message to the reminder description they want stopped
- This is different from "skip" (which skips ONE occurrence of a reminder)

EXAMPLES:
User: "Remind my dad to take his medicine at 9 AM every day"
Response: {{"intent": "request_phone", "message": "take his medicine", "recipient_name": "dad", "scheduled_time": null, "recurrence": "daily", "confidence": 0.95, "friendly_response": "I'd love to help look after your dad! 💛 What's his WhatsApp number so I can send him gentle reminders?"}}

User: "+919582790310"  
Response: {{"intent": "provide_phone", "recipient_phone": "+919582790310", "confidence": 0.98, "friendly_response": "Perfect! I've got the number. I'll make sure your dad gets his medicine reminders. That's really caring of you! 🌼"}}

User: "Remind me to call mom at 6 PM"
Response: {{"intent": "create_reminder", "message": "call mom", "recipient_name": "self", "scheduled_time": "[6 PM in user's timezone converted to UTC]", "recurrence": "once", "confidence": 0.95, "friendly_response": "I'll remind you to call your mom at 6 PM! It's lovely that you're staying connected. 💛"}}

User: "Stop the morning medicine reminder"
Response: {{"intent": "cancel", "message": "morning medicine", "confidence": 0.9, "friendly_response": "I'll stop the morning medicine reminder for you. 🌼"}}

User: "John is my father"
Response: {{"intent": "store_memory", "memory_fact": "father's name is John", "memory_type": "relationship", "confidence": 0.95, "friendly_response": "Got it! I'll remember that John is your father. 💛 Let me know if you'd ever like me to set up reminders for him!"}}

User: "Show my reminders"
Response: {{"intent": "list_reminders", "confidence": 0.95, "friendly_response": "Let me pull up your reminders..."}}
"""


async def parse_user_message(
    user_message: str, 
    user_phone: str, 
    user_context: dict = None,
    from_voice: bool = False,
    session_id: str = None,
    user_memory: list = None
) -> dict:
    """
    Parse user message using OpenAI to extract intent and entities.
    
    Args:
        user_message: The text message from the user
        user_phone: User's phone number for context
        user_context: Additional context about the user
        from_voice: Whether this message came from voice transcription
        session_id: Session ID for conversation continuity
        user_memory: List of stored memory facts about the user
        
    Returns:
        Parsed intent dictionary
    """
    if not openai_client:
        logger.error("OpenAI client not initialized - missing API key")
        return {
            "intent": "general_chat",
            "confidence": 0.5,
            "friendly_response": "I'm having a little trouble right now. Could you try again? 🌼"
        }
    
    # Check for quick phone number input first
    if is_likely_phone_input(user_message):
        phone = extract_phone_number_regex(user_message)
        if phone:
            return {
                "intent": "provide_phone",
                "recipient_phone": phone,
                "confidence": 0.95,
                "friendly_response": f"Got it! I have the number {phone}. 🌼"
            }
    
    # Detect user timezone from phone number
    user_timezone = detect_timezone_from_phone(user_phone)
    
    # Build user context string
    user_context_str = ""
    if user_context:
        if user_context.get('user_name'):
            user_context_str += f"User's name: {user_context['user_name']}\n"
        elif user_context.get('name'):
            user_context_str += f"User's name: {user_context['name']}\n"
        if user_context.get('pending_reminder'):
            pr = user_context['pending_reminder']
            user_context_str += f"CONTEXT: User previously wanted to remind '{pr.get('recipient_name', 'someone')}' about '{pr.get('message', 'something')}'. They may be providing a phone number now.\n"
        if user_context.get('contacts'):
            contacts = user_context['contacts']
            if contacts:
                user_context_str += "Known contacts:\n"
                for name, info in contacts.items():
                    user_context_str += f"  - {name}: {info.get('phone', 'no phone')} (consent: {info.get('consent_status', 'unknown')})\n"
    
    # Build user memory string
    memory_str = ""
    if user_memory:
        memory_str = "THINGS I REMEMBER ABOUT THIS USER:\n"
        for mem in user_memory[:20]:  # Limit to 20 most recent memories
            memory_str += f"- {mem.get('fact', '')}\n"
        memory_str += "\nUse this information naturally in your responses. Reference their family members by name when relevant."
    
    try:
        # Format system prompt with current time, user context, and memory
        system_prompt = DAISY_SYSTEM_PROMPT.format(
            current_time=get_current_time_context(user_timezone),
            user_context=user_context_str,
            user_memory=memory_str
        )
        
        # Send the message to OpenAI
        full_message = f"User message: {user_message}\nUser's phone: {user_phone}\nUser's timezone: {user_timezone}\n\nRespond with JSON only:"
        
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_message}
            ],
            temperature=0.7,
            max_tokens=1000
        )
        
        response_text = response.choices[0].message.content.strip()
        
        # Parse the JSON response
        try:
            # Remove markdown code blocks if present
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                parts = response_text.split("```")
                if len(parts) >= 2:
                    response_text = parts[1]
            
            # Find JSON object in response
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response_text, re.DOTALL)
            if json_match:
                response_text = json_match.group()
            
            parsed = json.loads(response_text.strip())
            logger.info(f"AI parsed message: {parsed}")
            
            # POST-PROCESSING: Try regex extraction if AI didn't find phone but message contains one
            if parsed.get('intent') in ['request_phone', 'general_chat'] and not parsed.get('recipient_phone'):
                regex_phone = extract_phone_number_regex(user_message)
                if regex_phone:
                    logger.info(f"AI missed phone, regex found: {regex_phone}")
                    parsed['intent'] = 'provide_phone'
                    parsed['recipient_phone'] = regex_phone
                    parsed['friendly_response'] = f"Got the number {regex_phone}! I'll reach out to them. 🌼"
            
            # If recipient is self and we have user's phone, add it
            if parsed.get('recipient_name') == 'self' and user_phone:
                parsed['recipient_phone'] = user_phone.replace('whatsapp:', '').strip()
            
            # Store user timezone in parsed result for later use
            parsed['user_timezone'] = user_timezone
            
            return parsed
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse AI response as JSON: {response_text}, error: {e}")
            
            # Try regex phone extraction as fallback
            regex_phone = extract_phone_number_regex(user_message)
            if regex_phone:
                return {
                    "intent": "provide_phone",
                    "recipient_phone": regex_phone,
                    "confidence": 0.85,
                    "friendly_response": f"Got the number {regex_phone}! I'll message them. 🌼",
                    "user_timezone": user_timezone
                }
            
            # Return a general chat response as fallback
            return {
                "intent": "general_chat",
                "confidence": 0.5,
                "friendly_response": "I'm not quite sure what you'd like me to do. Could you tell me more? 🌼",
                "user_timezone": user_timezone
            }
    except Exception as e:
        logger.error(f"Error calling OpenAI API: {e}")
        return {
            "intent": "general_chat",
            "confidence": 0.5,
            "friendly_response": "I'm having a moment - could you try that again? 🌼",
            "user_timezone": user_timezone if 'user_timezone' in dir() else 'UTC'
        }


async def generate_response(
    context: str,
    user_message: str,
    user_phone: str = None
) -> str:
    """
    Generate a conversational response using OpenAI.
    
    Args:
        context: Context about the conversation
        user_message: The user's message
        user_phone: User's phone for timezone detection
        
    Returns:
        Generated response string
    """
    if not openai_client:
        return "I'm here to help! What would you like me to remind you about? 🌼"
    
    user_timezone = detect_timezone_from_phone(user_phone) if user_phone else 'UTC'
    
    system_prompt = f"""You are Daisy, a warm and caring AI reminder assistant. 
You speak like a friendly, nurturing companion - never robotic.
Keep responses brief but warm. Use occasional emojis like 🌼 💛 ✨

{get_current_time_context(user_timezone)}

Context: {context}

Respond naturally and warmly in 1-2 sentences. ALWAYS respond in English only."""

    try:
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.8,
            max_tokens=200
        )
        
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Error generating response: {e}")
        return "I'm here to help! What would you like me to remind you about? 🌼"


async def transcribe_audio(audio_file_path: str) -> Optional[str]:
    """
    Transcribe audio file using OpenAI Whisper.
    
    Args:
        audio_file_path: Path to the audio file
        
    Returns:
        Transcribed text or None if failed
    """
    if not openai_client:
        logger.error("OpenAI client not initialized")
        return None
    
    try:
        with open(audio_file_path, "rb") as audio_file:
            response = await openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="en"
            )
            return response.text
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return None
