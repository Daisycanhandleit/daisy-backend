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
from openai import AsyncOpenAI

# Load environment variables
ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logger = logging.getLogger(__name__)

# Initialize OpenAI client
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


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
    if not openai_client:
        logger.error("OpenAI API key not configured for voice transcription")
        return None
    
    try:
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
            # Transcribe the audio using OpenAI Whisper
            with open(audio_path, "rb") as audio_file:
                # Force transcription to English output (romanized)
                response = await openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="en",  # Force English transcription
                    response_format="text"
                )
                
                transcription = response.strip() if isinstance(response, str) else response
                logger.info(f"Transcription result: {transcription}")
                
                # Clean up audio file
                try:
                    os.unlink(audio_path)
                except:
                    pass
                
                return transcription
                
        except Exception as transcribe_error:
            logger.error(f"Transcription failed: {transcribe_error}")
            # Clean up
            try:
                os.unlink(audio_path)
            except:
                pass
            return None
            
    except Exception as e:
        logger.error(f"Error in transcribe_audio: {e}")
        return None


def normalize_hindi_to_english(text: str) -> str:
    """
    Convert common Hindi/Hinglish phrases to English equivalents for intent detection
    
    Args:
        text: Transcribed text that may contain Hindi words
    
    Returns:
        Text with Hindi phrases converted to English
    """
    if not text:
        return text
    
    # Common Hindi -> English mappings for reminder-related phrases
    hindi_mappings = {
        # Completion phrases
        'ho gaya': 'done',
        'hogaya': 'done',
        'ho gya': 'done',
        'hogya': 'done',
        'kar diya': 'done',
        'kardiya': 'done',
        'kar liya': 'done',
        'karliya': 'done',
        'khatam': 'done',
        'complete': 'done',
        'finished': 'done',
        'khatm ho gaya': 'done',
        'ban gaya': 'done',
        'mukammal': 'done',
        
        # Skip/Later phrases
        'baad mein': 'later',
        'baad me': 'later',
        'thodi der baad': 'later',
        'abhi nahi': 'later',
        'rukho': 'wait',
        'ruko': 'wait',
        'skip karo': 'skip',
        'chhod do': 'skip',
        'chod do': 'skip',
        'rehne do': 'skip',
        'mat karo': 'skip',
        
        # Reminder phrases
        'yaad dilao': 'remind',
        'yaad dila do': 'remind',
        'yaad dilana': 'remind',
        'reminder set karo': 'set reminder',
        'remind karo': 'remind',
        
        # Time-related
        'kal': 'tomorrow',
        'aaj': 'today',
        'abhi': 'now',
        'subah': 'morning',
        'dopahar': 'afternoon',
        'shaam': 'evening',
        'raat': 'night',
        'minute': 'minute',
        'ghanta': 'hour',
        'baje': 'o\'clock',
        
        # Family members
        'mummy': 'mom',
        'mummi': 'mom',
        'maa': 'mom',
        'papa': 'dad',
        'pitaji': 'dad',
        'dadi': 'grandma',
        'nani': 'grandma',
        'dada': 'grandpa',
        'nana': 'grandpa',
        'bhai': 'brother',
        'behen': 'sister',
        'beti': 'daughter',
        'beta': 'son',
        
        # Common verbs
        'karo': 'do',
        'kar': 'do',
        'lo': 'take',
        'le': 'take',
        'khao': 'eat',
        'kha': 'eat',
        'piyo': 'drink',
        'pi': 'drink',
        'jao': 'go',
        'ja': 'go',
        'bolo': 'tell',
        'bol': 'tell',
        
        # Affirmations
        'haan': 'yes',
        'ha': 'yes',
        'ji': 'yes',
        'theek hai': 'okay',
        'thik hai': 'okay',
        'accha': 'okay',
        'nahi': 'no',
        'na': 'no',
    }
    
    text_lower = text.lower()
    
    # Apply mappings
    for hindi, english in hindi_mappings.items():
        text_lower = text_lower.replace(hindi, english)
    
    return text_lower


def is_voice_command_for_reminder_action(text: str) -> Tuple[bool, str]:
    """
    Check if voice command is for a reminder action (done/later/skip)
    
    Args:
        text: Transcribed and normalized text
    
    Returns:
        Tuple of (is_action_command, action_type)
    """
    if not text:
        return False, ""
    
    text_lower = normalize_hindi_to_english(text.lower().strip())
    
    # Check for completion keywords
    done_keywords = ['done', 'complete', 'finished', 'yes done', 'mark done', 'completed', 'i did it', 'all done']
    for keyword in done_keywords:
        if keyword in text_lower:
            return True, 'done'
    
    # Check for later/snooze keywords
    later_keywords = ['later', 'snooze', 'remind later', 'not now', 'wait', '10 minutes', 'remind me later']
    for keyword in later_keywords:
        if keyword in text_lower:
            return True, 'later'
    
    # Check for skip keywords
    skip_keywords = ['skip', 'cancel', 'ignore', 'not today', "don't remind", 'no thanks']
    for keyword in skip_keywords:
        if keyword in text_lower:
            return True, 'skip'
    
    return False, ""


async def process_voice_note(
    media_url: str,
    twilio_auth: tuple
) -> Dict:
    """
    Full voice note processing pipeline
    
    Args:
        media_url: Twilio media URL
        twilio_auth: (account_sid, auth_token)
    
    Returns:
        Dict with transcription and detected intent
    """
    result = {
        "success": False,
        "transcription": None,
        "normalized_text": None,
        "is_action_command": False,
        "action_type": None,
        "error": None
    }
    
    # Download audio
    audio_bytes = await download_audio_file(media_url, twilio_auth)
    if not audio_bytes:
        result["error"] = "Failed to download audio"
        return result
    
    # Transcribe
    transcription = await transcribe_audio(audio_bytes)
    if not transcription:
        result["error"] = "Failed to transcribe audio"
        return result
    
    result["transcription"] = transcription
    
    # Normalize Hindi/Hinglish to English
    normalized = normalize_hindi_to_english(transcription)
    result["normalized_text"] = normalized
    
    # Check for action commands
    is_action, action_type = is_voice_command_for_reminder_action(normalized)
    result["is_action_command"] = is_action
    result["action_type"] = action_type
    result["success"] = True
    
    return result


def is_complex_voice_command(text: str) -> bool:
    """
    Check if the voice command is complex and should be sent to AI
    rather than simple keyword matching.
    
    Complex commands include:
    - Time corrections ("not 3, I said 9")
    - Multiple instructions
    - Context-dependent phrases
    
    Args:
        text: Normalized transcription text
    
    Returns:
        True if command is complex
    """
    if not text:
        return False
    
    text_lower = text.lower()
    
    # Patterns that indicate complex commands
    complex_patterns = [
        'not',  # Negation/correction
        'i said',  # Correction
        'i meant',  # Correction
        'change',  # Modification
        'actually',  # Correction
        'instead',  # Change
        'wrong',  # Error correction
        'correct',  # Correction
        'update',  # Modification
        'modify',  # Modification
        ' and ',  # Multiple instructions
        ' then ',  # Sequential instructions
        ' also ',  # Additional instructions
    ]
    
    for pattern in complex_patterns:
        if pattern in text_lower:
            return True
    
    return False
