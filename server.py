from fastapi import FastAPI, APIRouter, HTTPException, Depends, Form, status, BackgroundTasks
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import re
import httpx
import vobject
import uuid
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from models import (
    UserCreate, UserLogin, User, UserResponse,
    ContactCreate, Contact, ContactResponse,
    ReminderCreate, Reminder, ReminderResponse,
    Message, MessageResponse,
    DashboardStats, TokenResponse, AIParseResult,
    TeamCreate, Team, TeamResponse,
    TeamMemberCreate, TeamMember, TeamMemberResponse,
    TeamReminderCreate, TeamReminder, TeamReminderResponse,
    TeamReminderAcknowledgment,
    MultiTimeReminder,
    Habit, HabitResponse, HabitLog, HabitModification, HabitStats,
    PendingHabitCreation, REMINDER_INTENSITY_CONFIG
)
from auth import (
    hash_password, verify_password, create_access_token,
    get_current_user, SECRET_KEY, ALGORITHM, security
)
import jwt
from jwt.exceptions import PyJWTError as JWTError
from whatsapp import (
    is_twilio_configured, send_whatsapp_message,
    send_consent_request, send_reminder_message,
    send_follow_up_message, send_acknowledgment_to_creator,
    send_team_reminder_message, send_team_join_notification
)
from ai_engine import parse_user_message, generate_response
from scheduler import start_scheduler, stop_scheduler

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Lifespan context manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start the scheduler and setup indexes
    logger.info("Starting Daisy reminder scheduler...")
    start_scheduler()
    
    # Create TTL index on pending_reminders for auto-cleanup after 24 hours
    try:
        await db.pending_reminders.create_index(
            "created_at", 
            expireAfterSeconds=86400,  # 24 hours
            background=True
        )
        logger.info("Created TTL index on pending_reminders collection")
    except Exception as e:
        logger.warning(f"Could not create TTL index (may already exist): {e}")
    
    yield
    # Shutdown: Stop the scheduler
    logger.info("Stopping Daisy reminder scheduler...")
    stop_scheduler()
    client.close()

# Create the main app with lifespan
app = FastAPI(title="Daisy - AI Life Concierge", lifespan=lifespan)

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Helper function to convert datetime to ISO string
def serialize_datetime(dt):
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt


def deserialize_datetime(dt_str):
    if isinstance(dt_str, str):
        return datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    return dt_str


# ============== ADMIN AUTH ==============

async def get_admin_user(credentials = Depends(security)):
    """Verify admin token and return admin info"""
    try:
        token = credentials.credentials
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        admin_id = payload.get("sub")
        is_admin = payload.get("is_admin", False)
        
        if not admin_id or not is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
        
        return {"admin_id": admin_id, "is_admin": True}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@api_router.post("/admin/auth/login")
async def admin_login(credentials: dict):
    """Admin login - separate from regular user login"""
    email = credentials.get('email')
    password = credentials.get('password')
    
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")
    
    # Find admin
    admin = await db.admins.find_one({"email": email}, {"_id": 0})
    
    if not admin:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    # Verify password
    if not verify_password(password, admin['password_hash']):
        raise HTTPException(status_code=401, detail="Invalid admin credentials")
    
    # Update last login
    await db.admins.update_one(
        {"email": email},
        {"$set": {"last_login": serialize_datetime(datetime.now(timezone.utc))}}
    )
    
    # Create admin token with special flag
    token_data = {
        "sub": admin['id'],
        "is_admin": True,
        "role": admin.get('role', 'admin'),
        "exp": datetime.now(timezone.utc) + timedelta(days=7)
    }
    token = jwt.encode(token_data, SECRET_KEY, algorithm=ALGORITHM)
    
    return {
        "access_token": token,
        "token_type": "bearer",
        "admin": {
            "id": admin['id'],
            "email": admin['email'],
            "name": admin['name'],
            "role": admin.get('role', 'admin')
        }
    }


@api_router.post("/admin/auth/setup")
async def setup_admin(credentials: dict):
    """
    One-time admin setup - creates the owner admin account.
    Only works if no admins exist yet.
    """
    # Check if any admin exists
    existing_admin = await db.admins.find_one({})
    if existing_admin:
        raise HTTPException(status_code=400, detail="Admin already exists. Contact system owner.")
    
    email = credentials.get('email')
    password = credentials.get('password')
    name = credentials.get('name', 'Owner')
    
    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")
    
    # Create owner admin
    admin = {
        "id": str(uuid.uuid4()),
        "email": email,
        "password_hash": hash_password(password),
        "name": name,
        "role": "owner",
        "created_at": serialize_datetime(datetime.now(timezone.utc)),
        "last_login": None
    }
    
    await db.admins.insert_one(admin)
    
    return {"message": "Admin account created successfully", "email": email}


@api_router.get("/admin/auth/check")
async def check_admin_exists():
    """Check if admin account has been set up"""
    existing_admin = await db.admins.find_one({}, {"_id": 0, "email": 1})
    return {"admin_exists": existing_admin is not None}


# ============== AUTH ROUTES ==============

