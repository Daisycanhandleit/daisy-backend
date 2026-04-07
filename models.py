from pydantic import BaseModel, Field, ConfigDict, EmailStr
from typing import Optional, List, Literal
from datetime import datetime, timezone
import uuid


def generate_uuid():
    return str(uuid.uuid4())


def get_utc_now():
    return datetime.now(timezone.utc)


# Admin Model
class Admin(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    email: EmailStr
    password_hash: str
    name: str
    role: Literal["owner", "admin"] = "admin"
    created_at: datetime = Field(default_factory=get_utc_now)
    last_login: Optional[datetime] = None


class AdminLogin(BaseModel):
    email: EmailStr
    password: str


# User Models
class UserBase(BaseModel):
    email: EmailStr
    name: str
    phone: Optional[str] = None
    timezone: str = "UTC"


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str
    phone: Optional[str] = None
    timezone: str = "UTC"


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class User(UserBase):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    password_hash: str
    subscription_status: Literal["trial", "active", "expired", "cancelled"] = "trial"
    trial_end: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    email: str
    name: str
    phone: Optional[str] = None
    timezone: str
    subscription_status: str
    trial_end: str
    created_at: str


# Contact Models
class ContactBase(BaseModel):
    name: str
    phone: str
    relationship: Optional[str] = None  # e.g., "mother", "team_member", "subordinate"


class ContactCreate(ContactBase):
    pass


class Contact(ContactBase):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    user_id: str
    consent_status: Literal["pending", "approved", "declined", "revoked"] = "pending"
    consent_date: Optional[datetime] = None
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class ContactResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    name: str
    phone: str
    relationship: Optional[str] = None
    consent_status: str
    consent_date: Optional[str] = None
    created_at: str


# WhatsApp User Models (for tracking user types and consent)
class WhatsAppUser(BaseModel):
    """
    Tracks all WhatsApp users interacting with Daisy.
    - new_user: First-time user, needs to accept privacy policy
    - pending_consent: Sent privacy policy, waiting for acceptance
    - recipient_only: Only receives reminders from others (FREE forever)
    - active_user: Accepted terms, can use Daisy (trial or paid)
    """
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    phone: str
    name: Optional[str] = None
    user_type: Literal["new_user", "pending_consent", "recipient_only", "active_user"] = "new_user"
    
    # Privacy & Consent
    privacy_consent_accepted: bool = False
    privacy_consent_date: Optional[datetime] = None
    privacy_policy_version: str = "1.0"
    data_storage_consent: bool = False
    
    # Subscription (only for active_user)
    subscription_status: Literal["none", "trial", "active", "expired", "cancelled"] = "none"
    trial_start: Optional[datetime] = None
    trial_end: Optional[datetime] = None
    
    # Daily Agenda Settings (Smart Reminder System)
    morning_agenda_time: str = "07:00"  # When to send morning briefing
    evening_wrapup_time: str = "21:00"  # When to send evening summary
    timezone: str = "UTC"
    agenda_enabled: bool = True  # User can disable daily agenda
    
    # Tracking
    first_interaction: datetime = Field(default_factory=get_utc_now)
    last_interaction: datetime = Field(default_factory=get_utc_now)
    total_messages_sent: int = 0
    total_reminders_received: int = 0  # As recipient
    total_reminders_created: int = 0   # As user
    
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


# Reminder Models
class ReminderBase(BaseModel):
    message: str
    scheduled_time: datetime
    recipient_phone: str
    recipient_name: Optional[str] = None
    recurrence: Literal["once", "daily", "weekly", "monthly"] = "once"
    end_date: Optional[datetime] = None


class ReminderCreate(ReminderBase):
    pass


class Reminder(ReminderBase):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    creator_id: str
    creator_phone: Optional[str] = None
    creator_name: Optional[str] = None
    contact_id: Optional[str] = None
    recipient_relationship: Optional[str] = None  # "mom", "dad", "brother" etc.
    
    # Status tracking
    status: Literal["pending", "sent", "acknowledged", "failed", "cancelled", "awaiting_consent", "skipped", "snoozed"] = "pending"
    acknowledgment: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    
    # Smart Follow-up System (max 2 follow-ups, then stop)
    follow_up_count: int = 0
    max_follow_ups: int = 2  # Changed from 3 to 2 (less spam)
    follow_up_intervals: List[int] = [10, 30]  # Minutes: 10min, then 30min
    last_follow_up_at: Optional[datetime] = None
    
    # Completion tracking
    completed: bool = False
    completed_at: Optional[datetime] = None
    skipped: bool = False
    snoozed_until: Optional[datetime] = None
    
    # For recipient reminders - track if creator was notified
    creator_notified_of_completion: bool = False
    creator_notified_of_pending: bool = False
    
    # Included in daily agenda?
    included_in_morning_agenda: bool = False
    included_in_evening_wrapup: bool = False
    
    last_sent_at: Optional[datetime] = None
    next_occurrence: Optional[datetime] = None
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class ReminderResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    message: str
    scheduled_time: str
    recipient_phone: str
    recipient_name: Optional[str] = None
    recurrence: str
    status: str
    acknowledgment: Optional[str] = None
    follow_up_count: int
    created_at: str


# Multi-Time Reminder Models (Send NOW + specific times until acknowledged)
class ReminderTime(BaseModel):
    """Individual reminder time within a multi-time reminder"""
    time: datetime
    label: Optional[str] = None  # e.g., "now", "before deadline", "deadline"
    status: Literal["pending", "sent"] = "pending"
    sent_at: Optional[datetime] = None


class MultiTimeReminder(BaseModel):
    """Reminder that gets sent at multiple specified times until acknowledged"""
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    creator_id: str
    creator_phone: str
    creator_name: Optional[str] = None
    recipient_phone: str
    recipient_name: Optional[str] = None
    message: str
    
    # Multiple scheduled times
    reminder_times: List[dict] = []  # List of {time, label, status, sent_at}
    
    # The main deadline/task time
    deadline_time: Optional[datetime] = None
    
    # Send immediately flag
    send_now: bool = False
    
    # Status tracking
    status: Literal["active", "acknowledged", "cancelled", "expired"] = "active"
    acknowledgment: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    
    # Follow-up settings (after all scheduled times sent)
    enable_followups: bool = True
    followup_interval_minutes: int = 30
    followup_count: int = 0
    max_followups: int = 10
    last_followup_at: Optional[datetime] = None
    
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class MultiTimeReminderResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    creator_name: Optional[str]
    recipient_name: Optional[str]
    recipient_phone: str
    message: str
    reminder_times: List[dict]
    deadline_time: Optional[str]
    status: str
    acknowledgment: Optional[str]
    created_at: str


# Message Models
class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    direction: Literal["incoming", "outgoing"]
    from_phone: str
    to_phone: str
    content: str
    message_type: Literal["reminder", "consent_request", "consent_response", "follow_up", "acknowledgment", "general"] = "general"
    reminder_id: Optional[str] = None
    twilio_sid: Optional[str] = None
    status: Literal["sent", "delivered", "read", "failed"] = "sent"
    created_at: datetime = Field(default_factory=get_utc_now)


class MessageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    direction: str
    from_phone: str
    to_phone: str
    content: str
    message_type: str
    status: str
    created_at: str


# WhatsApp Webhook Models
class WhatsAppIncoming(BaseModel):
    From: str
    To: str
    Body: str
    MessageSid: Optional[str] = None
    AccountSid: Optional[str] = None


# AI Request/Response Models
class AIParseResult(BaseModel):
    intent: Literal["create_reminder", "list_reminders", "cancel_reminder", "acknowledge", "consent_response", "help", "unknown"]
    message: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_phone: Optional[str] = None
    scheduled_time: Optional[str] = None
    recurrence: Optional[str] = None
    consent: Optional[bool] = None
    reminder_id: Optional[str] = None
    confidence: float = 0.0


# Dashboard Stats
class DashboardStats(BaseModel):
    total_reminders: int = 0
    pending_reminders: int = 0
    sent_today: int = 0
    acknowledged_today: int = 0
    total_contacts: int = 0
    pending_consents: int = 0
    approved_consents: int = 0


# Auth Response
class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


# ============== TEAM MODELS ==============

def generate_invite_code():
    """Generate a short invite code for team joining"""
    import secrets
    return secrets.token_urlsafe(8)


class TeamCreate(BaseModel):
    name: str
    description: Optional[str] = None


class Team(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    name: str
    description: Optional[str] = None
    owner_id: str  # User ID or WhatsApp phone of creator
    owner_phone: str  # WhatsApp phone of owner
    invite_code: str = Field(default_factory=generate_invite_code)
    is_active: bool = True
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class TeamResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    name: str
    description: Optional[str] = None
    owner_id: str
    owner_phone: str
    invite_code: str
    is_active: bool
    member_count: int = 0
    created_at: str


class TeamMemberCreate(BaseModel):
    phone: str
    name: Optional[str] = None
    role: Literal["admin", "member"] = "member"


class TeamMember(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    team_id: str
    phone: str
    name: Optional[str] = None
    role: Literal["owner", "admin", "member"] = "member"
    status: Literal["pending", "approved", "declined", "removed"] = "pending"
    added_by: str  # Phone of who added this member
    approved_by: Optional[str] = None  # Phone of who approved
    approved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class TeamMemberResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    team_id: str
    phone: str
    name: Optional[str] = None
    role: str
    status: str
    added_by: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    created_at: str


class TeamReminderCreate(BaseModel):
    team_id: str
    message: str
    scheduled_time: datetime
    recurrence: Literal["once", "daily", "weekly", "monthly"] = "once"
    end_date: Optional[datetime] = None
    persist_until_all_acknowledge: bool = True  # Keep reminding until everyone responds


class TeamReminder(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    team_id: str
    team_name: str
    creator_id: str
    creator_phone: str
    message: str
    scheduled_time: datetime
    recurrence: Literal["once", "daily", "weekly", "monthly"] = "once"
    end_date: Optional[datetime] = None
    persist_until_all_acknowledge: bool = True
    status: Literal["pending", "in_progress", "completed", "cancelled"] = "pending"
    total_members: int = 0
    acknowledged_count: int = 0
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class TeamReminderResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    team_id: str
    team_name: str
    message: str
    scheduled_time: str
    recurrence: str
    status: str
    total_members: int
    acknowledged_count: int
    persist_until_all_acknowledge: bool
    created_at: str


class TeamReminderAcknowledgment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    team_reminder_id: str
    member_phone: str
    member_name: Optional[str] = None
    status: Literal["pending", "sent", "acknowledged"] = "pending"
    acknowledgment_text: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    follow_up_count: int = 0
    last_sent_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=get_utc_now)



# ============== HABIT SYSTEM MODELS ==============

# Habit Categories
HABIT_CATEGORIES = ["Health", "Work", "Learning", "Spiritual", "Finance", "Relationships", "Custom"]

# Reminder Intensity Configs (follow-up timing in minutes)
REMINDER_INTENSITY_CONFIG = {
    "gentle": {"first_followup": 30, "second_followup": 60, "final_warning": 120},
    "standard": {"first_followup": 15, "second_followup": 30, "final_warning": 60},
    "strict": {"first_followup": 5, "second_followup": 15, "final_warning": 30},
}


class HabitCreate(BaseModel):
    name: str
    category: Literal["Health", "Work", "Learning", "Spiritual", "Finance", "Relationships", "Custom"] = "Custom"
    frequency: Literal["daily", "weekly", "custom"] = "daily"
    custom_days: Optional[List[str]] = None  # ["Monday", "Wednesday", "Friday"]
    time: str  # "06:00" format
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    difficulty: int = 3  # 1-5 scale
    reminder_intensity: Literal["gentle", "standard", "strict", "custom"] = "standard"
    custom_followup_minutes: Optional[List[int]] = None  # [10, 20, 40] for custom intensity


class Habit(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    user_id: str  # User ID or WhatsApp phone
    user_phone: str  # WhatsApp phone for sending reminders
    
    # Core habit info
    name: str
    category: Literal["Health", "Work", "Learning", "Spiritual", "Finance", "Relationships", "Custom"] = "Custom"
    description: Optional[str] = None
    
    # Scheduling
    frequency: Literal["daily", "weekly", "custom"] = "daily"
    custom_days: Optional[List[str]] = None  # ["Monday", "Wednesday", "Friday"]
    time: str  # "06:00" 24-hour format
    timezone: str = "UTC"
    
    # Duration
    start_date: datetime = Field(default_factory=get_utc_now)
    end_date: Optional[datetime] = None
    
    # Difficulty & Reminders
    difficulty: int = 3  # 1-5 scale
    reminder_intensity: Literal["gentle", "standard", "strict", "custom"] = "standard"
    custom_followup_minutes: Optional[List[int]] = None
    
    # Status
    status: Literal["active", "paused", "completed", "deleted"] = "active"
    
    # Stats (updated in real-time)
    current_streak: int = 0
    longest_streak: int = 0
    total_completions: int = 0
    total_missed: int = 0
    
    # Sharing & Collaboration
    shared_with: Optional[List[str]] = None  # List of phone numbers who can view/edit
    is_shared: bool = False
    shared_by: Optional[str] = None  # Who originally created & shared this
    
    # Edit tracking
    last_edited_by: Optional[str] = None  # Phone/name of last editor
    last_edit_description: Optional[str] = None  # What was changed
    edit_history: Optional[List[dict]] = None  # [{editor, change, timestamp}]
    
    # Timestamps
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)
    last_completed_at: Optional[datetime] = None


class HabitResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    name: str
    category: str
    frequency: str
    custom_days: Optional[List[str]]
    time: str
    timezone: str
    start_date: str
    end_date: Optional[str]
    difficulty: int
    reminder_intensity: str
    status: str
    current_streak: int
    longest_streak: int
    total_completions: int
    total_missed: int
    completion_rate: float = 0.0  # Calculated
    created_at: str


class HabitLog(BaseModel):
    """Individual log entry for each habit occurrence"""
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    habit_id: str
    user_id: str
    user_phone: str
    
    # Date and time of this specific habit instance
    scheduled_date: str  # "2026-02-28" format
    scheduled_time: str  # "06:00" format
    
    # Completion tracking
    status: Literal["pending", "reminded", "completed", "missed", "skipped", "snoozed"] = "pending"
    completed_at: Optional[datetime] = None
    
    # Reminder tracking
    reminder_sent: bool = False
    reminder_sent_at: Optional[datetime] = None
    followup_count: int = 0
    last_followup_at: Optional[datetime] = None
    
    # User actions
    snoozed: bool = False
    snooze_until: Optional[datetime] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    
    # Completion details
    completion_note: Optional[str] = None
    
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)


class HabitLogResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str
    habit_id: str
    scheduled_date: str
    scheduled_time: str
    status: str
    completed_at: Optional[str]
    reminder_sent: bool
    followup_count: int
    snoozed: bool
    skipped: bool


class HabitModification(BaseModel):
    """Track all changes to habits for history"""
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    habit_id: str
    user_id: str
    
    field_changed: str  # "time", "frequency", "status", etc.
    previous_value: str
    new_value: str
    
    modified_at: datetime = Field(default_factory=get_utc_now)


class HabitStats(BaseModel):
    """Weekly/Monthly statistics for a user's habits"""
    model_config = ConfigDict(extra="ignore")
    
    user_id: str
    period_start: str  # "2026-02-21"
    period_end: str    # "2026-02-28"
    period_type: Literal["weekly", "monthly"] = "weekly"
    
    total_habits: int = 0
    active_habits: int = 0
    total_scheduled: int = 0
    total_completed: int = 0
    total_missed: int = 0
    total_skipped: int = 0
    
    completion_rate: float = 0.0  # percentage
    
    # Category breakdown
    category_stats: dict = {}  # {"Health": {"completed": 5, "missed": 2}, ...}
    
    # Best and worst
    best_habit_id: Optional[str] = None
    best_habit_name: Optional[str] = None
    best_habit_rate: float = 0.0
    
    worst_habit_id: Optional[str] = None
    worst_habit_name: Optional[str] = None
    worst_habit_rate: float = 0.0
    
    # Insights
    insights: List[str] = []
    suggestions: List[str] = []
    
    generated_at: datetime = Field(default_factory=get_utc_now)


class PendingHabitCreation(BaseModel):
    """Store pending habit creation for confirmation flow"""
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=generate_uuid)
    user_phone: str
    
    # Habit details being confirmed
    name: str
    category: str = "Custom"
    frequency: str = "daily"
    custom_days: Optional[List[str]] = None
    time: str = "09:00"
    timezone: str = "UTC"
    start_date: Optional[str] = None
    difficulty: int = 3
    reminder_intensity: str = "standard"
    
    # Confirmation status
    status: Literal["awaiting_confirmation", "confirmed", "cancelled"] = "awaiting_confirmation"
    
    created_at: datetime = Field(default_factory=get_utc_now)
    expires_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))  # Auto-expire
