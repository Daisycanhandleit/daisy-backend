"""
Voice Processing Module for Daisy
Handles WhatsApp voice note transcription and intent detection
Supports English and Hindi (including Hinglish)
"""

import os
import logging
import tempfile
import httpx
from typing import Optional, Dict, Tuple
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

from emergentintegrations.llm.openai import OpenAISpeechToText

logger = logging.getLogger(__name__)

# Initialize Speech-to-Text
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY')


async def download_audio_file(media_url: str, auth: tuple) -> Optional[bytes]:
    """
    Download audio file from Twilio media URL
    
    Args:
        media_url: Twilio media URL for the voice note
        auth: Tuple of (account_sid, auth_token) for Twilio authentication
    
    Returns:
        Audio file bytes or None if download fails
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(media_url, auth=auth, follow_redirects=True)
            if response.status_code == 200:
                logger.info(f"Downloaded audio file: {len(response.content)} bytes")
                return response.content
            else:
                logger.error(f"Failed to download audio: HTTP {response.status_code}")
                return None
    except Exception as e:
        logger.error(f"Error downloading audio file: {e}")
        return None


async def transcribe_audio(audio_bytes: bytes, language: str = None) -> Optional[str]:
    """
    Transcribe audio bytes to text using OpenAI Whisper
    
    Args:
        audio_bytes: Raw audio file bytes
        language: Optional language code (e.g., 'en', 'hi' for Hindi)
    
    Returns:
        Transcribed text or None if transcription fails
    """
    if not EMERGENT_LLM_KEY:
        logger.error("EMERGENT_LLM_KEY not configured for voice transcription")
        return None
    
    try:
        # Initialize STT
        stt = OpenAISpeechToText(api_key=EMERGENT_LLM_KEY)
        
        # Write audio to temporary OGG file
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_ogg:
            tmp_ogg.write(audio_bytes)
            tmp_ogg_path = tmp_ogg.name
        
        # Convert OGG to MP3 using pydub (Whisper doesn't support OGG)
        try:
            from pydub import AudioSegment
            
            # Load OGG and export as MP3
            audio = AudioSegment.from_ogg(tmp_ogg_path)
            tmp_mp3_path = tmp_ogg_path.replace('.ogg', '.mp3')
            audio.export(tmp_mp3_path, format="mp3")
            
            logger.info(f"Converted OGG to MP3: {tmp_mp3_path}")
            
            # Clean up OGG file
            os.unlink(tmp_ogg_path)
            
            # Use MP3 file for transcription
            audio_path = tmp_mp3_path
            
        except Exception as conv_error:
            logger.warning(f"OGG to MP3 conversion failed: {conv_error}. Trying direct transcription...")
            audio_path = tmp_ogg_path
        
        try:
            # Transcribe the audio
            with open(audio_path, "rb") as audio_file:
                # Force transcription to English output (romanized)
                # This ensures Hindi/Hinglish is transcribed as English letters, not Devanagari/Urdu
                kwargs = {
                    "file": audio_file,
                    "model": "whisper-1",
                    "response_format": "json",
                    "temperature": 0.0,
                    "language": "en",  # Force English output (will romanize Hindi/Hinglish)
                    "prompt": "Transcribe this voice message in English letters only. The speaker may say Hindi words like: done, ho gaya, kar diya, later, baad mein, skip, remind me, yaad dilao, chhod do, abhi nahi. Use English/Roman letters only, no Devanagari or Urdu script."
                }
                
                response = await stt.transcribe(**kwargs)
                
                transcribed_text = response.text.strip()
                logger.info(f"Transcribed audio: '{transcribed_text}'")
                return transcribed_text
                
        finally:
            # Clean up temp file
            if os.path.exists(audio_path):
                os.unlink(audio_path)
            
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def detect_voice_intent(transcribed_text: str) -> Dict:
    """
    Detect intent from transcribed voice message
    Supports English, Hindi, and Hinglish phrases
    
    IMPORTANT: Only detect clear, standalone response intents.
    If the message seems like a command/request (not a response), return "unknown"
    so the AI can handle it properly.
    
    Args:
        transcribed_text: The transcribed text from voice note
    
    Returns:
        Dict with 'intent' and 'confidence' keys
    """
    if not transcribed_text:
        return {"intent": "unknown", "confidence": 0.0}
    
    text_lower = transcribed_text.lower().strip()
    words = text_lower.split()
    
    # If the message is long (more than 5 words), it's likely a command, not a simple response
    # Let the AI handle it
    if len(words) > 6:
        return {"intent": "unknown", "confidence": 0.0, "reason": "long_message", "transcription": transcribed_text}
    
    # Check if message contains command-like words - if so, let AI handle it
    command_indicators = [
        "remind", "yaad", "dilao", "set", "change", "karo", "baje", "time",
        "morning", "evening", "subah", "shaam", "kal", "tomorrow", "today", "aaj"
    ]
    
    for indicator in command_indicators:
        if indicator in text_lower:
            return {"intent": "unknown", "confidence": 0.0, "reason": "contains_command_word", "transcription": transcribed_text}
    
    # ============== COMPLETED INTENT ==============
    # Only match if these are the MAIN words, not part of a longer sentence
    completed_exact = [
        "done", "its done", "it's done", "finished", "completed", "complete",
        "yes", "yep", "yeah", "yup", "ok", "okay",
        "ho gaya", "hogaya", "ho gya", "hogya",
        "kar diya", "kardiya", "kar dia", 
        "kar liya", "karliya",
        "kha liya", "khaliya", "le liya", "leliya",
        "haan", "ha ji", "ji haan", "theek hai", "thik hai"
    ]
    
    # ============== DEFER/LATER INTENT ==============
    defer_exact = [
        "later", "not now", "busy",
        "baad mein", "baad me", "abhi nahi", "busy hoon"
    ]
    
    # ============== SNOOZE INTENT ==============
    snooze_exact = [
        "snooze", "remind again", "remind later", "again",
        "phir se", "dobara", "ek baar aur"
    ]
    
    # ============== SKIP INTENT ==============
    skip_exact = [
        "skip", "skip it", "no", "nope", "cancel", "ignore",
        "chhoddo", "chhodo", "chod do", "nahi karna", "rehne do", "jane do"
    ]
    
    # Check for EXACT matches or very close matches
    for phrase in completed_exact:
        if text_lower == phrase or text_lower.startswith(phrase + " ") or text_lower.endswith(" " + phrase):
            return {"intent": "completed", "confidence": 0.95, "matched_phrase": phrase}
    
    for phrase in snooze_exact:
        if text_lower == phrase or phrase in text_lower:
            return {"intent": "snooze", "confidence": 0.9, "matched_phrase": phrase}
    
    for phrase in defer_exact:
        if text_lower == phrase or phrase in text_lower:
            return {"intent": "defer", "confidence": 0.85, "matched_phrase": phrase}
    
    for phrase in skip_exact:
        # Be more careful with skip - only match exact phrases
        if text_lower == phrase or text_lower == phrase + ".":
            return {"intent": "skip", "confidence": 0.9, "matched_phrase": phrase}
    
    # No clear intent detected - let AI handle it
    return {"intent": "unknown", "confidence": 0.0, "transcription": transcribed_text}


async def process_voice_note(
    media_url: str,
    twilio_account_sid: str,
    twilio_auth_token: str
) -> Tuple[Optional[str], Dict]:
    """
    Full pipeline: Download -> Transcribe -> Detect Intent
    
    Args:
        media_url: Twilio media URL for the voice note
        twilio_account_sid: Twilio Account SID for authentication
        twilio_auth_token: Twilio Auth Token for authentication
    
    Returns:
        Tuple of (transcribed_text, intent_dict)
    """
    # Step 1: Download the audio
    auth = (twilio_account_sid, twilio_auth_token)
    audio_bytes = await download_audio_file(media_url, auth)
    
    if not audio_bytes:
        return None, {"intent": "error", "error": "Failed to download audio"}
    
    # Step 2: Transcribe
    transcribed_text = await transcribe_audio(audio_bytes)
    
    if not transcribed_text:
        return None, {"intent": "error", "error": "Failed to transcribe audio"}
    
    # Step 3: Detect intent
    intent_result = detect_voice_intent(transcribed_text)
    intent_result["transcription"] = transcribed_text
    
    logger.info(f"Voice note processed: '{transcribed_text}' -> Intent: {intent_result['intent']}")
    
    return transcribed_text, intent_result


def is_voice_command_for_reminder(transcribed_text: str) -> bool:
    """
    Check if the transcribed text is a command to CREATE a reminder
    (as opposed to responding to an existing reminder)
    
    Args:
        transcribed_text: The transcribed text from voice note
    
    Returns:
        True if this looks like a command to create a reminder
    """
    if not transcribed_text:
        return False
    
    text_lower = transcribed_text.lower()
    
    # English command patterns
    reminder_commands_en = [
        "remind me", "remind myself", "set a reminder", "set reminder",
        "remind my", "remind him", "remind her", "remind them",
        "create a reminder", "make a reminder", "add a reminder",
        "don't let me forget", "help me remember",
        "remind us", "set an alarm", "set alarm"
    ]
    
    # Hindi command patterns
    reminder_commands_hi = [
        "yaad dilao", "yaad dila do", "yaad dilana",
        "remind karo", "remind kar do", "reminder set karo",
        "mujhe yaad", "hame yaad", "unhe yaad",
        "bhulne mat dena", "bhoolne mat dena"
    ]
    
    # Habit creation patterns
    habit_commands = [
        "i want to start", "i want to build", "help me build",
        "create a habit", "set a habit", "daily habit",
        "every day", "everyday", "har din", "roz", "rozana"
    ]
    
    all_commands = reminder_commands_en + reminder_commands_hi + habit_commands
    
    for cmd in all_commands:
        if cmd in text_lower:
            return True
    
    return False
