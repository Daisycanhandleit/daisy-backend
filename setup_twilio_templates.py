"""
Twilio Content Templates Setup for Daisy
Creates WhatsApp quick reply button templates
Run this script once to create the templates, then save the Content SIDs
"""

import os
import json
from dotenv import load_dotenv
load_dotenv()

from twilio.rest import Client
from twilio.rest.content.v1.content import ContentList

# Get credentials
account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
auth_token = os.environ.get("TWILIO_AUTH_TOKEN")

if not account_sid or not auth_token:
    print("ERROR: TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    exit(1)

client = Client(account_sid, auth_token)

def create_reminder_template():
    """Create a quick reply template for reminder responses"""
    try:
        request = ContentList.ContentCreateRequest({
            "friendly_name": "daisy_reminder_response",
            "language": "en",
            "types": {
                "twilio/quick-reply": {
                    "body": "{{1}}",
                    "actions": [
                        {"title": "Done ✅", "id": "done"},
                        {"title": "Later ⏰", "id": "later"},
                        {"title": "Skip ⏭️", "id": "skip"}
                    ]
                }
            },
            "variables": {"1": "reminder_message"}
        })
        content = client.content.v1.contents.create(content_create_request=request)
        print(f"✅ Created reminder template: {content.sid}")
        return content.sid
    except Exception as e:
        print(f"❌ Error creating reminder template: {e}")
        return None

def create_followup_template():
    """Create a quick reply template for follow-up reminders"""
    try:
        request = ContentList.ContentCreateRequest({
            "friendly_name": "daisy_followup_response",
            "language": "en",
            "types": {
                "twilio/quick-reply": {
                    "body": "{{1}}",
                    "actions": [
                        {"title": "Done ✅", "id": "done"},
                        {"title": "10 more min", "id": "later"},
                        {"title": "Skip", "id": "skip"}
                    ]
                }
            },
            "variables": {"1": "followup_message"}
        })
        content = client.content.v1.contents.create(content_create_request=request)
        print(f"✅ Created follow-up template: {content.sid}")
        return content.sid
    except Exception as e:
        print(f"❌ Error creating follow-up template: {e}")
        return None

def create_consent_template():
    """Create a quick reply template for consent requests"""
    try:
        request = ContentList.ContentCreateRequest({
            "friendly_name": "daisy_consent_request",
            "language": "en",
            "types": {
                "twilio/quick-reply": {
                    "body": "{{1}}",
                    "actions": [
                        {"title": "Yes, I agree 💛", "id": "consent_yes"},
                        {"title": "No thanks", "id": "consent_no"}
                    ]
                }
            },
            "variables": {"1": "consent_message"}
        })
        content = client.content.v1.contents.create(content_create_request=request)
        print(f"✅ Created consent template: {content.sid}")
        return content.sid
    except Exception as e:
        print(f"❌ Error creating consent template: {e}")
        return None

def list_existing_templates():
    """List all existing content templates"""
    print("\n📋 Existing Content Templates:")
    try:
        contents = client.content.v1.contents.list(limit=20)
        for content in contents:
            print(f"  - {content.friendly_name}: {content.sid}")
        return contents
    except Exception as e:
        print(f"❌ Error listing templates: {e}")
        return []

if __name__ == "__main__":
    print("🌼 Daisy - Creating Twilio Content Templates")
    print("=" * 50)
    
    # List existing templates first
    existing = list_existing_templates()
    existing_names = [c.friendly_name for c in existing]
    
    print("\n📝 Creating new templates...")
    
    reminder_sid = None
    followup_sid = None
    consent_sid = None
    
    # Create templates if they don't exist
    if "daisy_reminder_response" not in existing_names:
        reminder_sid = create_reminder_template()
    else:
        reminder_sid = next((c.sid for c in existing if c.friendly_name == "daisy_reminder_response"), None)
        print(f"ℹ️ Reminder template already exists: {reminder_sid}")
    
    if "daisy_followup_response" not in existing_names:
        followup_sid = create_followup_template()
    else:
        followup_sid = next((c.sid for c in existing if c.friendly_name == "daisy_followup_response"), None)
        print(f"ℹ️ Follow-up template already exists: {followup_sid}")
    
    if "daisy_consent_request" not in existing_names:
        consent_sid = create_consent_template()
    else:
        consent_sid = next((c.sid for c in existing if c.friendly_name == "daisy_consent_request"), None)
        print(f"ℹ️ Consent template already exists: {consent_sid}")
    
    print("\n" + "=" * 50)
    print("📌 Add these to your backend/.env file:")
    print("=" * 50)
    if reminder_sid:
        print(f"TWILIO_REMINDER_CONTENT_SID={reminder_sid}")
    if followup_sid:
        print(f"TWILIO_FOLLOWUP_CONTENT_SID={followup_sid}")
    if consent_sid:
        print(f"TWILIO_CONSENT_CONTENT_SID={consent_sid}")
    print("=" * 50)