@api_router.post("/auth/register", response_model=TokenResponse)
async def register(user_data: UserCreate):
    """Register a new user"""
    # Check if user already exists
    existing = await db.users.find_one({"email": user_data.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Create user
    user = User(
        email=user_data.email,
        name=user_data.name,
        phone=user_data.phone,
        timezone=user_data.timezone,
        password_hash=hash_password(user_data.password),
        subscription_status="trial",
        trial_end=datetime.now(timezone.utc) + timedelta(days=30)
    )
    
    # Serialize for MongoDB
    user_dict = user.model_dump()
    user_dict['trial_end'] = serialize_datetime(user_dict['trial_end'])
    user_dict['created_at'] = serialize_datetime(user_dict['created_at'])
    user_dict['updated_at'] = serialize_datetime(user_dict['updated_at'])
    
    await db.users.insert_one(user_dict)
    
    # Create access token
    token = create_access_token({"sub": user.id, "email": user.email})
    
    return TokenResponse(
        access_token=token,
        user=UserResponse(
            id=user.id,
            email=user.email,
            name=user.name,
            phone=user.phone,
            timezone=user.timezone,
            subscription_status=user.subscription_status,
            trial_end=serialize_datetime(user.trial_end),
            created_at=serialize_datetime(user.created_at)
        )
    )


@api_router.post("/auth/login", response_model=TokenResponse)
async def login(user_data: UserLogin):
    """Login user"""
    user = await db.users.find_one({"email": user_data.email}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not verify_password(user_data.password, user['password_hash']):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    # Create access token
    token = create_access_token({"sub": user['id'], "email": user['email']})
    
    return TokenResponse(
        access_token=token,
        user=UserResponse(
            id=user['id'],
            email=user['email'],
            name=user['name'],
            phone=user.get('phone'),
            timezone=user.get('timezone', 'UTC'),
            subscription_status=user['subscription_status'],
            trial_end=user['trial_end'],
            created_at=user['created_at']
        )
    )


@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    """Get current user profile"""
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return UserResponse(
        id=user['id'],
        email=user['email'],
        name=user['name'],
        phone=user.get('phone'),
        timezone=user.get('timezone', 'UTC'),
        subscription_status=user['subscription_status'],
        trial_end=user['trial_end'],
        created_at=user['created_at']
    )


# ============== SMART MESSAGING SETTINGS ==============

@api_router.get("/settings/smart-messaging")
async def get_smart_messaging_settings(current_user: dict = Depends(get_current_user)):
    """Get the user's smart messaging settings (agenda/wrapup times)"""
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    if not user or not user.get('phone'):
        raise HTTPException(status_code=400, detail="Please link your phone number first")
    
    # Get WhatsApp user settings
    wa_user = await db.whatsapp_users.find_one({"phone": user['phone']}, {"_id": 0})
    
    if not wa_user:
        # Return defaults
        return {
            "morning_agenda_time": "07:00",
            "evening_wrapup_time": "21:00",
            "timezone": user.get('timezone', 'UTC'),
            "agenda_enabled": True
        }
    
    return {
        "morning_agenda_time": wa_user.get('morning_agenda_time', '07:00'),
        "evening_wrapup_time": wa_user.get('evening_wrapup_time', '21:00'),
        "timezone": wa_user.get('timezone', user.get('timezone', 'UTC')),
        "agenda_enabled": wa_user.get('agenda_enabled', True)
    }


@api_router.put("/settings/smart-messaging")
async def update_smart_messaging_settings(
    settings: dict,
    current_user: dict = Depends(get_current_user)
):
    """Update the user's smart messaging settings"""
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    if not user or not user.get('phone'):
        raise HTTPException(status_code=400, detail="Please link your phone number first")
    
    # Validate times
    morning_time = settings.get('morning_agenda_time', '07:00')
    evening_time = settings.get('evening_wrapup_time', '21:00')
    
    # Simple validation
    try:
        h, m = map(int, morning_time.split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError()
        h, m = map(int, evening_time.split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM (24-hour)")
    
    # Update WhatsApp user settings
    update_data = {
        "morning_agenda_time": morning_time,
        "evening_wrapup_time": evening_time,
        "agenda_enabled": settings.get('agenda_enabled', True),
        "updated_at": serialize_datetime(datetime.now(timezone.utc))
    }
    
    if settings.get('timezone'):
        update_data['timezone'] = settings['timezone']
    
    await db.whatsapp_users.update_one(
        {"phone": user['phone']},
        {"$set": update_data},
        upsert=True
    )
    
    return {"message": "Smart messaging settings updated", "settings": update_data}


# ============== CONTACT ROUTES ==============

@api_router.get("/contacts", response_model=List[ContactResponse])
async def get_contacts(current_user: dict = Depends(get_current_user)):
    """Get all contacts for the current user"""
    contacts = await db.contacts.find(
        {"user_id": current_user['user_id']},
        {"_id": 0}
    ).to_list(1000)
    
    return [ContactResponse(
        id=c['id'],
        name=c['name'],
        phone=c['phone'],
        relationship=c.get('relationship'),
        consent_status=c['consent_status'],
        consent_date=c.get('consent_date'),
        created_at=c['created_at']
    ) for c in contacts]


@api_router.post("/contacts", response_model=ContactResponse)
async def create_contact(
    contact_data: ContactCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new contact"""
    # Check if contact already exists for this user
    existing = await db.contacts.find_one({
        "user_id": current_user['user_id'],
        "phone": contact_data.phone
    })
    if existing:
        raise HTTPException(status_code=400, detail="Contact with this phone already exists")
    
    contact = Contact(
        user_id=current_user['user_id'],
        name=contact_data.name,
        phone=contact_data.phone,
        relationship=contact_data.relationship,
        consent_status="pending"
    )
    
    contact_dict = contact.model_dump()
    contact_dict['created_at'] = serialize_datetime(contact_dict['created_at'])
    contact_dict['updated_at'] = serialize_datetime(contact_dict['updated_at'])
    
    await db.contacts.insert_one(contact_dict)
    
    return ContactResponse(
        id=contact.id,
        name=contact.name,
        phone=contact.phone,
        relationship=contact.relationship,
        consent_status=contact.consent_status,
        consent_date=None,
        created_at=serialize_datetime(contact.created_at)
    )


@api_router.delete("/contacts/{contact_id}")
async def delete_contact(
    contact_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete a contact"""
    result = await db.contacts.delete_one({
        "id": contact_id,
        "user_id": current_user['user_id']
    })
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"message": "Contact deleted"}


# ============== REMINDER ROUTES ==============

@api_router.get("/reminders", response_model=List[ReminderResponse])
async def get_reminders(
    reminder_status: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Get all reminders for the current user"""
    query = {"creator_id": current_user['user_id']}
    if reminder_status:
        query["status"] = reminder_status
    
    reminders = await db.reminders.find(query, {"_id": 0}).sort("scheduled_time", 1).to_list(1000)
    
    return [ReminderResponse(
        id=r['id'],
        message=r['message'],
        scheduled_time=r['scheduled_time'],
        recipient_phone=r['recipient_phone'],
        recipient_name=r.get('recipient_name'),
        recurrence=r['recurrence'],
        status=r['status'],
        acknowledgment=r.get('acknowledgment'),
        follow_up_count=r.get('follow_up_count', 0),
        created_at=r['created_at']
    ) for r in reminders]


@api_router.post("/reminders", response_model=ReminderResponse)
async def create_reminder(
    reminder_data: ReminderCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(get_current_user)
):
    """Create a new reminder"""
    # Check if we have a contact for this phone
    contact = await db.contacts.find_one({
        "user_id": current_user['user_id'],
        "phone": reminder_data.recipient_phone
    }, {"_id": 0})
    
    # Get creator info
    creator = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    
    reminder = Reminder(
        creator_id=current_user['user_id'],
        message=reminder_data.message,
        scheduled_time=reminder_data.scheduled_time,
        recipient_phone=reminder_data.recipient_phone,
        recipient_name=reminder_data.recipient_name or (contact['name'] if contact else None),
        recurrence=reminder_data.recurrence,
        end_date=reminder_data.end_date,
        contact_id=contact['id'] if contact else None,
        status="pending"
    )
    
    # If no contact exists, create one and request consent
    if not contact:
        new_contact = Contact(
            user_id=current_user['user_id'],
            name=reminder_data.recipient_name or "Unknown",
            phone=reminder_data.recipient_phone,
            consent_status="pending"
        )
        contact_dict = new_contact.model_dump()
        contact_dict['created_at'] = serialize_datetime(contact_dict['created_at'])
        contact_dict['updated_at'] = serialize_datetime(contact_dict['updated_at'])
        await db.contacts.insert_one(contact_dict)
        reminder.contact_id = new_contact.id
        
        # Send consent request
        if is_twilio_configured():
            background_tasks.add_task(
                send_consent_request,
                reminder_data.recipient_phone,
                creator['name'],
                reminder_data.message
            )
    elif contact['consent_status'] == 'pending':
        # Resend consent request
        if is_twilio_configured():
            background_tasks.add_task(
                send_consent_request,
                reminder_data.recipient_phone,
                creator['name'],
                reminder_data.message
            )
    
    reminder_dict = reminder.model_dump()
    reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
    reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
    reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
    if reminder_dict.get('end_date'):
        reminder_dict['end_date'] = serialize_datetime(reminder_dict['end_date'])
    
    await db.reminders.insert_one(reminder_dict)
    
    return ReminderResponse(
        id=reminder.id,
        message=reminder.message,
        scheduled_time=serialize_datetime(reminder.scheduled_time),
        recipient_phone=reminder.recipient_phone,
        recipient_name=reminder.recipient_name,
        recurrence=reminder.recurrence,
        status=reminder.status,
        acknowledgment=reminder.acknowledgment,
        follow_up_count=reminder.follow_up_count,
        created_at=serialize_datetime(reminder.created_at)
    )


@api_router.put("/reminders/{reminder_id}")
async def update_reminder(
    reminder_id: str,
    reminder_data: ReminderCreate,
    current_user: dict = Depends(get_current_user)
):
    """Update a reminder"""
    result = await db.reminders.update_one(
        {"id": reminder_id, "creator_id": current_user['user_id']},
        {"$set": {
            "message": reminder_data.message,
            "scheduled_time": serialize_datetime(reminder_data.scheduled_time),
            "recipient_phone": reminder_data.recipient_phone,
            "recipient_name": reminder_data.recipient_name,
            "recurrence": reminder_data.recurrence,
            "updated_at": serialize_datetime(datetime.now(timezone.utc))
        }}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"message": "Reminder updated"}


@api_router.delete("/reminders/{reminder_id}")
async def delete_reminder(
    reminder_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Delete/cancel a reminder"""
    result = await db.reminders.update_one(
        {"id": reminder_id, "creator_id": current_user['user_id']},
        {"$set": {"status": "cancelled"}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"message": "Reminder cancelled"}


# ============== MESSAGE ROUTES ==============

@api_router.get("/messages", response_model=List[MessageResponse])
async def get_messages(
    limit: int = 50,
    current_user: dict = Depends(get_current_user)
):
    """Get message history for the current user"""
    # Get user's phone
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    if not user or not user.get('phone'):
        return []
    
    user_phone = user['phone']
    
    messages = await db.messages.find(
        {"$or": [{"from_phone": user_phone}, {"to_phone": user_phone}]},
        {"_id": 0}
    ).sort("created_at", -1).to_list(limit)
    
    return [MessageResponse(
        id=m['id'],
        direction=m['direction'],
        from_phone=m['from_phone'],
        to_phone=m['to_phone'],
        content=m['content'],
        message_type=m.get('message_type', 'general'),
        status=m.get('status', 'sent'),
        created_at=m['created_at']
    ) for m in messages]


# ============== DASHBOARD ROUTES ==============

@api_router.get("/dashboard/stats", response_model=DashboardStats)
async def get_dashboard_stats(current_user: dict = Depends(get_current_user)):
    """Get dashboard statistics"""
    user_id = current_user['user_id']
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Get counts
    total_reminders = await db.reminders.count_documents({"creator_id": user_id})
    pending_reminders = await db.reminders.count_documents({"creator_id": user_id, "status": "pending"})
    sent_today = await db.reminders.count_documents({
        "creator_id": user_id,
        "status": "sent",
        "last_sent_at": {"$gte": serialize_datetime(today_start)}
    })
    acknowledged_today = await db.reminders.count_documents({
        "creator_id": user_id,
        "status": "acknowledged",
        "acknowledged_at": {"$gte": serialize_datetime(today_start)}
    })
    total_contacts = await db.contacts.count_documents({"user_id": user_id})
    pending_consents = await db.contacts.count_documents({"user_id": user_id, "consent_status": "pending"})
    approved_consents = await db.contacts.count_documents({"user_id": user_id, "consent_status": "approved"})
    
    return DashboardStats(
        total_reminders=total_reminders,
        pending_reminders=pending_reminders,
        sent_today=sent_today,
        acknowledged_today=acknowledged_today,
        total_contacts=total_contacts,
        pending_consents=pending_consents,
        approved_consents=approved_consents
    )


# ============== USER HABIT ROUTES ==============

@api_router.get("/habits")
async def get_user_habits(current_user: dict = Depends(get_current_user)):
    """Get all habits for the current user"""
    user_id = current_user['user_id']
    
    # Get user's phone to also find habits created via WhatsApp
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    user_phone = user.get('phone') if user else None
    
    # Query by user_id OR by phone (for WhatsApp-created habits)
    query = {"status": {"$ne": "deleted"}}
    if user_phone:
        query["$or"] = [
            {"user_id": user_id},
            {"user_phone": user_phone},
            {"user_id": f"whatsapp_{user_phone}"},
            {"shared_with": user_phone}  # Include habits shared with this user
        ]
    else:
        query["user_id"] = user_id
    
    habits = await db.habits.find(query, {"_id": 0}).sort("created_at", -1).to_list(100)
    
    # Calculate completion rate for each habit and mark if it's shared with user
    for habit in habits:
        total = habit.get('total_completions', 0) + habit.get('total_missed', 0)
        habit['completion_rate'] = round((habit.get('total_completions', 0) / total * 100), 1) if total > 0 else 0
        # Check if this is a shared habit (not owned by user)
        habit['is_shared_with_me'] = user_phone in (habit.get('shared_with') or []) if user_phone else False
        habit['owner'] = habit.get('shared_by') or 'You'
    
    return habits


@api_router.get("/habits/stats")
async def get_user_habit_stats(current_user: dict = Depends(get_current_user)):
    """Get comprehensive habit statistics for the current user"""
    user_id = current_user['user_id']
    
    # Get user's phone to also find habits created via WhatsApp
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    user_phone = user.get('phone') if user else None
    
    # Query by user_id OR by phone (for WhatsApp-created habits) OR shared with user
    query = {"status": "active"}
    if user_phone:
        query["$or"] = [
            {"user_id": user_id},
            {"user_phone": user_phone},
            {"user_id": f"whatsapp_{user_phone}"},
            {"shared_with": user_phone}  # Include shared habits
        ]
    else:
        query["user_id"] = user_id
    
    habits = await db.habits.find(query, {"_id": 0}).to_list(100)
    
    # Calculate overall stats
    total_habits = len(habits)
    total_completions = sum(h.get('total_completions', 0) for h in habits)
    total_missed = sum(h.get('total_missed', 0) for h in habits)
    total_actions = total_completions + total_missed
    overall_completion_rate = round((total_completions / total_actions * 100), 1) if total_actions > 0 else 0
    
    # Find best and worst performing habits
    best_habit = None
    worst_habit = None
    if habits:
        sorted_by_streak = sorted(habits, key=lambda h: h.get('current_streak', 0), reverse=True)
        best_habit = {
            "name": sorted_by_streak[0]['name'],
            "streak": sorted_by_streak[0].get('current_streak', 0),
            "category": sorted_by_streak[0].get('category', 'Custom')
        }
        worst_habit = {
            "name": sorted_by_streak[-1]['name'],
            "streak": sorted_by_streak[-1].get('current_streak', 0),
            "category": sorted_by_streak[-1].get('category', 'Custom')
        }
    
    # Category breakdown
    category_stats = {}
    for habit in habits:
        cat = habit.get('category', 'Custom')
        if cat not in category_stats:
            category_stats[cat] = {"count": 0, "completions": 0, "missed": 0}
        category_stats[cat]["count"] += 1
        category_stats[cat]["completions"] += habit.get('total_completions', 0)
        category_stats[cat]["missed"] += habit.get('total_missed', 0)
    
    # Get recent habit logs (last 30 days)
    thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
    
    # Query logs by user_id or phone
    log_query = {"scheduled_date": {"$gte": thirty_days_ago}}
    if user_phone:
        log_query["$or"] = [
            {"user_id": user_id},
            {"user_phone": user_phone}
        ]
    else:
        log_query["user_id"] = user_id
    
    recent_logs = await db.habit_logs.find(log_query, {"_id": 0}).to_list(1000)
    
    # Build daily completion data for calendar/chart
    daily_data = {}
    for log in recent_logs:
        date = log.get('scheduled_date')
        if date not in daily_data:
            daily_data[date] = {"completed": 0, "missed": 0, "skipped": 0, "total": 0}
        daily_data[date]["total"] += 1
        if log.get('status') == 'completed':
            daily_data[date]["completed"] += 1
        elif log.get('status') == 'missed':
            daily_data[date]["missed"] += 1
        elif log.get('status') == 'skipped':
            daily_data[date]["skipped"] += 1
    
    # Calculate streaks summary
    current_streaks = [h.get('current_streak', 0) for h in habits]
    longest_streaks = [h.get('longest_streak', 0) for h in habits]
    
    return {
        "summary": {
            "total_habits": total_habits,
            "total_completions": total_completions,
            "total_missed": total_missed,
            "overall_completion_rate": overall_completion_rate,
            "total_current_streak": sum(current_streaks),
            "best_current_streak": max(current_streaks) if current_streaks else 0,
            "best_longest_streak": max(longest_streaks) if longest_streaks else 0
        },
        "best_habit": best_habit,
        "worst_habit": worst_habit,
        "category_breakdown": category_stats,
        "daily_data": daily_data,
        "habits": habits
    }


@api_router.get("/habits/{habit_id}/logs")
async def get_habit_logs(
    habit_id: str,
    limit: int = 30,
    current_user: dict = Depends(get_current_user)
):
    """Get logs for a specific habit"""
    user_id = current_user['user_id']
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    user_phone = user.get('phone') if user else None
    
    # Check if habit belongs to user OR is shared with user
    habit = await db.habits.find_one({
        "$and": [
            {"id": habit_id},
            {"$or": [
                {"user_id": user_id},
                {"user_phone": user_phone},
                {"shared_with": user_phone}
            ]}
        ]
    }, {"_id": 0})
    
    if not habit:
        raise HTTPException(status_code=404, detail="Habit not found")
    
    logs = await db.habit_logs.find(
        {"habit_id": habit_id},
        {"_id": 0}
    ).sort("scheduled_date", -1).to_list(limit)
    
    return {"habit": habit, "logs": logs}


@api_router.post("/habits/{habit_id}/share")
async def share_habit(
    habit_id: str,
    phone: str,
    current_user: dict = Depends(get_current_user)
):
    """Share a habit with another user (by phone number)"""
    user_id = current_user['user_id']
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    user_phone = user.get('phone') if user else None
    user_name = user.get('name', 'Someone') if user else 'Someone'
    
    # Verify habit belongs to user
    habit = await db.habits.find_one({
        "id": habit_id,
        "$or": [{"user_id": user_id}, {"user_phone": user_phone}]
    }, {"_id": 0})
    
    if not habit:
        raise HTTPException(status_code=404, detail="Habit not found")
    
    # Clean phone number
    clean_phone = phone.strip()
    if not clean_phone.startswith('+'):
        clean_phone = '+' + clean_phone
    
    # Get current shared list
    current_shared = habit.get('shared_with', []) or []
    if clean_phone not in current_shared:
        current_shared.append(clean_phone)
    
    # Update habit
    await db.habits.update_one(
        {"id": habit_id},
        {"$set": {
            "shared_with": current_shared,
            "is_shared": True,
            "shared_by": user_name,
            "updated_at": serialize_datetime(datetime.now(timezone.utc))
        }}
    )
    
    return {
        "message": f"Habit shared with {clean_phone}",
        "shared_with": current_shared
    }


@api_router.post("/habits/{habit_id}/unshare")
async def unshare_habit(
    habit_id: str,
    phone: str,
    current_user: dict = Depends(get_current_user)
):
    """Remove sharing for a habit"""
    user_id = current_user['user_id']
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    user_phone = user.get('phone') if user else None
    
    habit = await db.habits.find_one({
        "id": habit_id,
        "$or": [{"user_id": user_id}, {"user_phone": user_phone}]
    }, {"_id": 0})
    
    if not habit:
        raise HTTPException(status_code=404, detail="Habit not found")
    
    clean_phone = phone.strip()
    if not clean_phone.startswith('+'):
        clean_phone = '+' + clean_phone
    
    current_shared = habit.get('shared_with', []) or []
    if clean_phone in current_shared:
        current_shared.remove(clean_phone)
    
    await db.habits.update_one(
        {"id": habit_id},
        {"$set": {
            "shared_with": current_shared,
            "is_shared": len(current_shared) > 0,
            "updated_at": serialize_datetime(datetime.now(timezone.utc))
        }}
    )
    
    return {"message": f"Removed sharing with {clean_phone}"}


@api_router.put("/habits/{habit_id}")
async def update_habit(
    habit_id: str,
    updates: dict,
    current_user: dict = Depends(get_current_user)
):
    """Update a habit (tracks who made changes)"""
    user_id = current_user['user_id']
    user = await db.users.find_one({"id": user_id}, {"_id": 0})
    user_phone = user.get('phone') if user else None
    user_name = user.get('name', user_phone or 'Unknown') if user else 'Unknown'
    
    # Check if user owns OR has access to this habit
    habit = await db.habits.find_one({
        "$and": [
            {"id": habit_id},
            {"$or": [
                {"user_id": user_id},
                {"user_phone": user_phone},
                {"shared_with": user_phone}
            ]}
        ]
    }, {"_id": 0})
    
    if not habit:
        raise HTTPException(status_code=404, detail="Habit not found or no access")
    
    # Build edit history entry
    edit_entry = {
        "editor": user_name,
        "editor_phone": user_phone,
        "changes": list(updates.keys()),
        "timestamp": serialize_datetime(datetime.now(timezone.utc))
    }
    
    # Get current edit history
    edit_history = habit.get('edit_history', []) or []
    edit_history.append(edit_entry)
    
    # Keep only last 50 edits
    if len(edit_history) > 50:
        edit_history = edit_history[-50:]
    
    # Prepare update
    allowed_fields = ['name', 'category', 'description', 'frequency', 'time', 'difficulty', 'reminder_intensity', 'status']
    safe_updates = {k: v for k, v in updates.items() if k in allowed_fields}
    
    safe_updates['last_edited_by'] = user_name
    safe_updates['last_edit_description'] = f"Updated: {', '.join(updates.keys())}"
    safe_updates['edit_history'] = edit_history
    safe_updates['updated_at'] = serialize_datetime(datetime.now(timezone.utc))
    
    await db.habits.update_one({"id": habit_id}, {"$set": safe_updates})
    
    return {
        "message": "Habit updated",
        "edited_by": user_name,
        "changes": list(updates.keys())
    }


# ============== TEAM ROUTES ==============

@api_router.post("/teams", response_model=TeamResponse)
async def create_team(
    team_data: TeamCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new team"""
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    team = Team(
        name=team_data.name,
        description=team_data.description,
        owner_id=current_user['user_id'],
        owner_phone=user.get('phone', '')
    )
    
    team_dict = team.model_dump()
    team_dict['created_at'] = serialize_datetime(team_dict['created_at'])
    team_dict['updated_at'] = serialize_datetime(team_dict['updated_at'])
    
    await db.teams.insert_one(team_dict)
    
    # Add owner as a team member with 'owner' role
    owner_member = TeamMember(
        team_id=team.id,
        phone=user.get('phone', ''),
        name=user.get('name', ''),
        role="owner",
        status="approved",
        added_by=user.get('phone', ''),
        approved_by=user.get('phone', ''),
        approved_at=datetime.now(timezone.utc)
    )
    member_dict = owner_member.model_dump()
    member_dict['created_at'] = serialize_datetime(member_dict['created_at'])
    member_dict['updated_at'] = serialize_datetime(member_dict['updated_at'])
    member_dict['approved_at'] = serialize_datetime(member_dict['approved_at'])
    await db.team_members.insert_one(member_dict)
    
    return TeamResponse(
        id=team.id,
        name=team.name,
        description=team.description,
        owner_id=team.owner_id,
        owner_phone=team.owner_phone,
        invite_code=team.invite_code,
        is_active=team.is_active,
        member_count=1,
        created_at=serialize_datetime(team.created_at)
    )


@api_router.get("/teams", response_model=List[TeamResponse])
async def get_teams(current_user: dict = Depends(get_current_user)):
    """Get all teams the user owns or is a member of"""
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    user_phone = user.get('phone', '') if user else ''
    
    # Find teams where user is owner
    owned_teams = await db.teams.find({"owner_id": current_user['user_id']}, {"_id": 0}).to_list(100)
    
    # Find teams where user is a member
    memberships = await db.team_members.find(
        {"phone": user_phone, "status": "approved"},
        {"_id": 0}
    ).to_list(100)
    member_team_ids = [m['team_id'] for m in memberships]
    
    member_teams = []
    if member_team_ids:
        member_teams = await db.teams.find(
            {"id": {"$in": member_team_ids}, "owner_id": {"$ne": current_user['user_id']}},
            {"_id": 0}
        ).to_list(100)
    
    all_teams = owned_teams + member_teams
    
    # Get member counts
    result = []
    for team in all_teams:
        member_count = await db.team_members.count_documents({
            "team_id": team['id'],
            "status": "approved"
        })
        result.append(TeamResponse(
            id=team['id'],
            name=team['name'],
            description=team.get('description'),
            owner_id=team['owner_id'],
            owner_phone=team['owner_phone'],
            invite_code=team['invite_code'],
            is_active=team['is_active'],
            member_count=member_count,
            created_at=team['created_at']
        ))
    
    return result


@api_router.get("/teams/{team_id}", response_model=TeamResponse)
async def get_team(team_id: str, current_user: dict = Depends(get_current_user)):
    """Get team details"""
    team = await db.teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    member_count = await db.team_members.count_documents({
        "team_id": team_id,
        "status": "approved"
    })
    
    return TeamResponse(
        id=team['id'],
        name=team['name'],
        description=team.get('description'),
        owner_id=team['owner_id'],
        owner_phone=team['owner_phone'],
        invite_code=team['invite_code'],
        is_active=team['is_active'],
        member_count=member_count,
        created_at=team['created_at']
    )


@api_router.get("/teams/{team_id}/members", response_model=List[TeamMemberResponse])
async def get_team_members(team_id: str, current_user: dict = Depends(get_current_user)):
    """Get all members of a team"""
    team = await db.teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    members = await db.team_members.find({"team_id": team_id}, {"_id": 0}).to_list(500)
    
    return [TeamMemberResponse(
        id=m['id'],
        team_id=m['team_id'],
        phone=m['phone'],
        name=m.get('name'),
        role=m['role'],
        status=m['status'],
        added_by=m['added_by'],
        approved_by=m.get('approved_by'),
        approved_at=m.get('approved_at'),
        created_at=m['created_at']
    ) for m in members]


@api_router.post("/teams/{team_id}/members", response_model=TeamMemberResponse)
async def add_team_member(
    team_id: str,
    member_data: TeamMemberCreate,
    current_user: dict = Depends(get_current_user)
):
    """Add a member to a team"""
    team = await db.teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Check if current user is owner or admin
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    user_phone = user.get('phone', '') if user else ''
    
    user_membership = await db.team_members.find_one({
        "team_id": team_id,
        "phone": user_phone,
        "role": {"$in": ["owner", "admin"]},
        "status": "approved"
    }, {"_id": 0})
    
    if not user_membership and team['owner_id'] != current_user['user_id']:
        raise HTTPException(status_code=403, detail="Only team owners and admins can add members")
    
    # Check if member already exists
    existing = await db.team_members.find_one({
        "team_id": team_id,
        "phone": member_data.phone
    }, {"_id": 0})
    
    if existing:
        raise HTTPException(status_code=400, detail="Member already exists in this team")
    
    member = TeamMember(
        team_id=team_id,
        phone=member_data.phone,
        name=member_data.name,
        role=member_data.role,
        status="pending",
        added_by=user_phone
    )
    
    member_dict = member.model_dump()
    member_dict['created_at'] = serialize_datetime(member_dict['created_at'])
    member_dict['updated_at'] = serialize_datetime(member_dict['updated_at'])
    
    await db.team_members.insert_one(member_dict)
    
    # Send notification to the new member
    if is_twilio_configured():
        from whatsapp import send_team_join_notification
        await send_team_join_notification(
            member_data.phone,
            team['name'],
            user.get('name', 'Someone'),
            "added"
        )
    
    return TeamMemberResponse(
        id=member.id,
        team_id=member.team_id,
        phone=member.phone,
        name=member.name,
        role=member.role,
        status=member.status,
        added_by=member.added_by,
        approved_by=None,
        approved_at=None,
        created_at=serialize_datetime(member.created_at)
    )


@api_router.put("/teams/{team_id}/members/{member_id}/approve")
async def approve_team_member(
    team_id: str,
    member_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Approve a pending team member"""
    team = await db.teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Check if current user is owner or admin
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    user_phone = user.get('phone', '') if user else ''
    
    user_membership = await db.team_members.find_one({
        "team_id": team_id,
        "phone": user_phone,
        "role": {"$in": ["owner", "admin"]},
        "status": "approved"
    }, {"_id": 0})
    
    if not user_membership and team['owner_id'] != current_user['user_id']:
        raise HTTPException(status_code=403, detail="Only team owners and admins can approve members")
    
    member = await db.team_members.find_one({"id": member_id, "team_id": team_id}, {"_id": 0})
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    
    if member['status'] == 'approved':
        raise HTTPException(status_code=400, detail="Member is already approved")
    
    await db.team_members.update_one(
        {"id": member_id},
        {"$set": {
            "status": "approved",
            "approved_by": user_phone,
            "approved_at": serialize_datetime(datetime.now(timezone.utc)),
            "updated_at": serialize_datetime(datetime.now(timezone.utc))
        }}
    )
    
    # Notify the member they've been approved
    if is_twilio_configured():
        from whatsapp import send_team_member_approved
        await send_team_member_approved(
            member['phone'],
            team['name'],
            user.get('name', 'An admin')
        )
    
    return {"message": "Member approved successfully"}


@api_router.delete("/teams/{team_id}/members/{member_id}")
async def remove_team_member(
    team_id: str,
    member_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Remove a member from a team"""
    team = await db.teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Only owner can remove members
    if team['owner_id'] != current_user['user_id']:
        raise HTTPException(status_code=403, detail="Only team owner can remove members")
    
    member = await db.team_members.find_one({"id": member_id, "team_id": team_id}, {"_id": 0})
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")
    
    if member['role'] == 'owner':
        raise HTTPException(status_code=400, detail="Cannot remove team owner")
    
    await db.team_members.update_one(
        {"id": member_id},
        {"$set": {"status": "removed", "updated_at": serialize_datetime(datetime.now(timezone.utc))}}
    )
    
    return {"message": "Member removed successfully"}


@api_router.get("/teams/join/{invite_code}")
async def join_team_by_code(invite_code: str):
    """Get team info for joining via invite code"""
    team = await db.teams.find_one({"invite_code": invite_code, "is_active": True}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Invalid or expired invite code")
    
    member_count = await db.team_members.count_documents({
        "team_id": team['id'],
        "status": "approved"
    })
    
    return {
        "team_id": team['id'],
        "team_name": team['name'],
        "description": team.get('description'),
        "member_count": member_count
    }


@api_router.post("/teams/join/{invite_code}")
async def join_team(
    invite_code: str,
    phone: str,
    name: Optional[str] = None
):
    """Join a team via invite code (public endpoint for WhatsApp users)"""
    team = await db.teams.find_one({"invite_code": invite_code, "is_active": True}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Invalid or expired invite code")
    
    # Check if already a member
    existing = await db.team_members.find_one({
        "team_id": team['id'],
        "phone": phone
    }, {"_id": 0})
    
    if existing:
        if existing['status'] == 'approved':
            return {"message": "You are already a member of this team", "status": "already_member"}
        elif existing['status'] == 'pending':
            return {"message": "Your membership is pending approval", "status": "pending"}
    
    # Create pending membership
    member = TeamMember(
        team_id=team['id'],
        phone=phone,
        name=name,
        role="member",
        status="pending",
        added_by=phone  # Self-added via invite
    )
    
    member_dict = member.model_dump()
    member_dict['created_at'] = serialize_datetime(member_dict['created_at'])
    member_dict['updated_at'] = serialize_datetime(member_dict['updated_at'])
    
    await db.team_members.insert_one(member_dict)
    
    # Notify team owner about new join request
    if is_twilio_configured() and team.get('owner_phone'):
        await send_whatsapp_message(
            team['owner_phone'],
            f"🌼 New join request!\n\n{name or phone} wants to join your team \"{team['name']}\".\n\nReply \"Approve {phone}\" to approve them.\n\n- Daisy"
        )
    
    return {"message": "Join request submitted. Waiting for admin approval.", "status": "pending"}


# ============== TEAM REMINDER ROUTES ==============

@api_router.post("/teams/{team_id}/reminders", response_model=TeamReminderResponse)
async def create_team_reminder(
    team_id: str,
    reminder_data: TeamReminderCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a reminder for all team members"""
    team = await db.teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    # Check if user is owner or admin
    user = await db.users.find_one({"id": current_user['user_id']}, {"_id": 0})
    user_phone = user.get('phone', '') if user else ''
    
    user_membership = await db.team_members.find_one({
        "team_id": team_id,
        "phone": user_phone,
        "role": {"$in": ["owner", "admin"]},
        "status": "approved"
    }, {"_id": 0})
    
    if not user_membership and team['owner_id'] != current_user['user_id']:
        raise HTTPException(status_code=403, detail="Only team owners and admins can create team reminders")
    
    # Get all approved members
    members = await db.team_members.find({
        "team_id": team_id,
        "status": "approved"
    }, {"_id": 0}).to_list(500)
    
    if len(members) == 0:
        raise HTTPException(status_code=400, detail="Team has no approved members")
    
    # Create the team reminder
    team_reminder = TeamReminder(
        team_id=team_id,
        team_name=team['name'],
        creator_id=current_user['user_id'],
        creator_phone=user_phone,
        message=reminder_data.message,
        scheduled_time=reminder_data.scheduled_time,
        recurrence=reminder_data.recurrence,
        end_date=reminder_data.end_date,
        persist_until_all_acknowledge=reminder_data.persist_until_all_acknowledge,
        total_members=len(members)
    )
    
    reminder_dict = team_reminder.model_dump()
    reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
    reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
    reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
    if reminder_dict.get('end_date'):
        reminder_dict['end_date'] = serialize_datetime(reminder_dict['end_date'])
    
    await db.team_reminders.insert_one(reminder_dict)
    
    # Create individual acknowledgment records for each member
    for member in members:
        ack = TeamReminderAcknowledgment(
            team_reminder_id=team_reminder.id,
            member_phone=member['phone'],
            member_name=member.get('name')
        )
        ack_dict = ack.model_dump()
        ack_dict['created_at'] = serialize_datetime(ack_dict['created_at'])
        await db.team_reminder_acks.insert_one(ack_dict)
    
    return TeamReminderResponse(
        id=team_reminder.id,
        team_id=team_reminder.team_id,
        team_name=team_reminder.team_name,
        message=team_reminder.message,
        scheduled_time=serialize_datetime(team_reminder.scheduled_time),
        recurrence=team_reminder.recurrence,
        status=team_reminder.status,
        total_members=team_reminder.total_members,
        acknowledged_count=0,
        persist_until_all_acknowledge=team_reminder.persist_until_all_acknowledge,
        created_at=serialize_datetime(team_reminder.created_at)
    )


@api_router.get("/teams/{team_id}/reminders", response_model=List[TeamReminderResponse])
async def get_team_reminders(team_id: str, current_user: dict = Depends(get_current_user)):
    """Get all reminders for a team"""
    team = await db.teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    
    reminders = await db.team_reminders.find({"team_id": team_id}, {"_id": 0}).sort("scheduled_time", -1).to_list(100)
    
    return [TeamReminderResponse(
        id=r['id'],
        team_id=r['team_id'],
        team_name=r['team_name'],
        message=r['message'],
        scheduled_time=r['scheduled_time'],
        recurrence=r['recurrence'],
        status=r['status'],
        total_members=r['total_members'],
        acknowledged_count=r.get('acknowledged_count', 0),
        persist_until_all_acknowledge=r.get('persist_until_all_acknowledge', True),
        created_at=r['created_at']
    ) for r in reminders]


# ============== WHATSAPP WEBHOOK ==============

@api_router.post("/webhook/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(""),
    MessageSid: Optional[str] = Form(None),
    AccountSid: Optional[str] = Form(None),
    NumMedia: Optional[str] = Form("0"),
    MediaContentType0: Optional[str] = Form(None),
    MediaUrl0: Optional[str] = Form(None),
    # Interactive button response parameters
    ButtonText: Optional[str] = Form(None),
    ButtonPayload: Optional[str] = Form(None)
):
    """Handle incoming WhatsApp messages from Twilio"""
    logger.info(f"Received WhatsApp message from {From}: {Body}")
    logger.info(f"Media info: NumMedia={NumMedia}, ContentType={MediaContentType0}, URL={MediaUrl0}")
    
    # Check for button click responses
    if ButtonText or ButtonPayload:
        logger.info(f"Button clicked - Text: {ButtonText}, Payload: {ButtonPayload}")
        # Use button payload or text as the message body for processing
        if ButtonPayload:
            Body = ButtonPayload  # e.g., "done", "later", "skip"
        elif ButtonText:
            Body = ButtonText    # e.g., "Done ✅", "Later ⏰", "Skip ⏭️"
    
    # Clean and normalize phone numbers
    def normalize_phone(phone):
        """Normalize phone number - remove whatsapp prefix, ensure + prefix"""
        phone = phone.replace("whatsapp:", "").strip()
        phone = phone.replace(" ", "").replace("-", "")
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        return phone
    
    from_phone = normalize_phone(From)
    to_phone = normalize_phone(To)
    
    # Handle contact card (vCard) attachments
    extracted_phone = None
    extracted_name = None
    if NumMedia and int(NumMedia) > 0 and MediaContentType0 and MediaUrl0:
        if 'vcard' in MediaContentType0.lower() or 'x-vcard' in MediaContentType0.lower() or 'text/x-vcard' in MediaContentType0.lower():
            # Download and parse the vCard using vobject library
            try:
                twilio_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                twilio_token = os.environ.get("TWILIO_AUTH_TOKEN")
                
                # Build proper media URL
                media_url = MediaUrl0
                if not media_url.startswith('http'):
                    media_url = f"https://api.twilio.com{MediaUrl0}"
                
                logger.info(f"Downloading vCard from: {media_url}")
                
                async with httpx.AsyncClient() as http_client:
                    response = await http_client.get(media_url, auth=(twilio_sid, twilio_token), follow_redirects=True)
                    vcard_content = response.text
                    logger.info(f"vCard content received: {vcard_content[:500]}")
                    
                    # Parse vCard using vobject library
                    try:
                        vcard = vobject.readOne(vcard_content)
                        
                        # Extract phone number(s) from vCard
                        if hasattr(vcard, 'tel'):
                            # vobject can have multiple tel entries
                            tel_value = vcard.tel.value if hasattr(vcard.tel, 'value') else str(vcard.tel)
                            # Clean the phone number - keep only digits and +
                            extracted_phone = re.sub(r'[^\d+]', '', tel_value)
                            # Ensure it has + prefix
                            if extracted_phone and not extracted_phone.startswith('+'):
                                extracted_phone = '+' + extracted_phone
                            logger.info(f"Extracted phone from vCard (vobject): {extracted_phone}")
                        
                        # Extract name from vCard
                        if hasattr(vcard, 'fn'):
                            extracted_name = vcard.fn.value.strip()
                            logger.info(f"Extracted name from vCard (vobject): {extracted_name}")
                        elif hasattr(vcard, 'n'):
                            # N field has structured name (family, given, etc.)
                            n_val = vcard.n.value
                            extracted_name = f"{n_val.given or ''} {n_val.family or ''}".strip()
                            logger.info(f"Extracted name from vCard N field: {extracted_name}")
                            
                    except Exception as vobj_error:
                        logger.warning(f"vobject parsing failed, trying regex: {vobj_error}")
                        # Fallback to regex parsing
                        tel_patterns = [
                            r'TEL[;:][^:]*:([+\d\s\-()]+)',
                            r'TEL:([+\d\s\-()]+)',
                        ]
                        for pattern in tel_patterns:
                            tel_match = re.search(pattern, vcard_content, re.IGNORECASE)
                            if tel_match:
                                extracted_phone = re.sub(r'[^\d+]', '', tel_match.group(1))
                                if not extracted_phone.startswith('+'):
                                    extracted_phone = '+' + extracted_phone
                                if len(extracted_phone) >= 10:
                                    logger.info(f"Extracted phone from vCard (regex): {extracted_phone}")
                                    break
                        
                        name_match = re.search(r'FN:(.+?)(?:\r|\n|$)', vcard_content, re.IGNORECASE)
                        if name_match:
                            extracted_name = name_match.group(1).strip()
                    
                    # If we extracted a phone, update the Body and context
                    if extracted_phone and len(extracted_phone) >= 10:
                        Body = extracted_phone
                        logger.info(f"Using extracted phone as Body: {Body}")
                        
                        # Also store extracted name for later use if available
                        if extracted_name:
                            logger.info(f"Contact name from vCard: {extracted_name}")
                        
            except Exception as e:
                logger.error(f"Error parsing vCard: {e}")
                import traceback
                logger.error(traceback.format_exc())
    
    # ============== VOICE NOTE HANDLING ==============
    # Check if this is a voice note (audio message)
    voice_transcription = None
    voice_intent = None
    
    if NumMedia and int(NumMedia) > 0 and MediaContentType0:
        content_type = MediaContentType0.lower()
        # WhatsApp voice notes are typically audio/ogg or audio/mpeg
        if 'audio' in content_type or 'ogg' in content_type:
            logger.info(f"Voice note detected: {content_type}")
            
            try:
                from voice_processor import process_voice_note, is_voice_command_for_reminder
                
                # Get Twilio credentials for downloading media
                twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
                twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
                
                if MediaUrl0 and twilio_account_sid and twilio_auth_token:
                    # Process the voice note
                    voice_transcription, voice_intent = await process_voice_note(
                        media_url=MediaUrl0,
                        twilio_account_sid=twilio_account_sid,
                        twilio_auth_token=twilio_auth_token
                    )
                    
                    if voice_transcription:
                        logger.info(f"Voice transcription: '{voice_transcription}'")
                        logger.info(f"Voice intent: {voice_intent}")
                        
                        # Use transcription as the message body for AI processing
                        Body = voice_transcription
                        
                        # Store transcription in database for logging
                        await db.voice_logs.insert_one({
                            "phone": from_phone,
                            "media_url": MediaUrl0,
                            "transcription": voice_transcription,
                            "intent": voice_intent,
                            "created_at": serialize_datetime(datetime.now(timezone.utc))
                        })
                    else:
                        logger.warning("Failed to transcribe voice note")
                        # Send helpful message if transcription failed
                        await send_whatsapp_message(
                            from_phone,
                            "I couldn't quite hear that voice message clearly. 🎙️ Could you try again or type your message instead? 💛\n\n— Daisy"
                        )
                        return ""
                        
            except Exception as e:
                logger.error(f"Error processing voice note: {e}")
                import traceback
                logger.error(traceback.format_exc())
    
    # Store incoming message
    message = Message(
        direction="incoming",
        from_phone=from_phone,
        to_phone=to_phone,
        content=Body or "(contact shared)",
        twilio_sid=MessageSid
    )
    message_dict = message.model_dump()
    message_dict['created_at'] = serialize_datetime(message_dict['created_at'])
    await db.messages.insert_one(message_dict)
    
    # ============== USER ONBOARDING & CONSENT FLOW ==============
    # Check WhatsApp user status first
    wa_user = await db.whatsapp_users.find_one({"phone": from_phone}, {"_id": 0})
    
    # Privacy Policy and Consent Message - Warm, Family-Focused
    PRIVACY_CONSENT_MESSAGE = """🌼 *Hello! I'm Daisy* — your AI-powered life concierge.

*Important:* I'm an artificial intelligence (AI), not a human. I'm operated by *Daisy Can Handle It Pty Ltd*, an Australian business.

*What I collect to help you:*
• Your name and phone number
• Messages you send me
• Reminders and preferences you set
• Contact details for people you want to remind

*Why I collect this:*
To provide you with reminder and habit tracking services via WhatsApp.

*Data processing:*
Your data may be processed by AI providers in the USA (OpenAI) and stored securely on cloud servers. See our full Privacy Policy for details.

*Your rights:*
• Access, correct, or delete your data anytime
• Opt out by replying *STOP* to any message
• Lodge a complaint with the OAIC if needed

📄 *Privacy Policy:* https://daisycanhandleit.com/privacy
📄 *Terms & Conditions:* https://daisycanhandleit.com/terms

*To continue:*
Reply *AGREE* to accept these terms and start using Daisy.
Reply *DECLINE* if you'd prefer not to.

By continuing, you consent to these terms. 💛"""

    RECIPIENT_CONSENT_UPGRADE_MESSAGE = """🌼 *Hi there!*

I've noticed you've been receiving caring reminders from your family and friends through me - and I'm so happy I could help keep you connected!

*Note:* I'm Daisy, an AI assistant (not a human), operated by Daisy Can Handle It Pty Ltd.

Would you like to use me yourself to:
• 💛 Send gentle reminders to YOUR loved ones
• 🎯 Build healthy habits (like daily walks or meditation)
• 👨‍👩‍👧 Help care for your family the way they care for you

*Start your FREE 30-day trial* by replying *START TRIAL*

Or keep receiving reminders for free - I'm always here either way! 🌸

📄 Privacy: https://daisycanhandleit.com/privacy"""

    # Helper to detect if message is a recipient response vs user intent
    def is_recipient_response(message_text):
        """Check if this looks like a response to a reminder vs wanting to use Daisy"""
        recipient_patterns = [
            r'^(ok|okay|done|got it|noted|thanks|thank you|yes|no|sure|will do|on it)[\s!.]*$',
            r'^(acknowledged|received|confirm|confirmed)[\s!.]*$',
            r'^(snooze|skip|later|remind me later)[\s!.]*$',
        ]
        text_lower = message_text.lower().strip()
        for pattern in recipient_patterns:
            if re.match(pattern, text_lower, re.IGNORECASE):
                return True
        return False
    
    def is_user_intent(message_text):
        """Check if this looks like someone wanting to USE Daisy"""
        user_intent_patterns = [
            r'remind me',
            r'set (a )?reminder',
            r'create (a )?habit',
            r'i want to (use|try|start)',
            r'start (my )?(free )?trial',
            r'how (do i|can i|to) use',
            r'what can you do',
            r'help me with',
            r'sign (me )?up',
            r'register',
        ]
        text_lower = message_text.lower().strip()
        for pattern in user_intent_patterns:
            if re.search(pattern, text_lower):
                return True
        return False

    # CASE 1: Brand new user (never interacted with Daisy)
    if not wa_user:
        # Check if they're a known recipient (someone set reminders for them)
        is_known_recipient = await db.contacts.find_one({"phone": from_phone}, {"_id": 0})
        
        if is_known_recipient:
            # They're a recipient - create profile as recipient_only
            new_wa_user = {
                "id": str(uuid.uuid4()),
                "phone": from_phone,
                "name": is_known_recipient.get('name'),
                "user_type": "recipient_only",
                "privacy_consent_accepted": False,
                "subscription_status": "none",
                "first_interaction": serialize_datetime(datetime.now(timezone.utc)),
                "last_interaction": serialize_datetime(datetime.now(timezone.utc)),
                "total_messages_sent": 1,
                "total_reminders_received": 0,
                "total_reminders_created": 0,
                "created_at": serialize_datetime(datetime.now(timezone.utc)),
                "updated_at": serialize_datetime(datetime.now(timezone.utc))
            }
            await db.whatsapp_users.insert_one(new_wa_user)
            wa_user = new_wa_user
            
            # Check if they want to USE Daisy or just responding
            if is_user_intent(Body):
                # They want to use Daisy - send upgrade message
                await send_whatsapp_message(from_phone, RECIPIENT_CONSENT_UPGRADE_MESSAGE)
                return ""
            # Otherwise, continue to normal processing (likely responding to reminder)
        else:
            # Completely new user - send privacy consent
            new_wa_user = {
                "id": str(uuid.uuid4()),
                "phone": from_phone,
                "name": None,
                "user_type": "pending_consent",
                "privacy_consent_accepted": False,
                "subscription_status": "none",
                "first_interaction": serialize_datetime(datetime.now(timezone.utc)),
                "last_interaction": serialize_datetime(datetime.now(timezone.utc)),
                "total_messages_sent": 1,
                "total_reminders_received": 0,
                "total_reminders_created": 0,
                "created_at": serialize_datetime(datetime.now(timezone.utc)),
                "updated_at": serialize_datetime(datetime.now(timezone.utc))
            }
            await db.whatsapp_users.insert_one(new_wa_user)
            
            # Send privacy consent message
            if is_twilio_configured():
                await send_whatsapp_message(from_phone, PRIVACY_CONSENT_MESSAGE)
            return ""
    
    # CASE 2: User exists but hasn't accepted privacy consent yet
    elif wa_user.get('user_type') == 'pending_consent':
        body_lower = Body.lower().strip()
        
        if body_lower in ['agree', 'i agree', 'yes', 'accept']:
            # User accepted - start their trial
            trial_end = datetime.now(timezone.utc) + timedelta(days=30)
            await db.whatsapp_users.update_one(
                {"phone": from_phone},
                {"$set": {
                    "user_type": "active_user",
                    "privacy_consent_accepted": True,
                    "privacy_consent_date": serialize_datetime(datetime.now(timezone.utc)),
                    "data_storage_consent": True,
                    "subscription_status": "trial",
                    "trial_start": serialize_datetime(datetime.now(timezone.utc)),
                    "trial_end": serialize_datetime(trial_end),
                    "updated_at": serialize_datetime(datetime.now(timezone.utc))
                }}
            )
            
            welcome_msg = """🎉 *Welcome to the family!*

I'm so happy to have you here! Your *30-day free trial* has started, and I'm ready to help you take care of the people who matter most.

*Here's what we can do together:*

💭 *Care for loved ones:*
"Remind my dad to take his medicine at 8am"
"Check in with mom every Sunday"

🎯 *Build healthy habits:*
"I want to meditate every morning at 7am"
"Help me drink more water daily"

⏰ *Never forget:*
"Remind me to call my sister tomorrow"
"Set a reminder for my anniversary"

🎙️ *Voice Messages:*
You can send me voice notes too! I understand English and Hindi. Just say "Done", "Later", or "Skip" to respond to reminders.

☀️ *Daily Summaries (My Special Touch!):*
I can send you a *Morning Agenda* with your day's tasks and an *Evening Wrap-up* of what got done. Just say:
• "Set up my morning agenda at 7am"
• "Send me evening wrap-up at 9pm"

*First things first - what's your name?* 
(So I can make our conversations more personal 💛)"""
            
            if is_twilio_configured():
                await send_whatsapp_message(from_phone, welcome_msg)
            return ""
            
        elif body_lower in ['decline', 'no', 'reject']:
            await db.whatsapp_users.update_one(
                {"phone": from_phone},
                {"$set": {
                    "user_type": "declined",
                    "updated_at": serialize_datetime(datetime.now(timezone.utc))
                }}
            )
            
            decline_msg = """I completely understand - no pressure at all! 💛

Your privacy and comfort come first. If you ever change your mind or just want to chat, I'll be right here waiting.

Take care of yourself and your loved ones! 🌼"""
            
            if is_twilio_configured():
                await send_whatsapp_message(from_phone, decline_msg)
            return ""
        else:
            # They sent something else - remind them to accept
            reminder_msg = """I'd love to help you care for your family! 💛

Just reply *AGREE* to get started, or *DECLINE* if you'd prefer not to.

Take your time - I'll be here when you're ready! 🌼"""
            if is_twilio_configured():
                await send_whatsapp_message(from_phone, reminder_msg)
            return ""
    
    # CASE 3: Recipient-only user trying to use Daisy features
    elif wa_user.get('user_type') == 'recipient_only':
        body_lower = Body.lower().strip()
        
        # Check if they're responding to upgrade prompt
        if body_lower in ['start trial', 'start my trial', 'yes', 'start']:
            # Send them the full privacy consent before upgrading
            await db.whatsapp_users.update_one(
                {"phone": from_phone},
                {"$set": {
                    "user_type": "pending_consent",
                    "updated_at": serialize_datetime(datetime.now(timezone.utc))
                }}
            )
            if is_twilio_configured():
                await send_whatsapp_message(from_phone, PRIVACY_CONSENT_MESSAGE)
            return ""
        
        # Check if they want to use Daisy (not just responding to a reminder)
        if is_user_intent(Body) and not is_recipient_response(Body):
            if is_twilio_configured():
                await send_whatsapp_message(from_phone, RECIPIENT_CONSENT_UPGRADE_MESSAGE)
            return ""
        
        # Otherwise, continue to normal processing (they're likely responding to a reminder)
    
    # CASE 4: User declined previously but is messaging again
    elif wa_user.get('user_type') == 'declined':
        # Give them another chance
        await db.whatsapp_users.update_one(
            {"phone": from_phone},
            {"$set": {
                "user_type": "pending_consent",
                "updated_at": serialize_datetime(datetime.now(timezone.utc))
            }}
        )
        if is_twilio_configured():
            await send_whatsapp_message(from_phone, PRIVACY_CONSENT_MESSAGE)
        return ""
    
    # Update last interaction for active users
    if wa_user and wa_user.get('user_type') == 'active_user':
        await db.whatsapp_users.update_one(
            {"phone": from_phone},
            {"$set": {
                "last_interaction": serialize_datetime(datetime.now(timezone.utc)),
                "updated_at": serialize_datetime(datetime.now(timezone.utc))
            },
            "$inc": {"total_messages_sent": 1}}
        )
    
    # ============== END ONBOARDING FLOW ==============
    
    # Check if user exists
    user = await db.users.find_one({"phone": from_phone}, {"_id": 0})
    
    # Get user's name - prioritize WhatsApp profile name (most recently updated)
    user_name = "Someone"
    whatsapp_user = await db.whatsapp_users.find_one({"phone": from_phone}, {"_id": 0})
    
    if whatsapp_user and whatsapp_user.get('name'):
        # WhatsApp profile name takes priority (user may have updated it via chat)
        user_name = whatsapp_user.get('name')
    elif user and user.get('name'):
        # Fall back to web user name
        user_name = user.get('name', 'Someone')
    
    # Get user's existing contacts to help AI recognize them
    user_id = user['id'] if user else f"whatsapp_{from_phone}"
    existing_contacts = await db.contacts.find(
        {"user_id": user_id},
        {"_id": 0, "name": 1, "phone": 1, "consent_status": 1}
    ).to_list(100)
    
    # Build contacts context for AI
    contacts_context = {}
    for contact in existing_contacts:
        contact_name = contact.get('name', '').lower()
        contacts_context[contact_name] = {
            "phone": contact.get('phone'),
            "consent_status": contact.get('consent_status')
        }
    
    # Parse the message with AI (pass user phone and contacts context)
    # BUT if we have a clear voice intent, use that directly instead of AI parsing
    
    if voice_intent and voice_intent.get('intent') in ['completed', 'defer', 'snooze', 'skip']:
        # Map voice intent to Daisy's intent system
        voice_intent_mapping = {
            'completed': 'acknowledge',
            'defer': 'snooze_reminder',  # "later" maps to snooze
            'snooze': 'snooze_reminder',
            'skip': 'skip_reminder'
        }
        
        parsed = {
            'intent': voice_intent_mapping.get(voice_intent['intent'], 'acknowledge'),
            'confidence': voice_intent.get('confidence', 0.9),
            'from_voice': True,
            'transcription': voice_transcription,
            'friendly_response': None  # Will be generated based on intent
        }
        logger.info(f"Using voice intent directly: {parsed}")
    else:
        # Use AI to parse the message
        parsed = await parse_user_message(
            Body, 
            user_phone=from_phone, 
            user_context={
                "user_name": user_name,
                "contacts": contacts_context
            }
        )
    logger.info(f"Parsed intent: {parsed}")
    
    # Handle different intents
    response_text = parsed.get('friendly_response', '')
    
    if parsed.get('intent') == 'consent_response':
        # Handle consent response
        contact = await db.contacts.find_one({"phone": from_phone}, {"_id": 0})
        if contact:
            if parsed.get('consent'):
                # Update consent status
                await db.contacts.update_one(
                    {"phone": from_phone},
                    {"$set": {
                        "consent_status": "approved",
                        "consent_date": serialize_datetime(datetime.now(timezone.utc))
                    }}
                )
                
                # Activate any pending reminders for this contact
                await db.reminders.update_many(
                    {"recipient_phone": from_phone, "status": "awaiting_consent"},
                    {"$set": {"status": "pending"}}
                )
                
                response_text = parsed.get('friendly_response', "Thank you for approving! 🌼 I'll start sending you reminders as scheduled. You can reply STOP anytime to opt out.")
                
                # Notify the creator that consent was given
                if contact.get('user_id'):
                    creator = await db.users.find_one({"id": contact['user_id']}, {"_id": 0})
                    if creator and creator.get('phone') and is_twilio_configured():
                        await send_whatsapp_message(
                            creator['phone'],
                            f"Great news! 🌼 {contact.get('name', 'Your contact')} has approved reminders. I'll start sending them as scheduled!"
                        )
            else:
                await db.contacts.update_one(
                    {"phone": from_phone},
                    {"$set": {"consent_status": "declined"}}
                )
                
                # Cancel any pending reminders for this contact
                await db.reminders.update_many(
                    {"recipient_phone": from_phone, "status": "awaiting_consent"},
                    {"$set": {"status": "cancelled"}}
                )
                
                response_text = parsed.get('friendly_response', "No problem! I won't send you any reminders. 🌼")
                
                # Notify the creator that consent was declined
                if contact.get('user_id'):
                    creator = await db.users.find_one({"id": contact['user_id']}, {"_id": 0})
                    if creator and creator.get('phone') and is_twilio_configured():
                        await send_whatsapp_message(
                            creator['phone'],
                            f"Unfortunately, {contact.get('name', 'your contact')} has declined reminder notifications. 🌼"
                        )
        else:
            response_text = parsed.get('friendly_response', "Hi! I'm Daisy. How can I help you today? 🌼")
    
    elif parsed.get('intent') == 'set_name':
        # User wants to set their name
        new_name = parsed.get('user_name', '').strip()
        if new_name:
            now_str = serialize_datetime(datetime.now(timezone.utc))
            
            # Store in whatsapp_users collection
            await db.whatsapp_users.update_one(
                {"phone": from_phone},
                {"$set": {
                    "phone": from_phone,
                    "name": new_name,
                    "updated_at": now_str
                }},
                upsert=True
            )
            
            # Also update the web users collection if they have an account
            await db.users.update_one(
                {"phone": from_phone},
                {"$set": {"name": new_name, "updated_at": now_str}}
            )
            
            response_text = parsed.get('friendly_response', f"Nice to meet you, {new_name}! 🌼 I'll remember your name from now on.")
            logger.info(f"Stored user name '{new_name}' for phone {from_phone}")
        else:
            response_text = "I didn't catch your name. Could you tell me again? 🌼"
    
    elif parsed.get('intent') == 'acknowledge':
        # Handle acknowledgment - check individual, team, and multi-time reminders
        # IMPORTANT: Only acknowledge reminders that have ACTUALLY BEEN SENT
        # "Thanks" after setting a reminder should NOT count as acknowledgment
        
        found_sent_reminder = False
        now = datetime.now(timezone.utc)
        
        # First check for individual reminders (only status="sent" - already delivered)
        reminder = await db.reminders.find_one(
            {"recipient_phone": from_phone, "status": "sent"},
            {"_id": 0},
            sort=[("last_sent_at", -1)]
        )
        if reminder:
            found_sent_reminder = True
            is_self_reminder = reminder.get('recipient_name') == 'self' or reminder.get('recipient_phone') == reminder.get('creator_phone')
            
            await db.reminders.update_one(
                {"id": reminder['id']},
                {"$set": {
                    "status": "acknowledged",
                    "acknowledgment": Body,
                    "acknowledged_at": serialize_datetime(now),
                    "completed": True,
                    "completed_at": serialize_datetime(now)
                }}
            )
            
            # Notify the creator (if not self-reminder)
            if not is_self_reminder:
                creator_phone = reminder.get('creator_phone')
                if not creator_phone:
                    creator = await db.users.find_one({"id": reminder['creator_id']}, {"_id": 0})
                    creator_phone = creator.get('phone') if creator else None
                
                if creator_phone and is_twilio_configured() and creator_phone != from_phone:
                    recipient_name = reminder.get('recipient_name', 'Your contact')
                    recipient_relationship = reminder.get('recipient_relationship')
                    
                    # Use the warm notification from scheduler
                    from scheduler import notify_creator_of_completion
                    await notify_creator_of_completion(
                        creator_phone=creator_phone,
                        recipient_name=recipient_name,
                        reminder_message=reminder['message'],
                        recipient_relationship=recipient_relationship
                    )
        
        # Check for multi-time reminders - ONLY if at least one reminder time has been SENT
        multi_reminder = await db.multi_time_reminders.find_one(
            {"recipient_phone": from_phone, "status": "active"},
            {"_id": 0},
            sort=[("created_at", -1)]
        )
        if multi_reminder:
            # Check if ANY reminder time has been sent
            reminder_times = multi_reminder.get('reminder_times', [])
            any_sent = any(rt.get('status') == 'sent' for rt in reminder_times)
            
            if any_sent:
                # At least one reminder was sent - this is a valid acknowledgment
                found_sent_reminder = True
                await db.multi_time_reminders.update_one(
                    {"id": multi_reminder['id']},
                    {"$set": {
                        "status": "acknowledged",
                        "acknowledgment": Body,
                        "acknowledged_at": serialize_datetime(datetime.now(timezone.utc)),
                        "updated_at": serialize_datetime(datetime.now(timezone.utc))
                    }}
                )
                
                # Notify the creator (only if it's not a self-reminder)
                if is_twilio_configured() and multi_reminder.get('creator_phone'):
                    recipient_name = multi_reminder.get('recipient_name', from_phone)
                    if recipient_name != 'self' and multi_reminder['creator_phone'] != from_phone:
                        await send_whatsapp_message(
                            multi_reminder['creator_phone'],
                            f"🌼 Great news! {recipient_name} has confirmed:\n\n\"{multi_reminder['message']}\"\n\nThey replied: \"{Body}\"\n\nNo more reminders will be sent for this task. ✅"
                        )
                
                logger.info(f"Multi-time reminder {multi_reminder['id']} acknowledged by {from_phone}")
            else:
                # No reminders sent yet - this is NOT a valid acknowledgment
                # It's probably just "thanks" after setting the reminder
                logger.info(f"Ignoring acknowledgment for multi_reminder {multi_reminder['id']} - no reminders sent yet")
        
        # Also check for team reminder acknowledgments (only status="sent")
        team_ack = await db.team_reminder_acks.find_one(
            {"member_phone": from_phone, "status": "sent"},
            {"_id": 0},
            sort=[("last_sent_at", -1)]
        )
        if team_ack:
            await db.team_reminder_acks.update_one(
                {"id": team_ack['id']},
                {"$set": {
                    "status": "acknowledged",
                    "acknowledgment_text": Body,
                    "acknowledged_at": serialize_datetime(datetime.now(timezone.utc))
                }}
            )
            
            # Update the team reminder acknowledged count
            team_reminder = await db.team_reminders.find_one({"id": team_ack['team_reminder_id']}, {"_id": 0})
            if team_reminder:
                ack_count = await db.team_reminder_acks.count_documents({
                    "team_reminder_id": team_reminder['id'],
                    "status": "acknowledged"
                })
                
                await db.team_reminders.update_one(
                    {"id": team_reminder['id']},
                    {"$set": {"acknowledged_count": ack_count}}
                )
                
                # Notify creator about progress
                if is_twilio_configured():
                    member_name = team_ack.get('member_name', from_phone)
                    await send_whatsapp_message(
                        team_reminder['creator_phone'],
                        f"🌼 {member_name} acknowledged the team reminder for {team_reminder['team_name']}!\n\nProgress: {ack_count}/{team_reminder['total_members']} members"
                    )
                    
                    # If all acknowledged, mark as complete
                    if ack_count >= team_reminder['total_members']:
                        await db.team_reminders.update_one(
                            {"id": team_reminder['id']},
                            {"$set": {"status": "completed", "updated_at": serialize_datetime(datetime.now(timezone.utc))}}
                        )
                        from whatsapp import send_team_reminder_progress
                        await send_team_reminder_progress(
                            team_reminder['creator_phone'],
                            team_reminder['team_name'],
                            team_reminder['message'],
                            ack_count,
                            team_reminder['total_members']
                        )
            found_sent_reminder = True
        
        # Respond based on whether we found a sent reminder to acknowledge
        if found_sent_reminder:
            # If this was a voice response, give a more natural acknowledgment
            if parsed.get('from_voice'):
                voice_responses = [
                    "Got it! I heard you - marked as done! ✅💛",
                    "Perfect! I've noted that it's done. Great job! 🌼",
                    "Wonderful! Marked as complete. Keep it up! 💛",
                    "Done and dusted! You're on top of things! ✅🌼"
                ]
                import random
                response_text = random.choice(voice_responses)
            else:
                response_text = parsed.get('friendly_response', "Great! I've noted that. 🌼")
        else:
            # No sent reminder found - this is probably just a polite "thanks"
            # Treat it as general conversation, not an acknowledgment
            response_text = "You're welcome! 🌼 Let me know if you need anything else."
    
    # ============== SMART MESSAGING SYSTEM - SNOOZE HANDLER ==============
    elif parsed.get('intent') == 'snooze_reminder':
        # User wants to snooze a reminder (replied "Later" / "2" / "Remind me in 10 minutes" / voice "baad mein")
        snooze_minutes = parsed.get('snooze_minutes', 10)
        now = datetime.now(timezone.utc)
        snooze_until = now + timedelta(minutes=snooze_minutes)
        
        found_reminder = False
        
        # Find the most recent sent reminder to this user
        reminder = await db.reminders.find_one(
            {"recipient_phone": from_phone, "status": "sent"},
            {"_id": 0},
            sort=[("last_sent_at", -1)]
        )
        
        if reminder:
            found_reminder = True
            # Update the reminder to snooze
            await db.reminders.update_one(
                {"id": reminder['id']},
                {"$set": {
                    "status": "snoozed",
                    "snoozed_until": serialize_datetime(snooze_until),
                    "scheduled_time": serialize_datetime(snooze_until),  # Reschedule
                    "updated_at": serialize_datetime(now)
                }}
            )
            
            # After snooze time, it will be picked up again as "pending"
            # Actually set it back to pending so scheduler picks it up
            await db.reminders.update_one(
                {"id": reminder['id']},
                {"$set": {"status": "pending"}}
            )
            
            # Voice-specific response
            if parsed.get('from_voice'):
                response_text = f"No worries! I heard you. I'll check back in {snooze_minutes} minutes. 💛"
            else:
                response_text = f"No problem! I'll remind you again in {snooze_minutes} minutes. 💛"
            logger.info(f"Reminder {reminder['id']} snoozed for {snooze_minutes} minutes")
        
        # Also check multi-time reminders
        if not found_reminder:
            multi_reminder = await db.multi_time_reminders.find_one(
                {"recipient_phone": from_phone, "status": "active"},
                {"_id": 0},
                sort=[("updated_at", -1)]
            )
            
            if multi_reminder:
                # Check if any reminders were sent
                reminder_times = multi_reminder.get('reminder_times', [])
                any_sent = any(rt.get('status') == 'sent' for rt in reminder_times)
                
                if any_sent:
                    found_reminder = True
                    # Add a new snooze time
                    reminder_times.append({
                        "time": serialize_datetime(snooze_until),
                        "label": f"snoozed +{snooze_minutes}min",
                        "status": "pending"
                    })
                    
                    await db.multi_time_reminders.update_one(
                        {"id": multi_reminder['id']},
                        {"$set": {
                            "reminder_times": reminder_times,
                            "updated_at": serialize_datetime(now)
                        }}
                    )
                    
                    response_text = f"Got it! I'll check back in {snooze_minutes} minutes. 💛"
                    logger.info(f"Multi-time reminder {multi_reminder['id']} snoozed")
        
        if not found_reminder:
            response_text = "I don't have any active reminders for you right now. Let me know if you need anything! 🌼"
    
    # ============== SMART MESSAGING SYSTEM - SKIP HANDLER ==============
    elif parsed.get('intent') == 'skip_reminder':
        # User wants to skip a reminder (replied "Skip" / "3" / "Not now" / voice "chhod do")
        now = datetime.now(timezone.utc)
        
        found_reminder = False
        
        # Find the most recent sent reminder to this user
        reminder = await db.reminders.find_one(
            {"recipient_phone": from_phone, "status": "sent"},
            {"_id": 0},
            sort=[("last_sent_at", -1)]
        )
        
        if reminder:
            found_reminder = True
            is_self_reminder = reminder.get('recipient_name') == 'self' or reminder.get('recipient_phone') == reminder.get('creator_phone')
            
            # Mark the reminder as skipped
            await db.reminders.update_one(
                {"id": reminder['id']},
                {"$set": {
                    "status": "skipped",
                    "skipped": True,
                    "updated_at": serialize_datetime(now)
                }}
            )
            
            # Voice-specific response
            if parsed.get('from_voice'):
                response_text = "Okay, I heard you. Skipping this one! No pressure. 💛"
            else:
                response_text = "Okay, I've skipped this reminder. No worries! 💛"
            
            # If not a self-reminder, notify the creator
            if not is_self_reminder:
                creator_phone = reminder.get('creator_phone')
                if creator_phone and is_twilio_configured():
                    recipient_name = reminder.get('recipient_name', 'Your contact')
                    recipient_relationship = reminder.get('recipient_relationship')
                    name = recipient_relationship.capitalize() if recipient_relationship else recipient_name
                    
                    await send_whatsapp_message(
                        creator_phone,
                        f"ℹ️ *Update*\n\n{name} chose to skip:\n\"{reminder['message'][:50]}...\"\n\nThey might need a check-in. 💛\n\n— Daisy"
                    )
            
            logger.info(f"Reminder {reminder['id']} skipped by user")
        
        # Also check multi-time reminders
        if not found_reminder:
            multi_reminder = await db.multi_time_reminders.find_one(
                {"recipient_phone": from_phone, "status": "active"},
                {"_id": 0},
                sort=[("updated_at", -1)]
            )
            
            if multi_reminder:
                reminder_times = multi_reminder.get('reminder_times', [])
                any_sent = any(rt.get('status') == 'sent' for rt in reminder_times)
                
                if any_sent:
                    found_reminder = True
                    
                    # Mark all remaining pending times as skipped
                    await db.multi_time_reminders.update_one(
                        {"id": multi_reminder['id']},
                        {"$set": {
                            "status": "skipped",
                            "updated_at": serialize_datetime(now)
                        }}
                    )
                    
                    response_text = "Got it, I've stopped the reminders for this task. 💛"
                    
                    # Notify creator if not self-reminder
                    if multi_reminder.get('creator_phone') and multi_reminder['creator_phone'] != from_phone:
                        await send_whatsapp_message(
                            multi_reminder['creator_phone'],
                            f"ℹ️ *Update*\n\n{multi_reminder.get('recipient_name', 'Your contact')} chose to skip:\n\"{multi_reminder['message'][:50]}...\"\n\nThey might need a check-in. 💛\n\n— Daisy"
                        )
                    
                    logger.info(f"Multi-time reminder {multi_reminder['id']} skipped")
        
        if not found_reminder:
            response_text = "I don't have any active reminders for you to skip. Let me know if you need anything! 🌼"
    
    elif parsed.get('intent') == 'create_reminder':
        # Handle reminder creation from WhatsApp
        reminder_message = parsed.get('message', Body)
        scheduled_time = parsed.get('scheduled_time')
        recipient_name = parsed.get('recipient_name', 'self')
        recipient_phone = parsed.get('recipient_phone', from_phone)
        recurrence = parsed.get('recurrence', 'once')
        
        # If recipient is self, use sender's phone
        if recipient_name == 'self' or not recipient_phone:
            recipient_phone = from_phone
            recipient_name = 'self'
        
        if scheduled_time:
            try:
                # Parse the scheduled time
                if isinstance(scheduled_time, str):
                    scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00'))
                else:
                    scheduled_dt = scheduled_time
                
                # Create the reminder
                reminder = Reminder(
                    creator_id=user['id'] if user else f"whatsapp_{from_phone}",
                    creator_phone=from_phone,
                    message=reminder_message,
                    scheduled_time=scheduled_dt,
                    recipient_phone=recipient_phone,
                    recipient_name=recipient_name,
                    recurrence=recurrence,
                    status="pending"
                )
                
                reminder_dict = reminder.model_dump()
                reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
                reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                
                await db.reminders.insert_one(reminder_dict)
                
                # Use AI's friendly_response which has the correct local time
                # Only fall back to UTC formatting if AI didn't provide a response
                response_text = parsed.get('friendly_response')
                if not response_text:
                    # Convert to user's local timezone for display
                    user_tz_name = parsed.get('user_timezone', 'Australia/Melbourne')
                    try:
                        import pytz
                        user_tz = pytz.timezone(user_tz_name)
                        local_dt = scheduled_dt.astimezone(user_tz)
                        friendly_time = local_dt.strftime('%I:%M %p')
                        friendly_date = local_dt.strftime('%B %d')
                    except Exception:
                        friendly_time = scheduled_dt.strftime('%I:%M %p')
                        friendly_date = scheduled_dt.strftime('%B %d')
                    
                    if recipient_name == 'self':
                        response_text = f"Got it! I'll remind you to {reminder_message} at {friendly_time} on {friendly_date}. 🌼"
                    else:
                        response_text = f"I'll remind {recipient_name} to {reminder_message}. 🌼"
                    
                logger.info(f"Created reminder: {reminder.id} for {recipient_phone} at {scheduled_time}")
                
            except Exception as e:
                logger.error(f"Error creating reminder: {e}")
                response_text = "I had trouble setting that reminder. Could you try again with a clearer time? For example: 'Remind me to call mom in 30 minutes' 🌼"
        else:
            # No scheduled time extracted
            response_text = parsed.get('friendly_response', "I'd love to set that reminder! When would you like to be reminded? 🌼")
    
    elif parsed.get('intent') == 'request_phone':
        # User wants to remind someone else but didn't provide phone
        # FIRST: Check if we already have a contact with this name
        recipient_name = parsed.get('recipient_name', '').strip()
        current_user_id = user['id'] if user else f"whatsapp_{from_phone}"
        whatsapp_user_id = f"whatsapp_{from_phone}"
        
        # Search for existing contact by name (case-insensitive)
        existing_contact = None
        if recipient_name:
            existing_contact = await db.contacts.find_one({
                "$or": [
                    {"user_id": current_user_id},
                    {"user_id": whatsapp_user_id}
                ],
                "name": {"$regex": f"^{recipient_name}$", "$options": "i"},
                "consent_status": "approved"
            }, {"_id": 0})
        
        if existing_contact:
            # Found existing contact with consent - create reminder directly!
            recipient_phone = existing_contact['phone']
            contact_name = existing_contact.get('name', recipient_name)
            reminder_message = parsed.get('message', 'your task')
            scheduled_time = parsed.get('scheduled_time')
            recurrence = parsed.get('recurrence', 'once')
            
            if scheduled_time:
                try:
                    scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00')) if isinstance(scheduled_time, str) else scheduled_time
                    
                    reminder = Reminder(
                        creator_id=current_user_id,
                        creator_phone=from_phone,
                        creator_name=user_name,
                        message=reminder_message,
                        scheduled_time=scheduled_dt,
                        recipient_phone=recipient_phone,
                        recipient_name=contact_name,
                        recurrence=recurrence,
                        status="pending"
                    )
                    
                    reminder_dict = reminder.model_dump()
                    reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
                    reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                    reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                    
                    await db.reminders.insert_one(reminder_dict)
                    
                    # Format time for display
                    user_tz_name = parsed.get('user_timezone', 'Australia/Melbourne')
                    try:
                        import pytz
                        user_tz = pytz.timezone(user_tz_name)
                        local_dt = scheduled_dt.astimezone(user_tz)
                        friendly_time = local_dt.strftime('%I:%M %p')
                    except Exception:
                        friendly_time = scheduled_dt.strftime('%I:%M %p')
                    
                    response_text = f"Done! I'll remind {contact_name} to {reminder_message} at {friendly_time}. 🌼💛"
                    logger.info(f"Created reminder for existing contact {contact_name} ({recipient_phone})")
                except Exception as e:
                    logger.error(f"Error creating reminder for existing contact: {e}")
                    response_text = f"I found {contact_name}'s number but had trouble setting the reminder. Could you try again? 🌼"
            else:
                # No time specified - ask for time
                response_text = f"I found {contact_name} in your contacts! 🌼 When would you like me to remind them?"
        else:
            # No existing contact found - store pending reminder and ask for phone
            pending_reminder = {
                "user_phone": from_phone,
                "message": parsed.get('message'),
                "recipient_name": recipient_name,
                "scheduled_time": parsed.get('scheduled_time'),
                "recurrence": parsed.get('recurrence', 'once'),
                "created_at": serialize_datetime(datetime.now(timezone.utc))
            }
            await db.pending_reminders.update_one(
                {"user_phone": from_phone},
                {"$set": pending_reminder},
                upsert=True
            )
            response_text = parsed.get('friendly_response', f"I'd be happy to remind your {recipient_name or 'contact'}! 🌼 What's their WhatsApp phone number? (Include country code like +91 or +61)")
    
    elif parsed.get('intent') == 'provide_phone':
        # User provided a phone number - check if we have a pending reminder
        recipient_phone = parsed.get('recipient_phone', '').replace(' ', '').replace('-', '')
        
        # Get pending reminder for this user
        pending = await db.pending_reminders.find_one({"user_phone": from_phone}, {"_id": 0})
        
        if pending:
            # Create the reminder for the other person
            recipient_name = pending.get('recipient_name', 'Contact')
            reminder_message = pending.get('message', 'your task')
            scheduled_time = pending.get('scheduled_time')
            recurrence = pending.get('recurrence', 'once')
            
            # Build user_id for current user
            current_user_id = user['id'] if user else f"whatsapp_{from_phone}"
            whatsapp_user_id = f"whatsapp_{from_phone}"
            
            # Check if contact already exists and has consent - check BOTH web user ID and WhatsApp user ID
            existing_contact = await db.contacts.find_one({
                "phone": recipient_phone,
                "$or": [
                    {"user_id": current_user_id},
                    {"user_id": whatsapp_user_id}
                ],
                "consent_status": "approved"
            }, {"_id": 0})
            
            if existing_contact:
                # Contact already consented - create reminder directly
                if scheduled_time:
                    scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00')) if isinstance(scheduled_time, str) else scheduled_time
                    
                    reminder = Reminder(
                        creator_id=user['id'] if user else f"whatsapp_{from_phone}",
                        creator_phone=from_phone,
                        message=reminder_message,
                        scheduled_time=scheduled_dt,
                        recipient_phone=recipient_phone,
                        recipient_name=recipient_name,
                        recurrence=recurrence,
                        status="pending"
                    )
                    
                    reminder_dict = reminder.model_dump()
                    reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
                    reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                    reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                    
                    await db.reminders.insert_one(reminder_dict)
                    response_text = f"Great news! {recipient_name} has already approved reminders from you. I've scheduled the reminder to {reminder_message}. 🌼"
                else:
                    response_text = f"{recipient_name} has approved reminders. When would you like me to remind them to {reminder_message}?"
            else:
                # New contact or pending consent - create contact and send consent request
                creator_name = user_name  # Use the user_name we extracted earlier
                logger.info(f"Sending consent request with creator_name: {creator_name}")
                
                # Create or update contact
                new_contact = Contact(
                    user_id=user['id'] if user else f"whatsapp_{from_phone}",
                    name=recipient_name,
                    phone=recipient_phone,
                    consent_status="pending"
                )
                
                await db.contacts.update_one(
                    {"phone": recipient_phone, "user_id": new_contact.user_id},
                    {"$set": {
                        "name": recipient_name,
                        "phone": recipient_phone,
                        "user_id": new_contact.user_id,
                        "consent_status": "pending",
                        "updated_at": serialize_datetime(datetime.now(timezone.utc))
                    }},
                    upsert=True
                )
                
                # Store the pending reminder with phone now included
                if scheduled_time:
                    scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00')) if isinstance(scheduled_time, str) else scheduled_time
                    
                    reminder = Reminder(
                        creator_id=user['id'] if user else f"whatsapp_{from_phone}",
                        creator_phone=from_phone,
                        message=reminder_message,
                        scheduled_time=scheduled_dt,
                        recipient_phone=recipient_phone,
                        recipient_name=recipient_name,
                        recurrence=recurrence,
                        status="awaiting_consent"  # Won't be sent until consent is given
                    )
                    
                    reminder_dict = reminder.model_dump()
                    reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
                    reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                    reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                    
                    await db.reminders.insert_one(reminder_dict)
                
                # Send consent request
                if is_twilio_configured():
                    recurrence_text = "daily" if recurrence == "daily" else "weekly" if recurrence == "weekly" else ""
                    await send_consent_request(recipient_phone, creator_name, f"{recurrence_text} {reminder_message}".strip())
                
                response_text = f"Perfect! I've sent a message to {recipient_name} at {recipient_phone} asking for their permission. Once they reply YES, I'll start sending reminders. 🌼"
            
            # Clear the pending reminder
            await db.pending_reminders.delete_one({"user_phone": from_phone})
        else:
            response_text = parsed.get('friendly_response', "Thanks for the number! What would you like me to remind them about?")
    
    elif parsed.get('intent') == 'create_reminder_for_other':
        # User provided both recipient name and phone number
        recipient_name = parsed.get('recipient_name', 'Contact')
        recipient_phone = parsed.get('recipient_phone', '').replace(' ', '').replace('-', '')
        reminder_message = parsed.get('message', 'your task')
        scheduled_time = parsed.get('scheduled_time')
        recurrence = parsed.get('recurrence', 'once')
        
        if recipient_phone:
            # Check existing consent
            existing_contact = await db.contacts.find_one({
                "phone": recipient_phone,
                "user_id": user['id'] if user else f"whatsapp_{from_phone}"
            }, {"_id": 0})
            
            creator_name = user['name'] if user else "Someone"
            
            if existing_contact and existing_contact.get('consent_status') == 'approved':
                # Already consented
                if scheduled_time:
                    scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00')) if isinstance(scheduled_time, str) else scheduled_time
                    
                    reminder = Reminder(
                        creator_id=user['id'] if user else f"whatsapp_{from_phone}",
                        creator_phone=from_phone,
                        message=reminder_message,
                        scheduled_time=scheduled_dt,
                        recipient_phone=recipient_phone,
                        recipient_name=recipient_name,
                        recurrence=recurrence,
                        status="pending"
                    )
                    
                    reminder_dict = reminder.model_dump()
                    reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
                    reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                    reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                    
                    await db.reminders.insert_one(reminder_dict)
                    response_text = f"{recipient_name} has already approved reminders. I've scheduled it! 🌼"
                else:
                    response_text = f"{recipient_name} has approved reminders. When should I remind them?"
            else:
                # Need consent
                creator_name = user_name  # Use extracted user_name
                
                await db.contacts.update_one(
                    {"phone": recipient_phone, "user_id": user['id'] if user else f"whatsapp_{from_phone}"},
                    {"$set": {
                        "name": recipient_name,
                        "phone": recipient_phone,
                        "user_id": user['id'] if user else f"whatsapp_{from_phone}",
                        "consent_status": "pending",
                        "updated_at": serialize_datetime(datetime.now(timezone.utc))
                    }},
                    upsert=True
                )
                
                # Store reminder as awaiting consent
                if scheduled_time:
                    scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00')) if isinstance(scheduled_time, str) else scheduled_time
                    
                    reminder = Reminder(
                        creator_id=user['id'] if user else f"whatsapp_{from_phone}",
                        creator_phone=from_phone,
                        message=reminder_message,
                        scheduled_time=scheduled_dt,
                        recipient_phone=recipient_phone,
                        recipient_name=recipient_name,
                        recurrence=recurrence,
                        status="awaiting_consent"
                    )
                    
                    reminder_dict = reminder.model_dump()
                    reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
                    reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                    reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                    
                    await db.reminders.insert_one(reminder_dict)
                
                if is_twilio_configured():
                    await send_consent_request(recipient_phone, creator_name, reminder_message)
                
                response_text = parsed.get('friendly_response', f"I've sent a consent request to {recipient_name}. Once they approve, I'll start the reminders! 🌼")
        else:
            response_text = "I need a phone number to send reminders. What's their WhatsApp number?"
    
    # ============== TEAM INTENT HANDLERS ==============
    
    elif parsed.get('intent') == 'create_team':
        # Create a new team via WhatsApp
        team_name = parsed.get('team_name', '').strip()
        if team_name:
            # Check if team already exists for this user
            existing_team = await db.teams.find_one({
                "owner_phone": from_phone,
                "name": {"$regex": f"^{team_name}$", "$options": "i"}
            }, {"_id": 0})
            
            if existing_team:
                response_text = f"You already have a team called '{existing_team['name']}'. 🌼"
            else:
                import secrets
                invite_code = secrets.token_urlsafe(8)
                
                team = Team(
                    name=team_name,
                    owner_id=user['id'] if user else f"whatsapp_{from_phone}",
                    owner_phone=from_phone,
                    invite_code=invite_code
                )
                
                team_dict = team.model_dump()
                team_dict['created_at'] = serialize_datetime(team_dict['created_at'])
                team_dict['updated_at'] = serialize_datetime(team_dict['updated_at'])
                await db.teams.insert_one(team_dict)
                
                # Add owner as member
                owner_member = TeamMember(
                    team_id=team.id,
                    phone=from_phone,
                    name=user_name,
                    role="owner",
                    status="approved",
                    added_by=from_phone,
                    approved_by=from_phone,
                    approved_at=datetime.now(timezone.utc)
                )
                member_dict = owner_member.model_dump()
                member_dict['created_at'] = serialize_datetime(member_dict['created_at'])
                member_dict['updated_at'] = serialize_datetime(member_dict['updated_at'])
                member_dict['approved_at'] = serialize_datetime(member_dict['approved_at'])
                await db.team_members.insert_one(member_dict)
                
                response_text = f"""🌼 Team "{team_name}" created!

Now add your team members:
• "Add +91XXXXXXXXXX to {team_name}"
• Or share a contact card

Once added, send reminders:
• "Remind {team_name} to [task] at [time]"
"""
        else:
            response_text = "What would you like to name your team? 🌼"
    
    elif parsed.get('intent') == 'add_team_member':
        # Add a member to a team
        team_name = parsed.get('team_name', '').strip()
        member_phone = parsed.get('member_phone', '').replace(' ', '').replace('-', '')
        member_name = parsed.get('member_name', '')
        
        if not member_phone:
            # Try to extract from message
            from ai_engine import extract_phone_number_regex
            member_phone = extract_phone_number_regex(Body)
        
        if team_name and member_phone:
            # Find the team
            team = await db.teams.find_one({
                "owner_phone": from_phone,
                "name": {"$regex": f"^{team_name}$", "$options": "i"}
            }, {"_id": 0})
            
            if not team:
                # Check if user is admin of any team with that name
                user_teams = await db.team_members.find({
                    "phone": from_phone,
                    "role": {"$in": ["owner", "admin"]},
                    "status": "approved"
                }, {"_id": 0}).to_list(50)
                
                team_ids = [m['team_id'] for m in user_teams]
                if team_ids:
                    team = await db.teams.find_one({
                        "id": {"$in": team_ids},
                        "name": {"$regex": f"^{team_name}$", "$options": "i"}
                    }, {"_id": 0})
            
            if team:
                # Check if already a member
                existing = await db.team_members.find_one({
                    "team_id": team['id'],
                    "phone": member_phone
                }, {"_id": 0})
                
                if existing:
                    response_text = f"{member_name or member_phone} is already in {team['name']}. 🌼"
                else:
                    # AUTO-APPROVE: When admin/owner adds someone, they're immediately approved
                    new_member = TeamMember(
                        team_id=team['id'],
                        phone=member_phone,
                        name=member_name,
                        role="member",
                        status="approved",  # Auto-approved by admin
                        added_by=from_phone,
                        approved_by=from_phone,
                        approved_at=datetime.now(timezone.utc)
                    )
                    member_dict = new_member.model_dump()
                    member_dict['created_at'] = serialize_datetime(member_dict['created_at'])
                    member_dict['updated_at'] = serialize_datetime(member_dict['updated_at'])
                    member_dict['approved_at'] = serialize_datetime(member_dict['approved_at'])
                    await db.team_members.insert_one(member_dict)
                    
                    # Notify the new member they've been added (no approval needed)
                    if is_twilio_configured():
                        await send_whatsapp_message(
                            member_phone,
                            f"🌼 Hi! {user_name} has added you to the team \"{team['name']}\".\n\nYou'll receive team reminders from now on. Reply \"Done\" or \"Sure\" to acknowledge any reminder.\n\n- Daisy"
                        )
                    
                    response_text = f"Done! Added {member_name or member_phone} to {team['name']}. They'll now receive team reminders. 🌼"
            else:
                response_text = f"I couldn't find a team called '{team_name}'. Use 'Create team {team_name}' first. 🌼"
        else:
            response_text = "Please provide both the team name and phone number. Example: 'Add +919876543210 to Marketing' 🌼"
    
    elif parsed.get('intent') == 'approve_team_member':
        # Approve a pending team member
        member_phone = parsed.get('member_phone', '').replace(' ', '').replace('-', '')
        team_name = parsed.get('team_name', '').strip()
        
        if not member_phone:
            from ai_engine import extract_phone_number_regex
            member_phone = extract_phone_number_regex(Body)
        
        if member_phone:
            # Find pending member in user's teams
            user_teams = await db.team_members.find({
                "phone": from_phone,
                "role": {"$in": ["owner", "admin"]},
                "status": "approved"
            }, {"_id": 0}).to_list(50)
            
            team_ids = [m['team_id'] for m in user_teams]
            
            query = {"phone": member_phone, "status": "pending"}
            if team_ids:
                query["team_id"] = {"$in": team_ids}
            if team_name:
                # Get team ID for the specific team
                team = await db.teams.find_one({"name": {"$regex": f"^{team_name}$", "$options": "i"}}, {"_id": 0})
                if team:
                    query["team_id"] = team['id']
            
            pending_member = await db.team_members.find_one(query, {"_id": 0})
            
            if pending_member:
                await db.team_members.update_one(
                    {"id": pending_member['id']},
                    {"$set": {
                        "status": "approved",
                        "approved_by": from_phone,
                        "approved_at": serialize_datetime(datetime.now(timezone.utc)),
                        "updated_at": serialize_datetime(datetime.now(timezone.utc))
                    }}
                )
                
                # Get team name
                team = await db.teams.find_one({"id": pending_member['team_id']}, {"_id": 0})
                team_display_name = team['name'] if team else "the team"
                
                # Notify the member
                if is_twilio_configured():
                    from whatsapp import send_team_member_approved
                    await send_team_member_approved(member_phone, team_display_name, user_name)
                
                response_text = f"Approved {pending_member.get('name', member_phone)} for {team_display_name}! They'll now receive team reminders. 🌼"
            else:
                response_text = f"I couldn't find a pending member with phone {member_phone} in your teams. 🌼"
        else:
            response_text = "Please provide the phone number to approve. Example: 'Approve +919876543210' 🌼"
    
    elif parsed.get('intent') == 'create_team_reminder':
        # Create a reminder for entire team
        team_name = parsed.get('team_name', '').strip()
        reminder_message = parsed.get('message', '')
        scheduled_time = parsed.get('scheduled_time')
        recurrence = parsed.get('recurrence', 'once')
        persist = parsed.get('persist_until_all_acknowledge', True)
        
        if team_name:
            # Find the team
            team = await db.teams.find_one({
                "$or": [
                    {"owner_phone": from_phone, "name": {"$regex": f"^{team_name}$", "$options": "i"}},
                    {"name": {"$regex": f"^{team_name}$", "$options": "i"}}
                ]
            }, {"_id": 0})
            
            if not team:
                response_text = f"I couldn't find a team called '{team_name}'. 🌼"
            else:
                # Check if user is owner or admin
                user_membership = await db.team_members.find_one({
                    "team_id": team['id'],
                    "phone": from_phone,
                    "role": {"$in": ["owner", "admin"]},
                    "status": "approved"
                }, {"_id": 0})
                
                if not user_membership:
                    response_text = f"You need to be an owner or admin of {team['name']} to send team reminders. 🌼"
                else:
                    # Get approved members
                    members = await db.team_members.find({
                        "team_id": team['id'],
                        "status": "approved"
                    }, {"_id": 0}).to_list(500)
                    
                    if len(members) <= 1:
                        response_text = f"{team['name']} has no other approved members yet. Add some members first! 🌼"
                    elif scheduled_time:
                        scheduled_dt = datetime.fromisoformat(scheduled_time.replace('Z', '+00:00')) if isinstance(scheduled_time, str) else scheduled_time
                        
                        # Create team reminder
                        team_reminder = TeamReminder(
                            team_id=team['id'],
                            team_name=team['name'],
                            creator_id=user['id'] if user else f"whatsapp_{from_phone}",
                            creator_phone=from_phone,
                            message=reminder_message,
                            scheduled_time=scheduled_dt,
                            recurrence=recurrence,
                            persist_until_all_acknowledge=persist,
                            total_members=len(members)
                        )
                        
                        reminder_dict = team_reminder.model_dump()
                        reminder_dict['scheduled_time'] = serialize_datetime(reminder_dict['scheduled_time'])
                        reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                        reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                        await db.team_reminders.insert_one(reminder_dict)
                        
                        # Create acknowledgment records
                        for member in members:
                            if member['phone'] != from_phone:  # Don't remind the creator
                                ack = TeamReminderAcknowledgment(
                                    team_reminder_id=team_reminder.id,
                                    member_phone=member['phone'],
                                    member_name=member.get('name')
                                )
                                ack_dict = ack.model_dump()
                                ack_dict['created_at'] = serialize_datetime(ack_dict['created_at'])
                                await db.team_reminder_acks.insert_one(ack_dict)
                        
                        persist_text = "I'll keep reminding until everyone confirms!" if persist else ""
                        response_text = parsed.get('friendly_response', f"Team reminder set for {team['name']}! {len(members)-1} members will be reminded. {persist_text} 🌼")
                    else:
                        response_text = "When should I remind the team? Please include a time. 🌼"
        else:
            response_text = "Which team should I remind? 🌼"
    
    elif parsed.get('intent') == 'list_teams':
        # List user's teams
        user_teams = await db.team_members.find({
            "phone": from_phone,
            "status": "approved"
        }, {"_id": 0}).to_list(50)
        
        if user_teams:
            team_ids = [m['team_id'] for m in user_teams]
            teams = await db.teams.find({"id": {"$in": team_ids}}, {"_id": 0}).to_list(50)
            
            team_list = []
            for team in teams:
                member_count = await db.team_members.count_documents({"team_id": team['id'], "status": "approved"})
                role = next((m['role'] for m in user_teams if m['team_id'] == team['id']), 'member')
                team_list.append(f"• {team['name']} ({member_count} members) - You're {role}")
            
            response_text = "🌼 Your teams:\n\n" + "\n".join(team_list)
        else:
            response_text = "You're not part of any teams yet. Create one with 'Create team [name]' 🌼"
    
    elif parsed.get('intent') == 'show_team_members':
        # Show members of a team
        team_name = parsed.get('team_name', '').strip()
        
        if team_name:
            team = await db.teams.find_one({
                "name": {"$regex": f"^{team_name}$", "$options": "i"}
            }, {"_id": 0})
            
            if team:
                members = await db.team_members.find({"team_id": team['id']}, {"_id": 0}).to_list(100)
                
                member_list = []
                for m in members:
                    status_emoji = "✅" if m['status'] == 'approved' else "⏳"
                    role_text = f" ({m['role']})" if m['role'] != 'member' else ""
                    member_list.append(f"{status_emoji} {m.get('name', m['phone'])}{role_text}")
                
                response_text = f"🌼 {team['name']} members:\n\n" + "\n".join(member_list)
            else:
                response_text = f"I couldn't find a team called '{team_name}'. 🌼"
        else:
            response_text = "Which team's members would you like to see? 🌼"
    
    elif parsed.get('intent') == 'create_multi_time_reminder':
        # Handle multi-time reminder creation (send NOW + specific times until acknowledged)
        reminder_message = parsed.get('message', '')
        recipient_name = parsed.get('recipient_name', '')
        recipient_phone = parsed.get('recipient_phone', '')
        deadline_time = parsed.get('deadline_time')
        reminder_times_raw = parsed.get('reminder_times', [])
        send_now = parsed.get('send_now', False)
        
        # Check if this is a SELF-reminder (remind ME to do something)
        is_self_reminder = recipient_name.lower() == 'self' or not recipient_name
        
        if is_self_reminder:
            # SELF multi-time reminder - remind the sender themselves
            recipient_phone = from_phone
            recipient_name = 'self'
        elif recipient_name and not recipient_phone:
            # Get recipient phone from contacts if not provided
            contact = await db.contacts.find_one({
                "user_id": user['id'] if user else f"whatsapp_{from_phone}",
                "name": {"$regex": f"^{recipient_name}$", "$options": "i"}
            }, {"_id": 0})
            if contact:
                recipient_phone = contact.get('phone')
        
        if not recipient_phone and not is_self_reminder:
            # Check if we need to ask for phone (only for non-self reminders)
            response_text = f"I'd love to set up those reminders for {recipient_name}! What's their WhatsApp number? 🌼"
        elif not reminder_message:
            response_text = "What would you like me to remind about? 🌼"
        else:
            # Parse and convert reminder times
            now = datetime.now(timezone.utc)
            
            # Build reminder times list
            parsed_times = []
            
            # Handle "send now" / immediate
            if send_now or any(t.get('time') == 'now' for t in reminder_times_raw if isinstance(t, dict)):
                parsed_times.append({
                    "time": serialize_datetime(now),
                    "label": "immediate",
                    "status": "pending",
                    "sent_at": None
                })
            
            # Parse other times
            for rt in reminder_times_raw:
                if isinstance(rt, dict):
                    time_val = rt.get('time', '')
                    label = rt.get('label', '')
                    
                    if time_val == 'now':
                        continue  # Already handled above
                    
                    # Try to parse as ISO datetime
                    try:
                        if isinstance(time_val, str) and 'T' in time_val:
                            parsed_dt = datetime.fromisoformat(time_val.replace('Z', '+00:00'))
                            parsed_times.append({
                                "time": serialize_datetime(parsed_dt),
                                "label": label,
                                "status": "pending",
                                "sent_at": None
                            })
                    except Exception as e:
                        logger.warning(f"Could not parse reminder time {time_val}: {e}")
            
            # Add deadline time as the final reminder
            if deadline_time:
                try:
                    if isinstance(deadline_time, str):
                        deadline_dt = datetime.fromisoformat(deadline_time.replace('Z', '+00:00'))
                    else:
                        deadline_dt = deadline_time
                    parsed_times.append({
                        "time": serialize_datetime(deadline_dt),
                        "label": "deadline",
                        "status": "pending",
                        "sent_at": None
                    })
                except Exception as e:
                    logger.warning(f"Could not parse deadline time {deadline_time}: {e}")
            
            # Sort times chronologically
            parsed_times.sort(key=lambda x: x['time'])
            
            if not parsed_times:
                response_text = "I need at least one reminder time. When should I remind? 🌼"
            else:
                # For SELF-reminders, no consent needed
                if is_self_reminder:
                    has_consent = True
                else:
                    # Check consent for recipient (non-self reminders)
                    contact = await db.contacts.find_one({
                        "phone": recipient_phone,
                        "user_id": user['id'] if user else f"whatsapp_{from_phone}"
                    }, {"_id": 0})
                    has_consent = contact and contact.get('consent_status') == 'approved'
                
                # Create the multi-time reminder
                multi_reminder = MultiTimeReminder(
                    creator_id=user['id'] if user else f"whatsapp_{from_phone}",
                    creator_phone=from_phone,
                    creator_name=user_name,
                    recipient_phone=recipient_phone,
                    recipient_name=recipient_name,
                    message=reminder_message,
                    reminder_times=parsed_times,
                    deadline_time=datetime.fromisoformat(deadline_time.replace('Z', '+00:00')) if deadline_time and isinstance(deadline_time, str) else deadline_time,
                    send_now=send_now,
                    status="active" if has_consent else "awaiting_consent"
                )
                
                reminder_dict = multi_reminder.model_dump()
                reminder_dict['created_at'] = serialize_datetime(reminder_dict['created_at'])
                reminder_dict['updated_at'] = serialize_datetime(reminder_dict['updated_at'])
                if reminder_dict.get('deadline_time'):
                    reminder_dict['deadline_time'] = serialize_datetime(reminder_dict['deadline_time'])
                
                await db.multi_time_reminders.insert_one(reminder_dict)
                
                # If consent needed (non-self reminder without consent), request it
                if not has_consent and not is_self_reminder:
                    # Save/update contact
                    await db.contacts.update_one(
                        {"phone": recipient_phone, "user_id": user['id'] if user else f"whatsapp_{from_phone}"},
                        {"$set": {
                            "name": recipient_name,
                            "phone": recipient_phone,
                            "user_id": user['id'] if user else f"whatsapp_{from_phone}",
                            "consent_status": "pending",
                            "updated_at": serialize_datetime(datetime.now(timezone.utc))
                        }},
                        upsert=True
                    )
                    
                    if is_twilio_configured():
                        await send_consent_request(recipient_phone, user_name, reminder_message)
                    
                    response_text = f"I've asked {recipient_name} for permission. Once they approve, I'll send reminders at all the scheduled times until they confirm! 🌼"
                else:
                    # Has consent OR self-reminder - send immediate reminder if requested
                    if send_now:
                        if is_twilio_configured():
                            if is_self_reminder:
                                await send_whatsapp_message(
                                    recipient_phone,
                                    f"🌼 Reminder:\n\n{reminder_message}\n\nReply \"Done\" when completed.\n\n- Daisy"
                                )
                            else:
                                await send_whatsapp_message(
                                    recipient_phone,
                                    f"🌼 Reminder from {user_name}:\n\n{reminder_message}\n\nPlease reply \"Done\" or \"Sure\" when completed.\n\n- Daisy"
                                )
                        # Mark the immediate one as sent
                        for pt in parsed_times:
                            if pt['label'] == 'immediate':
                                pt['status'] = 'sent'
                                pt['sent_at'] = serialize_datetime(now)
                                break
                        
                        await db.multi_time_reminders.update_one(
                            {"id": multi_reminder.id},
                            {"$set": {"reminder_times": parsed_times}}
                        )
                    
                    time_count = len(parsed_times)
                    response_text = parsed.get('friendly_response', f"All set! I'll remind {recipient_name} at {time_count} different times until they confirm. 🌼")
                
                logger.info(f"Created multi-time reminder {multi_reminder.id} with {len(parsed_times)} scheduled times")
    
    # ============== HABIT SYSTEM INTENT HANDLERS ==============
    
    elif parsed.get('intent') == 'create_habit':
        # Create a new habit with confirmation flow
        habit_name = parsed.get('habit_name', '')
        category = parsed.get('category', 'Custom')
        frequency = parsed.get('frequency', 'daily')
        custom_days = parsed.get('custom_days', [])
        time_str = parsed.get('time', '09:00')
        difficulty = parsed.get('difficulty', 3)
        reminder_intensity = parsed.get('reminder_intensity', 'standard')
        
        if not habit_name:
            response_text = "What habit would you like to build? Tell me about it! 🌼"
        else:
            # Detect user's timezone
            from ai_engine import detect_timezone_from_phone
            user_tz = detect_timezone_from_phone(from_phone)
            
            # Create pending habit for confirmation
            pending_habit = PendingHabitCreation(
                user_phone=from_phone,
                name=habit_name,
                category=category,
                frequency=frequency,
                custom_days=custom_days if frequency == 'custom' else None,
                time=time_str,
                timezone=user_tz,
                difficulty=difficulty,
                reminder_intensity=reminder_intensity,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
            )
            
            pending_dict = pending_habit.model_dump()
            pending_dict['created_at'] = serialize_datetime(pending_dict['created_at'])
            pending_dict['expires_at'] = serialize_datetime(pending_dict['expires_at'])
            
            # Remove any existing pending habits for this user
            await db.pending_habits.delete_many({"user_phone": from_phone, "status": "awaiting_confirmation"})
            await db.pending_habits.insert_one(pending_dict)
            
            # Build confirmation message
            freq_display = frequency
            if frequency == 'custom' and custom_days:
                freq_display = ', '.join(custom_days)
            
            response_text = f"""🌼 Let's build this habit!

📋 **{habit_name}**
• Time: {time_str} ({user_tz})
• Frequency: {freq_display}
• Category: {category}
• Difficulty: {'⭐' * difficulty} ({difficulty}/5)
• Reminder: {reminder_intensity.title()}

Reply **Yes** to confirm, or tell me what you'd like to change!"""
    
    elif parsed.get('intent') == 'confirm_habit':
        # User confirmed habit creation
        confirmed = parsed.get('confirmed', True)
        
        # Find pending habit
        pending = await db.pending_habits.find_one({
            "user_phone": from_phone,
            "status": "awaiting_confirmation"
        }, {"_id": 0})
        
        if not pending:
            response_text = "I don't have a pending habit to confirm. Want to create a new habit? 🌼"
        elif confirmed:
            # Create the actual habit
            habit = Habit(
                user_id=user['id'] if user else f"whatsapp_{from_phone}",
                user_phone=from_phone,
                name=pending['name'],
                category=pending['category'],
                frequency=pending['frequency'],
                custom_days=pending.get('custom_days'),
                time=pending['time'],
                timezone=pending.get('timezone', 'UTC'),
                difficulty=pending.get('difficulty', 3),
                reminder_intensity=pending.get('reminder_intensity', 'standard')
            )
            
            habit_dict = habit.model_dump()
            habit_dict['created_at'] = serialize_datetime(habit_dict['created_at'])
            habit_dict['updated_at'] = serialize_datetime(habit_dict['updated_at'])
            habit_dict['start_date'] = serialize_datetime(habit_dict['start_date'])
            
            await db.habits.insert_one(habit_dict)
            
            # Mark pending as confirmed
            await db.pending_habits.update_one(
                {"id": pending['id']},
                {"$set": {"status": "confirmed"}}
            )
            
            response_text = f"""🎉 Your habit is now active!

**{habit.name}** will start from today.

I'll remind you at {habit.time} ({habit.frequency}).

Tips:
• Reply "Done" when you complete it
• Reply "Snooze" if you need more time
• Reply "Skip" if you can't do it today

Let's build this streak together! 🌼"""
            
            logger.info(f"Created habit {habit.id} for {from_phone}")
        else:
            # User cancelled
            await db.pending_habits.update_one(
                {"id": pending['id']},
                {"$set": {"status": "cancelled"}}
            )
            response_text = "No problem! Let me know when you want to create a habit. 🌼"
    
    elif parsed.get('intent') == 'complete_habit':
        # User completed a habit
        habit_name = parsed.get('habit_name', '')
        note = parsed.get('note', '')
        
        # Find the most recent habit log that was reminded
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        habit_log = await db.habit_logs.find_one({
            "user_phone": from_phone,
            "scheduled_date": today,
            "status": {"$in": ["reminded", "pending"]}
        }, {"_id": 0}, sort=[("scheduled_time", -1)])
        
        if habit_log:
            # Mark as completed
            await db.habit_logs.update_one(
                {"id": habit_log['id']},
                {"$set": {
                    "status": "completed",
                    "completed_at": serialize_datetime(datetime.now(timezone.utc)),
                    "completion_note": note,
                    "updated_at": serialize_datetime(datetime.now(timezone.utc))
                }}
            )
            
            # Update habit stats
            habit = await db.habits.find_one({"id": habit_log['habit_id']}, {"_id": 0})
            if habit:
                new_streak = habit.get('current_streak', 0) + 1
                longest = max(habit.get('longest_streak', 0), new_streak)
                total_completions = habit.get('total_completions', 0) + 1
                
                await db.habits.update_one(
                    {"id": habit['id']},
                    {"$set": {
                        "current_streak": new_streak,
                        "longest_streak": longest,
                        "total_completions": total_completions,
                        "last_completed_at": serialize_datetime(datetime.now(timezone.utc)),
                        "updated_at": serialize_datetime(datetime.now(timezone.utc))
                    }}
                )
                
                # Celebratory message based on streak
                if new_streak == 7:
                    response_text = f"🎉 ONE WEEK STREAK! You've done {habit['name']} for 7 days straight! Incredible dedication! 🌼"
                elif new_streak == 30:
                    response_text = f"🏆 30 DAY STREAK! You've made {habit['name']} a true habit! So proud of you! 🌼"
                elif new_streak % 10 == 0:
                    response_text = f"🔥 {new_streak} days in a row! Your {habit['name']} streak is on fire! 🌼"
                else:
                    response_text = f"✅ Done! That's {new_streak} days in a row for {habit['name']}! Keep it up! 🌼"
        else:
            response_text = parsed.get('friendly_response', "Great work! 🌼")
    
    elif parsed.get('intent') == 'snooze_habit':
        # User wants to snooze a habit
        snooze_minutes = parsed.get('snooze_minutes', 30)
        
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        habit_log = await db.habit_logs.find_one({
            "user_phone": from_phone,
            "scheduled_date": today,
            "status": "reminded"
        }, {"_id": 0}, sort=[("scheduled_time", -1)])
        
        if habit_log:
            snooze_until = datetime.now(timezone.utc) + timedelta(minutes=snooze_minutes)
            
            await db.habit_logs.update_one(
                {"id": habit_log['id']},
                {"$set": {
                    "snoozed": True,
                    "snooze_until": serialize_datetime(snooze_until),
                    "status": "pending",  # Reset to pending for re-reminder
                    "updated_at": serialize_datetime(datetime.now(timezone.utc))
                }}
            )
            
            response_text = f"No problem! I'll remind you again in {snooze_minutes} minutes. Take your time! 🌼"
        else:
            response_text = "I'll remind you later! 🌼"
    
    elif parsed.get('intent') == 'skip_habit':
        # User wants to skip a habit today
        reason = parsed.get('reason', '')
        
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        habit_log = await db.habit_logs.find_one({
            "user_phone": from_phone,
            "scheduled_date": today,
            "status": {"$in": ["reminded", "pending"]}
        }, {"_id": 0}, sort=[("scheduled_time", -1)])
        
        if habit_log:
            await db.habit_logs.update_one(
                {"id": habit_log['id']},
                {"$set": {
                    "status": "skipped",
                    "skipped": True,
                    "skip_reason": reason,
                    "updated_at": serialize_datetime(datetime.now(timezone.utc))
                }}
            )
            
            # Reset streak but don't increment missed count for skip
            habit = await db.habits.find_one({"id": habit_log['habit_id']}, {"_id": 0})
            if habit:
                await db.habits.update_one(
                    {"id": habit['id']},
                    {"$set": {
                        "current_streak": 0,
                        "updated_at": serialize_datetime(datetime.now(timezone.utc))
                    }}
                )
            
            response_text = "Okay, skipping for today. No judgment - life happens! 💛 See you tomorrow! 🌼"
        else:
            response_text = "Got it! Take care of yourself! 🌼"
    
    elif parsed.get('intent') == 'list_habits':
        # Show user's habits
        habits = await db.habits.find({
            "user_phone": from_phone,
            "status": "active"
        }, {"_id": 0}).to_list(20)
        
        if habits:
            habit_list = []
            for h in habits:
                streak_emoji = "🔥" if h.get('current_streak', 0) >= 7 else "📊"
                habit_list.append(f"{streak_emoji} **{h['name']}** - {h['time']} {h['frequency']} (Streak: {h.get('current_streak', 0)})")
            
            # Get the web dashboard URL from environment or use default
            dashboard_url = os.environ.get('FRONTEND_URL', 'https://caregiver-app-14.preview.emergentagent.com')
            
            response_text = "🌼 Your Active Habits:\n\n" + "\n".join(habit_list) + f"\n\n📱 View detailed stats & calendar at:\n{dashboard_url}/habits"
        else:
            response_text = "You don't have any active habits yet. Want to start one? Try: 'I want to start meditating every day at 6 AM' 🌼"
    
    elif parsed.get('intent') == 'pause_habit':
        # Pause a habit
        habit_name = parsed.get('habit_name', '')
        
        habit = await db.habits.find_one({
            "user_phone": from_phone,
            "name": {"$regex": habit_name, "$options": "i"},
            "status": "active"
        }, {"_id": 0})
        
        if habit:
            # Log the modification
            mod = HabitModification(
                habit_id=habit['id'],
                user_id=habit['user_id'],
                field_changed="status",
                previous_value="active",
                new_value="paused"
            )
            mod_dict = mod.model_dump()
            mod_dict['modified_at'] = serialize_datetime(mod_dict['modified_at'])
            await db.habit_modifications.insert_one(mod_dict)
            
            await db.habits.update_one(
                {"id": habit['id']},
                {"$set": {"status": "paused", "updated_at": serialize_datetime(datetime.now(timezone.utc))}}
            )
            
            response_text = f"I've paused **{habit['name']}**. Just say 'resume {habit['name']}' when you're ready to continue! 🌼"
        else:
            response_text = f"I couldn't find an active habit called '{habit_name}'. Check your habits with 'show my habits'. 🌼"
    
    elif parsed.get('intent') == 'resume_habit':
        # Resume a paused habit
        habit_name = parsed.get('habit_name', '')
        
        habit = await db.habits.find_one({
            "user_phone": from_phone,
            "name": {"$regex": habit_name, "$options": "i"},
            "status": "paused"
        }, {"_id": 0})
        
        if habit:
            # Log the modification
            mod = HabitModification(
                habit_id=habit['id'],
                user_id=habit['user_id'],
                field_changed="status",
                previous_value="paused",
                new_value="active"
            )
            mod_dict = mod.model_dump()
            mod_dict['modified_at'] = serialize_datetime(mod_dict['modified_at'])
            await db.habit_modifications.insert_one(mod_dict)
            
            await db.habits.update_one(
                {"id": habit['id']},
                {"$set": {"status": "active", "updated_at": serialize_datetime(datetime.now(timezone.utc))}}
            )
            
            response_text = f"Welcome back! **{habit['name']}** is active again. Let's rebuild that streak! 🌼"
        else:
            response_text = f"I couldn't find a paused habit called '{habit_name}'. 🌼"
    
    elif parsed.get('intent') == 'edit_habit':
        # Edit a habit
        habit_name = parsed.get('habit_name', '')
        field = parsed.get('field', '')
        new_value = parsed.get('new_value', '')
        
        habit = await db.habits.find_one({
            "user_phone": from_phone,
            "name": {"$regex": habit_name, "$options": "i"},
            "status": {"$in": ["active", "paused"]}
        }, {"_id": 0})
        
        if habit and field and new_value:
            old_value = habit.get(field, '')
            
            # Log the modification
            mod = HabitModification(
                habit_id=habit['id'],
                user_id=habit['user_id'],
                field_changed=field,
                previous_value=str(old_value),
                new_value=str(new_value)
            )
            mod_dict = mod.model_dump()
            mod_dict['modified_at'] = serialize_datetime(mod_dict['modified_at'])
            await db.habit_modifications.insert_one(mod_dict)
            
            await db.habits.update_one(
                {"id": habit['id']},
                {"$set": {field: new_value, "updated_at": serialize_datetime(datetime.now(timezone.utc))}}
            )
            
            response_text = f"Done! Updated **{habit['name']}** - {field} changed to {new_value}. 🌼"
        else:
            response_text = "I couldn't update that habit. Please specify the habit name and what you want to change. 🌼"
    
    elif parsed.get('intent') == 'delete_habit':
        # Delete a habit
        habit_name = parsed.get('habit_name', '')
        
        habit = await db.habits.find_one({
            "user_phone": from_phone,
            "name": {"$regex": habit_name, "$options": "i"}
        }, {"_id": 0})
        
        if habit:
            await db.habits.update_one(
                {"id": habit['id']},
                {"$set": {"status": "deleted", "updated_at": serialize_datetime(datetime.now(timezone.utc))}}
            )
            
            response_text = f"I've removed **{habit['name']}**. If you ever want to track it again, just create a new habit! 🌼"
        else:
            response_text = f"I couldn't find a habit called '{habit_name}'. 🌼"
    
    elif parsed.get('intent') == 'habit_stats':
        # Show stats for a specific habit
        habit_name = parsed.get('habit_name', '')
        
        habit = await db.habits.find_one({
            "user_phone": from_phone,
            "name": {"$regex": habit_name, "$options": "i"}
        }, {"_id": 0})
        
        if habit:
            total = habit.get('total_completions', 0) + habit.get('total_missed', 0)
            completion_rate = (habit.get('total_completions', 0) / total * 100) if total > 0 else 0
            
            response_text = f"""📊 **{habit['name']}** Stats:

🔥 Current Streak: {habit.get('current_streak', 0)} days
🏆 Longest Streak: {habit.get('longest_streak', 0)} days
✅ Total Completions: {habit.get('total_completions', 0)}
❌ Total Missed: {habit.get('total_missed', 0)}
📈 Completion Rate: {completion_rate:.1f}%

Keep going! 🌼"""
        else:
            response_text = f"I couldn't find a habit called '{habit_name}'. 🌼"
    
    # ============== SETUP MORNING AGENDA ==============
    elif parsed.get('intent') == 'setup_morning_agenda':
        # User wants to set up daily morning briefing
        time_str = parsed.get('time', '07:00')
        
        # Validate time format
        try:
            h, m = map(int, time_str.replace(' ', '').split(':'))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError()
            time_str = f"{h:02d}:{m:02d}"
        except Exception:
            time_str = "07:00"  # Default
        
        # Update user's morning agenda time
        await db.whatsapp_users.update_one(
            {"phone": from_phone},
            {"$set": {
                "morning_agenda_time": time_str,
                "agenda_enabled": True,
                "updated_at": serialize_datetime(datetime.now(timezone.utc))
            }},
            upsert=True
        )
        
        # Convert to 12-hour format for display
        h, m = map(int, time_str.split(':'))
        am_pm = "AM" if h < 12 else "PM"
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        display_time = f"{display_h}:{m:02d} {am_pm}"
        
        response_text = f"""☀️ *Morning Agenda Set Up!*

I'll send you a daily briefing at *{display_time}* with:
• All your reminders for the day
• Habits to complete
• Tasks you've set for others

Start your day organized and in control! 💛

To change the time, just say "Change morning agenda to [time]"

— Daisy 🌼"""
    
    # ============== SETUP EVENING WRAPUP ==============
    elif parsed.get('intent') == 'setup_evening_wrapup':
        # User wants to set up daily evening summary
        time_str = parsed.get('time', '21:00')
        
        # Validate time format
        try:
            h, m = map(int, time_str.replace(' ', '').split(':'))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError()
            time_str = f"{h:02d}:{m:02d}"
        except Exception:
            time_str = "21:00"  # Default
        
        # Update user's evening wrapup time
        await db.whatsapp_users.update_one(
            {"phone": from_phone},
            {"$set": {
                "evening_wrapup_time": time_str,
                "wrapup_enabled": True,
                "updated_at": serialize_datetime(datetime.now(timezone.utc))
            }},
            upsert=True
        )
        
        # Convert to 12-hour format for display
        h, m = map(int, time_str.split(':'))
        am_pm = "AM" if h < 12 else "PM"
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        display_time = f"{display_h}:{m:02d} {am_pm}"
        
        response_text = f"""🌙 *Evening Wrap-Up Set Up!*

I'll send you a daily summary at *{display_time}* with:
• Tasks you completed ✅
• Tasks that were missed or skipped ❌
• Tomorrow's preview

End your day with clarity! 💛

To change the time, just say "Change evening wrapup to [time]"

— Daisy 🌼"""
    
    elif parsed.get('intent') == 'weekly_report':
        # Generate weekly report
        habits = await db.habits.find({
            "user_phone": from_phone,
            "status": "active"
        }, {"_id": 0}).to_list(50)
        
        if habits:
            total_habits = len(habits)
            total_completions = sum(h.get('total_completions', 0) for h in habits)
            total_missed = sum(h.get('total_missed', 0) for h in habits)
            
            # Find best and worst performing
            best_habit = max(habits, key=lambda h: h.get('current_streak', 0))
            worst_habit = min(habits, key=lambda h: h.get('current_streak', 0))
            
            overall_rate = (total_completions / (total_completions + total_missed) * 100) if (total_completions + total_missed) > 0 else 0
            
            # Get the web dashboard URL
            dashboard_url = os.environ.get('FRONTEND_URL', 'https://caregiver-app-14.preview.emergentagent.com')
            
            response_text = f"""📊 **Weekly Habit Report**

📋 Total Habits: {total_habits}
✅ Completions: {total_completions}
❌ Missed: {total_missed}
📈 Overall Rate: {overall_rate:.1f}%

🏆 Best Streak: **{best_habit['name']}** ({best_habit.get('current_streak', 0)} days)
⚠️ Needs Attention: **{worst_habit['name']}** ({worst_habit.get('current_streak', 0)} days)

💡 Tip: Consistency beats intensity. Even small steps count!

📱 Full dashboard: {dashboard_url}/habits

Keep building those habits! 🌼"""
        else:
            response_text = "You don't have any active habits yet. Want to start one? 🌼"
    
    # ============== TASKS OVERVIEW (Smart Messaging System) ==============
    elif parsed.get('intent') == 'tasks_overview':
        # Show user their pending tasks for today
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        
        # Get pending reminders (self reminders)
        self_reminders = await db.reminders.find({
            "recipient_phone": from_phone,
            "status": {"$in": ["pending", "sent"]},
            "scheduled_time": {
                "$gte": serialize_datetime(today_start),
                "$lt": serialize_datetime(today_end)
            }
        }, {"_id": 0}).sort("scheduled_time", 1).to_list(20)
        
        # Get reminders user created for others
        others_reminders = await db.reminders.find({
            "creator_phone": from_phone,
            "recipient_phone": {"$ne": from_phone},
            "status": {"$in": ["pending", "sent", "awaiting_consent"]},
            "scheduled_time": {
                "$gte": serialize_datetime(today_start),
                "$lt": serialize_datetime(today_end)
            }
        }, {"_id": 0}).sort("scheduled_time", 1).to_list(20)
        
        # Get habit logs for today
        habit_logs = await db.habit_logs.find({
            "user_phone": from_phone,
            "scheduled_date": today_str,
            "status": {"$in": ["pending", "reminded"]}
        }, {"_id": 0}).to_list(20)
        
        # Build the overview message
        if not self_reminders and not others_reminders and not habit_logs:
            response_text = """📋 *Today's Tasks*

Your schedule is clear! No pending reminders or habits.

Enjoy your day, or let me know if you'd like to set something up. 💛

— Daisy"""
        else:
            overview_msg = "📋 *Today's Tasks*\n"
            
            # Self reminders section
            if self_reminders:
                overview_msg += "\n*Your Reminders:*\n"
                for rem in self_reminders[:5]:
                    try:
                        time_dt = deserialize_datetime(rem['scheduled_time'])
                        time_str = time_dt.strftime('%I:%M %p')
                    except Exception:
                        time_str = "Today"
                    status_emoji = "⏳" if rem['status'] == 'pending' else "📬"
                    overview_msg += f"{status_emoji} {rem['message'][:35]}... — {time_str}\n"
            
            # Reminders for others
            if others_reminders:
                overview_msg += "\n*Caring for Others:*\n"
                for rem in others_reminders[:5]:
                    name = rem.get('recipient_name') or rem.get('recipient_relationship', 'Contact')
                    status = "Awaiting consent" if rem['status'] == 'awaiting_consent' else "Scheduled"
                    overview_msg += f"💛 {name}: {rem['message'][:25]}... ({status})\n"
            
            # Habits
            if habit_logs:
                overview_msg += "\n*Today's Habits:*\n"
                for log in habit_logs[:5]:
                    habit = await db.habits.find_one({"id": log['habit_id']}, {"_id": 0})
                    if habit:
                        status_emoji = "⏰" if log['status'] == 'reminded' else "📌"
                        overview_msg += f"{status_emoji} {habit['name']} — {habit.get('time', 'Today')}\n"
            
            overview_msg += "\n*Quick Actions:*\n"
            overview_msg += "• Reply *Done* after completing a task\n"
            overview_msg += "• Reply *Later* to snooze 10 minutes\n"
            overview_msg += "• Reply *Skip* to skip a reminder\n"
            overview_msg += "\n— Daisy 💛"
            
            response_text = overview_msg
    
    elif parsed.get('intent') == 'help':
        response_text = parsed.get('friendly_response', """Hi! I'm Daisy, your friendly life assistant. 🌼

Here's what I can do:

📌 **REMINDERS:**
• "Remind me to [task] in [time]"
• "Remind my dad to [task] at [time]"

🏢 **TEAMS:**
• "Create team [name]"
• "Remind [team] to [task]"

🌱 **HABITS (NEW!):**
• "I want to start meditating daily at 6 AM"
• "Show my habits"
• "How am I doing with exercise?"

Try: "I want to build a habit of reading every day at 9 PM" """)
    
    elif parsed.get('intent') == 'general_chat':
        response_text = parsed.get('friendly_response', "Hi! I'm Daisy 🌼 I can help you set reminders. Just tell me what you'd like to be reminded about!")
    
    elif parsed.get('intent') == 'list_reminders':
        # Show pending reminders for this user
        reminders = await db.reminders.find(
            {"$or": [{"creator_id": user['id'] if user else ""}, {"recipient_phone": from_phone}], "status": "pending"},
            {"_id": 0}
        ).to_list(5)
        
        if reminders:
            reminder_list = "\n".join([f"• {r['message']} at {r['scheduled_time']}" for r in reminders[:5]])
            response_text = f"Here are your upcoming reminders:\n{reminder_list}\n\n🌼"
        else:
            response_text = "You don't have any pending reminders. Want me to set one? 🌼"
    
    else:
        # Unknown intent - use AI's friendly response or generate one
        if not response_text:
            response_text = await generate_response(
                f"User message type: {parsed.get('intent', 'unknown')}",
                Body
            )
    
    # Store outgoing message
    if response_text:
        out_message = Message(
            direction="outgoing",
            from_phone=to_phone,
            to_phone=from_phone,
            content=response_text
        )
        out_dict = out_message.model_dump()
        out_dict['created_at'] = serialize_datetime(out_dict['created_at'])
        await db.messages.insert_one(out_dict)
        
        # Send via Twilio if configured
        if is_twilio_configured():
            await send_whatsapp_message(from_phone, response_text)
    
    # Return TwiML response (empty is fine, we send separately)
    return ""


# ============== SETTINGS ROUTES ==============

@api_router.get("/settings/twilio")
async def get_twilio_status():
    """Check if Twilio is configured"""
    return {"configured": is_twilio_configured()}


@api_router.put("/auth/profile")
async def update_profile(
    name: Optional[str] = None,
    phone: Optional[str] = None,
    timezone_str: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """Update user profile"""
    update_data = {"updated_at": serialize_datetime(datetime.now(timezone.utc))}
    if name:
        update_data["name"] = name
    if phone:
        update_data["phone"] = phone
    if timezone_str:
        update_data["timezone"] = timezone_str
    
    await db.users.update_one(
        {"id": current_user['user_id']},
        {"$set": update_data}
    )
    return {"message": "Profile updated"}


# ============== ADMIN PANEL ROUTES ==============

@api_router.get("/admin/overview")
async def admin_overview():
    """
    Admin overview - get all system statistics and data counts
    Access: This endpoint provides admin-level overview
    """
    total_users = await db.users.count_documents({})
    total_whatsapp_users = await db.whatsapp_users.count_documents({})
    total_contacts = await db.contacts.count_documents({})
    total_reminders = await db.reminders.count_documents({})
    total_messages = await db.messages.count_documents({})
    pending_reminders = await db.reminders.count_documents({"status": "pending"})
    sent_reminders = await db.reminders.count_documents({"status": "sent"})
    acknowledged_reminders = await db.reminders.count_documents({"status": "acknowledged"})
    awaiting_consent = await db.reminders.count_documents({"status": "awaiting_consent"})
    approved_contacts = await db.contacts.count_documents({"consent_status": "approved"})
    pending_contacts = await db.contacts.count_documents({"consent_status": "pending"})
    
    # Habit stats
    total_habits = await db.habits.count_documents({})
    active_habits = await db.habits.count_documents({"status": "active"})
    paused_habits = await db.habits.count_documents({"status": "paused"})
    total_habit_logs = await db.habit_logs.count_documents({})
    completed_habit_logs = await db.habit_logs.count_documents({"status": "completed"})
    missed_habit_logs = await db.habit_logs.count_documents({"status": "missed"})
    
    # Team stats
    total_teams = await db.teams.count_documents({})
    total_team_reminders = await db.team_reminders.count_documents({})
    
    return {
        "overview": {
            "total_registered_users": total_users,
            "total_whatsapp_users": total_whatsapp_users,
            "total_contacts": total_contacts,
            "total_reminders": total_reminders,
            "total_messages": total_messages,
            "total_teams": total_teams,
            "total_habits": total_habits,
        },
        "reminders_breakdown": {
            "pending": pending_reminders,
            "sent": sent_reminders,
            "acknowledged": acknowledged_reminders,
            "awaiting_consent": awaiting_consent,
        },
        "contacts_breakdown": {
            "approved": approved_contacts,
            "pending": pending_contacts,
        },
        "habits_breakdown": {
            "active": active_habits,
            "paused": paused_habits,
            "total_logs": total_habit_logs,
            "completed_logs": completed_habit_logs,
            "missed_logs": missed_habit_logs,
        },
        "teams_breakdown": {
            "total_teams": total_teams,
            "total_team_reminders": total_team_reminders,
        },
        "database": {
            "name": os.environ.get('DB_NAME'),
            "collections": ["users", "whatsapp_users", "contacts", "reminders", "messages", "pending_reminders", "teams", "team_members", "team_reminders", "habits", "habit_logs"]
        }
    }


@api_router.get("/admin/users")
async def admin_list_users():
    """List all registered web users (admin view)"""
    users = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(100)
    return {"count": len(users), "users": users}


@api_router.get("/admin/whatsapp-users")
async def admin_list_whatsapp_users():
    """List all WhatsApp-only users (admin view)"""
    whatsapp_users = await db.whatsapp_users.find({}, {"_id": 0}).to_list(100)
    return {"count": len(whatsapp_users), "whatsapp_users": whatsapp_users}


@api_router.get("/admin/contacts")
async def admin_list_contacts():
    """List all contacts across all users (admin view)"""
    contacts = await db.contacts.find({}, {"_id": 0}).to_list(500)
    return {"count": len(contacts), "contacts": contacts}


@api_router.get("/admin/reminders")
async def admin_list_reminders(reminder_status: Optional[str] = None, limit: int = 100):
    """List all reminders (admin view) - optionally filter by status"""
    query = {}
    if reminder_status:
        query["status"] = reminder_status
    reminders = await db.reminders.find(query, {"_id": 0}).sort("scheduled_time", -1).to_list(limit)
    return {"count": len(reminders), "reminders": reminders}


@api_router.get("/admin/messages")
async def admin_list_messages(limit: int = 100):
    """List recent messages (admin view)"""
    messages = await db.messages.find({}, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return {"count": len(messages), "messages": messages}


@api_router.get("/admin/pending-actions")
async def admin_list_pending_actions():
    """List all pending conversational actions (admin view)"""
    pending = await db.pending_reminders.find({}, {"_id": 0}).to_list(100)
    return {"count": len(pending), "pending_actions": pending}


@api_router.get("/admin/teams")
async def admin_list_teams():
    """List all teams (admin view)"""
    teams = await db.teams.find({}, {"_id": 0}).to_list(100)
    result = []
    for team in teams:
        member_count = await db.team_members.count_documents({"team_id": team['id'], "status": "approved"})
        pending_count = await db.team_members.count_documents({"team_id": team['id'], "status": "pending"})
        team['member_count'] = member_count
        team['pending_members'] = pending_count
        result.append(team)
    return {"count": len(result), "teams": result}


@api_router.get("/admin/team-members")
async def admin_list_all_team_members():
    """List all team members across all teams (admin view)"""
    members = await db.team_members.find({}, {"_id": 0}).to_list(500)
    return {"count": len(members), "members": members}


@api_router.get("/admin/team-reminders")
async def admin_list_team_reminders(reminder_status: Optional[str] = None):
    """List all team reminders (admin view)"""
    query = {}
    if reminder_status:
        query["status"] = reminder_status
    reminders = await db.team_reminders.find(query, {"_id": 0}).sort("scheduled_time", -1).to_list(100)
    return {"count": len(reminders), "team_reminders": reminders}


@api_router.get("/admin/team-acknowledgments")
async def admin_list_team_acks(team_reminder_id: Optional[str] = None):
    """List team reminder acknowledgments (admin view)"""
    query = {}
    if team_reminder_id:
        query["team_reminder_id"] = team_reminder_id
    acks = await db.team_reminder_acks.find(query, {"_id": 0}).to_list(500)
    return {"count": len(acks), "acknowledgments": acks}


@api_router.get("/admin/multi-time-reminders")
async def admin_list_multi_time_reminders(reminder_status: Optional[str] = None):
    """List all multi-time reminders (admin view)"""
    query = {}
    if reminder_status:
        query["status"] = reminder_status
    reminders = await db.multi_time_reminders.find(query, {"_id": 0}).sort("created_at", -1).to_list(100)
    return {"count": len(reminders), "multi_time_reminders": reminders}


@api_router.get("/admin/habits")
async def admin_list_habits(habit_status: Optional[str] = None):
    """List all habits (admin view)"""
    query = {}
    if habit_status:
        query["status"] = habit_status
    habits = await db.habits.find(query, {"_id": 0}).sort("created_at", -1).to_list(100)
    return {"count": len(habits), "habits": habits}


@api_router.get("/admin/habit-logs")
async def admin_list_habit_logs(habit_id: Optional[str] = None, log_status: Optional[str] = None, limit: int = 100):
    """List habit logs (admin view)"""
    query = {}
    if habit_id:
        query["habit_id"] = habit_id
    if log_status:
        query["status"] = log_status
    logs = await db.habit_logs.find(query, {"_id": 0}).sort("scheduled_date", -1).to_list(limit)
    return {"count": len(logs), "habit_logs": logs}


@api_router.get("/admin/habit-modifications")
async def admin_list_habit_modifications(habit_id: Optional[str] = None):
    """List habit modification history (admin view)"""
    query = {}
    if habit_id:
        query["habit_id"] = habit_id
    modifications = await db.habit_modifications.find(query, {"_id": 0}).sort("modified_at", -1).to_list(100)
    return {"count": len(modifications), "modifications": modifications}


@api_router.get("/admin/pending-habits")
async def admin_list_pending_habits():
    """List pending habit creations awaiting confirmation (admin view)"""
    pending = await db.pending_habits.find({}, {"_id": 0}).to_list(100)
    return {"count": len(pending), "pending_habits": pending}


@api_router.get("/admin/system-health")
async def admin_system_health():
    """
    Comprehensive system health check for owner dashboard.
    Includes API status, database status, integrations, and resource info.
    """
    from datetime import datetime, timezone
    
    # Check Twilio status
    twilio_configured = is_twilio_configured()
    twilio_number = os.environ.get('TWILIO_WHATSAPP_NUMBER', 'Not configured')
    
    # Check LLM Key status
    llm_key = os.environ.get('EMERGENT_LLM_KEY', '')
    llm_configured = bool(llm_key and len(llm_key) > 10)
    
    # Database connectivity check
    db_status = "healthy"
    try:
        await db.command('ping')
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    # Get scheduler status
    from scheduler import scheduler
    scheduler_running = scheduler is not None and scheduler.running if scheduler else False
    
    return {
        "status": "healthy" if db_status == "healthy" else "degraded",
        "timestamp": serialize_datetime(datetime.now(timezone.utc)),
        "database": {
            "status": db_status,
            "name": os.environ.get('DB_NAME', 'unknown')
        },
        "integrations": {
            "twilio": {
                "configured": twilio_configured,
                "whatsapp_number": twilio_number if twilio_configured else None
            },
            "openai": {
                "configured": llm_configured,
                "model": "gpt-5.2" if llm_configured else None
            }
        },
        "scheduler": {
            "running": scheduler_running
        },
        "environment": {
            "frontend_url": os.environ.get('FRONTEND_URL', os.environ.get('REACT_APP_BACKEND_URL', 'Not set')),
            "cors_origins": os.environ.get('CORS_ORIGINS', '*')
        }
    }


@api_router.get("/admin/subscriptions")
async def admin_subscriptions_overview():
    """
    Get subscription statistics and user subscription breakdown.
    """
    # Get all users with subscription info
    users = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(1000)
    
    now = datetime.now(timezone.utc)
    
    # Categorize by subscription status
    trial_users = []
    active_users = []
    expired_users = []
    cancelled_users = []
    
    for user in users:
        sub_status = user.get('subscription_status', 'trial')
        trial_end = user.get('trial_end')
        
        # Parse trial_end if string
        if isinstance(trial_end, str):
            try:
                trial_end = datetime.fromisoformat(trial_end.replace('Z', '+00:00'))
            except Exception:
                trial_end = None
        
        user_summary = {
            "id": user.get('id'),
            "name": user.get('name'),
            "email": user.get('email'),
            "phone": user.get('phone'),
            "subscription_status": sub_status,
            "trial_end": serialize_datetime(trial_end) if trial_end else None,
            "created_at": user.get('created_at'),
            "days_remaining": (trial_end - now).days if trial_end and trial_end > now else 0
        }
        
        if sub_status == 'trial':
            if trial_end and trial_end > now:
                trial_users.append(user_summary)
            else:
                user_summary['subscription_status'] = 'trial_expired'
                expired_users.append(user_summary)
        elif sub_status == 'active':
            active_users.append(user_summary)
        elif sub_status == 'expired':
            expired_users.append(user_summary)
        elif sub_status == 'cancelled':
            cancelled_users.append(user_summary)
    
    return {
        "summary": {
            "total_users": len(users),
            "trial": len(trial_users),
            "active": len(active_users),
            "expired": len(expired_users),
            "cancelled": len(cancelled_users)
        },
        "trial_users": trial_users,
        "active_users": active_users,
        "expired_users": expired_users,
        "cancelled_users": cancelled_users
    }


@api_router.get("/admin/activity-log")
async def admin_activity_log(limit: int = 50):
    """
    Get recent system activity (messages, reminders sent, etc.)
    """
    # Get recent messages
    recent_messages = await db.messages.find(
        {}, {"_id": 0}
    ).sort("created_at", -1).to_list(limit)
    
    # Get recently sent reminders
    recent_reminders = await db.reminders.find(
        {"status": {"$in": ["sent", "acknowledged"]}},
        {"_id": 0}
    ).sort("last_sent_at", -1).to_list(limit)
    
    # Get recent habit completions
    recent_habit_logs = await db.habit_logs.find(
        {"status": {"$in": ["completed", "missed"]}},
        {"_id": 0}
    ).sort("updated_at", -1).to_list(limit)
    
    # Build activity timeline
    activities = []
    
    for msg in recent_messages[:20]:
        activities.append({
            "type": "message",
            "direction": msg.get('direction'),
            "from": msg.get('from_phone'),
            "to": msg.get('to_phone'),
            "preview": msg.get('content', '')[:100],
            "timestamp": msg.get('created_at')
        })
    
    for rem in recent_reminders[:15]:
        activities.append({
            "type": "reminder",
            "status": rem.get('status'),
            "message": rem.get('message', '')[:50],
            "recipient": rem.get('recipient_name') or rem.get('recipient_phone'),
            "timestamp": rem.get('last_sent_at') or rem.get('created_at')
        })
    
    for log in recent_habit_logs[:15]:
        habit = await db.habits.find_one({"id": log.get('habit_id')}, {"_id": 0, "name": 1})
        activities.append({
            "type": "habit",
            "status": log.get('status'),
            "habit_name": habit.get('name') if habit else 'Unknown',
            "user_phone": log.get('user_phone'),
            "timestamp": log.get('updated_at')
        })
    
    # Sort by timestamp
    activities.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    
    return {
        "count": len(activities),
        "activities": activities[:limit]
    }


@api_router.get("/admin/analytics")
async def admin_analytics():
    """
    Get analytics data for dashboard charts
    """
    now = datetime.now(timezone.utc)
    
    # Get data for last 30 days
    thirty_days_ago = now - timedelta(days=30)
    thirty_days_ago_str = serialize_datetime(thirty_days_ago)
    
    # Messages per day
    messages = await db.messages.find(
        {"created_at": {"$gte": thirty_days_ago_str}},
        {"_id": 0, "created_at": 1, "direction": 1}
    ).to_list(10000)
    
    # Group messages by day
    messages_by_day = {}
    for msg in messages:
        try:
            date = msg.get('created_at', '')[:10]
            if date not in messages_by_day:
                messages_by_day[date] = {"incoming": 0, "outgoing": 0}
            messages_by_day[date][msg.get('direction', 'incoming')] += 1
        except Exception:
            pass
    
    # Reminders created per day
    reminders = await db.reminders.find(
        {"created_at": {"$gte": thirty_days_ago_str}},
        {"_id": 0, "created_at": 1, "status": 1}
    ).to_list(5000)
    
    reminders_by_day = {}
    for rem in reminders:
        try:
            date = rem.get('created_at', '')[:10]
            if date not in reminders_by_day:
                reminders_by_day[date] = 0
            reminders_by_day[date] += 1
        except Exception:
            pass
    
    # User signups per day
    users = await db.users.find(
        {"created_at": {"$gte": thirty_days_ago_str}},
        {"_id": 0, "created_at": 1}
    ).to_list(1000)
    
    signups_by_day = {}
    for user in users:
        try:
            date = user.get('created_at', '')[:10]
            if date not in signups_by_day:
                signups_by_day[date] = 0
            signups_by_day[date] += 1
        except Exception:
            pass
    
    # Habit completion rate over time
    habit_logs = await db.habit_logs.find(
        {"scheduled_date": {"$gte": thirty_days_ago.strftime('%Y-%m-%d')}},
        {"_id": 0, "scheduled_date": 1, "status": 1}
    ).to_list(10000)
    
    habits_by_day = {}
    for log in habit_logs:
        date = log.get('scheduled_date')
        if date not in habits_by_day:
            habits_by_day[date] = {"completed": 0, "missed": 0, "total": 0}
        habits_by_day[date]["total"] += 1
        if log.get('status') == 'completed':
            habits_by_day[date]["completed"] += 1
        elif log.get('status') == 'missed':
            habits_by_day[date]["missed"] += 1
    
    return {
        "period": {
            "start": thirty_days_ago_str,
            "end": serialize_datetime(now)
        },
        "messages_by_day": messages_by_day,
        "reminders_by_day": reminders_by_day,
        "signups_by_day": signups_by_day,
        "habits_by_day": habits_by_day,
        "totals": {
            "messages": len(messages),
            "reminders": len(reminders),
            "signups": len(users),
            "habit_logs": len(habit_logs)
        }
    }


@api_router.get("/admin/users/{user_id}")
async def admin_get_user_details(user_id: str):
    """Get detailed information about a specific user"""
    # Try to find in registered users
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    
    if not user:
        # Try WhatsApp users
        user = await db.whatsapp_users.find_one({"id": user_id}, {"_id": 0})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
    
    phone = user.get('phone')
    
    # Get user's reminders
    reminders = await db.reminders.find(
        {"creator_id": user_id},
        {"_id": 0}
    ).sort("created_at", -1).to_list(50)
    
    # Get user's habits
    habits_query = {"user_id": user_id}
    if phone:
        habits_query = {"$or": [{"user_id": user_id}, {"user_phone": phone}]}
    habits = await db.habits.find(habits_query, {"_id": 0}).to_list(50)
    
    # Get user's contacts
    contacts = await db.contacts.find({"user_id": user_id}, {"_id": 0}).to_list(100)
    
    # Get user's teams
    team_memberships = await db.team_members.find({"phone": phone}, {"_id": 0}).to_list(50) if phone else []
    
    return {
        "user": user,
        "stats": {
            "total_reminders": len(reminders),
            "total_habits": len(habits),
            "total_contacts": len(contacts),
            "total_teams": len(team_memberships)
        },
        "reminders": reminders[:10],
        "habits": habits,
        "contacts": contacts[:10],
        "team_memberships": team_memberships
    }


# ============== HEALTH CHECK ==============

@api_router.get("/")
async def root():
    return {"message": "Daisy API - AI Life Concierge", "status": "healthy"}


@api_router.get("/health")
async def health_check():
    return {"status": "healthy", "twilio_configured": is_twilio_configured()}


# ============== ONBOARDING & QR CODE ==============

@api_router.get("/onboarding/whatsapp-link")
async def get_whatsapp_link():
    """Get the WhatsApp click-to-chat link for Daisy"""
    twilio_number = os.environ.get('TWILIO_WHATSAPP_NUMBER', '')
    # Remove + and any spaces
    clean_number = twilio_number.replace('+', '').replace(' ', '').replace('-', '')
    
    # Click-to-chat link with pre-filled message
    whatsapp_link = f"https://wa.me/{clean_number}?text=Hi%20Daisy!"
    
    # QR Code links in multiple sizes for different use cases
    qr_sizes = {
        "small": 300,      # Social media, digital ads
        "medium": 500,     # Flyers, business cards
        "large": 1000,     # Posters, billboards
        "xlarge": 2000     # Large format printing
    }
    
    qr_codes = {}
    for size_name, size in qr_sizes.items():
        qr_codes[size_name] = {
            "size": f"{size}x{size}",
            "url": f"https://api.qrserver.com/v1/create-qr-code/?size={size}x{size}&data={whatsapp_link}&format=png&margin=10"
        }
    
    return {
        "whatsapp_number": twilio_number,
        "click_to_chat_link": whatsapp_link,
        "qr_code_url": qr_codes["small"]["url"],  # Default for backward compatibility
        "qr_codes": qr_codes,
        "instructions": "Scan the QR code or click the link to start chatting with Daisy on WhatsApp!",
        "usage_guide": {
            "digital_ads": "Use 'small' (300px) for social media and websites",
            "flyers": "Use 'medium' (500px) for flyers and business cards",
            "posters": "Use 'large' (1000px) for posters and banners",
            "billboards": "Use 'xlarge' (2000px) for billboards and large prints"
        }
    }


@api_router.get("/onboarding/user-stats")
async def get_onboarding_stats():
    """Get statistics about user onboarding (admin view)"""
    total_users = await db.whatsapp_users.count_documents({})
    pending_consent = await db.whatsapp_users.count_documents({"user_type": "pending_consent"})
    active_users = await db.whatsapp_users.count_documents({"user_type": "active_user"})
    recipients_only = await db.whatsapp_users.count_documents({"user_type": "recipient_only"})
    declined = await db.whatsapp_users.count_documents({"user_type": "declined"})
    
    # Subscription breakdown for active users
    trial_users = await db.whatsapp_users.count_documents({"user_type": "active_user", "subscription_status": "trial"})
    paid_users = await db.whatsapp_users.count_documents({"user_type": "active_user", "subscription_status": "active"})
    expired_users = await db.whatsapp_users.count_documents({"user_type": "active_user", "subscription_status": "expired"})
    
    return {
        "total_whatsapp_users": total_users,
        "user_type_breakdown": {
            "pending_consent": pending_consent,
            "active_users": active_users,
            "recipients_only": recipients_only,
            "declined": declined
        },
        "subscription_breakdown": {
            "trial": trial_users,
            "paid": paid_users,
            "expired": expired_users
        },
        "conversion_rate": round((active_users / total_users * 100), 1) if total_users > 0 else 0
    }


@api_router.get("/privacy")
async def privacy_policy():
    """Return Daisy's privacy policy"""
    return {
        "title": "Daisy Privacy Policy",
        "version": "1.0",
        "last_updated": "2026-02-28",
        "content": {
            "introduction": "Daisy is an AI-powered life concierge that helps you manage reminders, habits, and communications via WhatsApp. This policy explains how we collect, use, and protect your data.",
            "data_collected": [
                "Phone number (to identify and communicate with you)",
                "Messages you send to Daisy (to understand and fulfill your requests)",
                "Reminders you create (content, times, recipients)",
                "Habits you track (names, schedules, completion status)",
                "Contact information you share (names, phone numbers of people you want to remind)"
            ],
            "how_we_use_data": [
                "To send you reminders at your requested times",
                "To track and report on your habit progress",
                "To personalize Daisy's responses based on your history",
                "To improve our AI's understanding of requests",
                "To send reminders to recipients on your behalf (with their consent)"
            ],
            "data_sharing": "We never sell your personal data. We share data only with: Twilio (for WhatsApp messaging), OpenAI (for processing messages - anonymized), MongoDB (secure cloud storage).",
            "data_retention": "Your data is retained as long as your account is active. You can request deletion at any time by messaging 'Delete my data' to Daisy.",
            "your_rights": [
                "Access: Request a copy of your data",
                "Deletion: Request deletion of all your data",
                "Correction: Update or correct your information",
                "Withdrawal: Revoke consent at any time"
            ],
            "contact": "For privacy concerns, message Daisy with 'Privacy help' or email privacy@daisy-app.com"
        }
    }


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)
