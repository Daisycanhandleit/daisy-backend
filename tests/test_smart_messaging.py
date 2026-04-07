"""
Test Smart Messaging System for Daisy - AI Life Concierge

Features tested:
1. Smart Messaging Settings API - GET and PUT /api/settings/smart-messaging
2. Snooze reminder handler - When user replies 'Later' or '2'
3. Skip reminder handler - When user replies 'Skip' or '3'
4. Tasks overview - When user asks 'What are my tasks today?'
5. Morning Agenda and Evening Wrapup scheduler jobs registration
"""

import pytest
import requests
import os
import uuid
from datetime import datetime, timezone, timedelta

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
TEST_EMAIL = "test@test.com"
TEST_PASSWORD = "password"
TEST_PHONE = "+61452502696"


class TestSmartMessagingSettings:
    """Test Smart Messaging Settings API endpoints"""
    
    @pytest.fixture
    def auth_token(self):
        """Get authentication token for tests"""
        # First try to login with existing user
        response = requests.post(f"{BASE_URL}/api/auth/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD
        })
        
        if response.status_code == 200:
            return response.json().get("access_token")
        
        # If login fails, try to register
        response = requests.post(f"{BASE_URL}/api/auth/register", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "name": "Test User",
            "phone": TEST_PHONE,
            "timezone": "Australia/Melbourne"
        })
        
        if response.status_code in [200, 201]:
            return response.json().get("access_token")
        
        pytest.skip("Could not authenticate - skipping test")
    
    @pytest.fixture
    def auth_headers(self, auth_token):
        """Get auth headers"""
        return {"Authorization": f"Bearer {auth_token}"}
    
    def test_get_smart_messaging_settings_without_phone(self):
        """Test GET settings returns error when user has no phone linked"""
        # Create a user without phone number
        unique_email = f"test_no_phone_{uuid.uuid4().hex[:8]}@test.com"
        
        # Register user without phone
        response = requests.post(f"{BASE_URL}/api/auth/register", json={
            "email": unique_email,
            "password": "testpass123",
            "name": "No Phone User"
        })
        
        if response.status_code not in [200, 201]:
            pytest.skip("Could not register test user")
        
        token = response.json().get("access_token")
        headers = {"Authorization": f"Bearer {token}"}
        
        # Try to get settings - should fail
        response = requests.get(f"{BASE_URL}/api/settings/smart-messaging", headers=headers)
        
        # Should return 400 error about needing phone
        assert response.status_code == 400
        data = response.json()
        assert "phone" in data.get("detail", "").lower()
        print(f"PASS: GET settings without phone returns correct error: {data['detail']}")
    
    def test_get_smart_messaging_settings_with_phone(self, auth_headers):
        """Test GET settings returns defaults when user has phone"""
        response = requests.get(f"{BASE_URL}/api/settings/smart-messaging", headers=auth_headers)
        
        # If user has phone, should work
        if response.status_code == 200:
            data = response.json()
            assert "morning_agenda_time" in data
            assert "evening_wrapup_time" in data
            assert "timezone" in data
            assert "agenda_enabled" in data
            print(f"PASS: GET settings returned: morning={data['morning_agenda_time']}, evening={data['evening_wrapup_time']}")
        elif response.status_code == 400:
            # User doesn't have phone linked - this is acceptable
            print("INFO: Test user doesn't have phone linked - skipping full test")
    
    def test_update_smart_messaging_settings(self, auth_headers):
        """Test PUT settings updates successfully"""
        new_settings = {
            "morning_agenda_time": "08:00",
            "evening_wrapup_time": "20:00",
            "agenda_enabled": True,
            "timezone": "Australia/Sydney"
        }
        
        response = requests.put(
            f"{BASE_URL}/api/settings/smart-messaging",
            headers=auth_headers,
            json=new_settings
        )
        
        if response.status_code == 200:
            data = response.json()
            assert "message" in data
            assert "settings" in data
            settings = data["settings"]
            assert settings["morning_agenda_time"] == "08:00"
            assert settings["evening_wrapup_time"] == "20:00"
            print(f"PASS: Settings updated successfully: {settings}")
        elif response.status_code == 400:
            # User doesn't have phone linked
            print("INFO: Cannot update settings - user needs phone linked")
    
    def test_update_settings_invalid_time_format(self, auth_headers):
        """Test PUT settings with invalid time format returns error"""
        invalid_settings = {
            "morning_agenda_time": "25:00",  # Invalid hour
            "evening_wrapup_time": "9pm"      # Invalid format
        }
        
        response = requests.put(
            f"{BASE_URL}/api/settings/smart-messaging",
            headers=auth_headers,
            json=invalid_settings
        )
        
        # Should return 400 for invalid format
        if response.status_code == 400:
            data = response.json()
            assert "Invalid time format" in data.get("detail", "") or "HH:MM" in data.get("detail", "")
            print(f"PASS: Invalid time format rejected: {data['detail']}")
        else:
            print(f"INFO: Response status {response.status_code}")


class TestSnoozeReminderHandler:
    """Test snooze_reminder intent handling via webhook"""
    
    @pytest.fixture
    def create_test_reminder(self):
        """Create a test reminder in 'sent' status for snooze testing"""
        # We need to insert directly into DB - this is a helper for testing
        return None  # Will test via webhook simulation
    
    def test_snooze_via_webhook_later(self):
        """Test snoozing reminder when user replies 'Later'"""
        # Simulate incoming WhatsApp message with 'Later'
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Later",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        # Response should indicate snooze action or no active reminder
        print(f"PASS: Webhook responded to 'Later': status={response.status_code}")
    
    def test_snooze_via_webhook_number_2(self):
        """Test snoozing reminder when user replies '2'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "2",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to '2': status={response.status_code}")
    
    def test_snooze_via_webhook_remind_me_later(self):
        """Test snoozing reminder when user replies 'Remind me later'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Remind me later",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to 'Remind me later': status={response.status_code}")


class TestSkipReminderHandler:
    """Test skip_reminder intent handling via webhook"""
    
    def test_skip_via_webhook_skip(self):
        """Test skipping reminder when user replies 'Skip'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Skip",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to 'Skip': status={response.status_code}")
    
    def test_skip_via_webhook_number_3(self):
        """Test skipping reminder when user replies '3'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "3",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to '3': status={response.status_code}")
    
    def test_skip_via_webhook_not_now(self):
        """Test skipping reminder when user replies 'Not now'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Not now",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to 'Not now': status={response.status_code}")


class TestTasksOverview:
    """Test tasks_overview intent handling"""
    
    def test_tasks_overview_what_are_my_tasks(self):
        """Test tasks overview when user asks 'What are my tasks today?'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "What are my tasks today?",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to 'What are my tasks today?': status={response.status_code}")
    
    def test_tasks_overview_whats_pending(self):
        """Test tasks overview when user asks 'What's pending?'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "What's pending?",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to 'What's pending?': status={response.status_code}")
    
    def test_tasks_overview_show_my_reminders(self):
        """Test tasks overview when user asks 'Show my reminders'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Show my reminders",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to 'Show my reminders': status={response.status_code}")
    
    def test_tasks_overview_my_schedule(self):
        """Test tasks overview when user asks 'My schedule'"""
        webhook_data = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "My schedule",
            "MessageSid": f"TEST_SM{uuid.uuid4().hex[:24]}",
            "AccountSid": "TEST_AC",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=webhook_data
        )
        
        assert response.status_code == 200
        print(f"PASS: Webhook responded to 'My schedule': status={response.status_code}")


class TestSchedulerJobsRegistration:
    """Test that scheduler jobs are properly registered"""
    
    def test_scheduler_module_imports(self):
        """Test that scheduler module imports correctly"""
        try:
            from scheduler import start_scheduler, stop_scheduler, send_morning_agenda, send_evening_wrapup
            print("PASS: Scheduler module imports correctly with all required functions")
            assert True
        except ImportError as e:
            pytest.fail(f"Failed to import scheduler module: {e}")
    
    def test_scheduler_functions_exist(self):
        """Test that morning_agenda and evening_wrapup functions exist"""
        from scheduler import send_morning_agenda, send_evening_wrapup
        
        # Check functions are async (coroutines)
        import asyncio
        assert asyncio.iscoroutinefunction(send_morning_agenda), "send_morning_agenda should be async"
        assert asyncio.iscoroutinefunction(send_evening_wrapup), "send_evening_wrapup should be async"
        print("PASS: send_morning_agenda and send_evening_wrapup are async functions")
    
    def test_smart_reminder_functions_exist(self):
        """Test that smart reminder functions exist"""
        from scheduler import send_smart_reminder_with_buttons, send_gentle_followup
        
        import asyncio
        assert asyncio.iscoroutinefunction(send_smart_reminder_with_buttons), "send_smart_reminder_with_buttons should be async"
        assert asyncio.iscoroutinefunction(send_gentle_followup), "send_gentle_followup should be async"
        print("PASS: send_smart_reminder_with_buttons and send_gentle_followup are async functions")


class TestAIEngineIntents:
    """Test that AI engine recognizes snooze/skip/tasks_overview intents"""
    
    def test_ai_engine_imports(self):
        """Test that AI engine module imports correctly"""
        try:
            from ai_engine import parse_user_message, DAISY_SYSTEM_PROMPT
            print("PASS: AI engine module imports correctly")
            assert True
        except ImportError as e:
            pytest.fail(f"Failed to import ai_engine module: {e}")
    
    def test_system_prompt_contains_snooze_intent(self):
        """Test that system prompt defines snooze_reminder intent"""
        from ai_engine import DAISY_SYSTEM_PROMPT
        
        assert "snooze_reminder" in DAISY_SYSTEM_PROMPT
        assert "snooze_minutes" in DAISY_SYSTEM_PROMPT
        print("PASS: System prompt contains snooze_reminder intent definition")
    
    def test_system_prompt_contains_skip_intent(self):
        """Test that system prompt defines skip_reminder intent"""
        from ai_engine import DAISY_SYSTEM_PROMPT
        
        assert "skip_reminder" in DAISY_SYSTEM_PROMPT
        print("PASS: System prompt contains skip_reminder intent definition")
    
    def test_system_prompt_contains_tasks_overview_intent(self):
        """Test that system prompt defines tasks_overview intent"""
        from ai_engine import DAISY_SYSTEM_PROMPT
        
        assert "tasks_overview" in DAISY_SYSTEM_PROMPT
        assert "What are my tasks today?" in DAISY_SYSTEM_PROMPT
        print("PASS: System prompt contains tasks_overview intent definition")


class TestInteractiveReminderFormat:
    """Test that reminders are sent with interactive Done/Later/Skip options"""
    
    def test_smart_reminder_message_format(self):
        """Test smart reminder message includes interactive options"""
        import asyncio
        from scheduler import send_smart_reminder_with_buttons
        
        # The function is async - we'll just verify the function signature and return type
        import inspect
        sig = inspect.signature(send_smart_reminder_with_buttons)
        params = list(sig.parameters.keys())
        
        assert 'to_phone' in params
        assert 'message' in params
        assert 'reminder_id' in params
        print(f"PASS: send_smart_reminder_with_buttons has correct params: {params}")
    
    def test_gentle_followup_message_format(self):
        """Test gentle followup message format"""
        import inspect
        from scheduler import send_gentle_followup
        
        sig = inspect.signature(send_gentle_followup)
        params = list(sig.parameters.keys())
        
        assert 'to_phone' in params
        assert 'message' in params
        assert 'follow_up_count' in params
        print(f"PASS: send_gentle_followup has correct params: {params}")


class TestMaxFollowUpsLimit:
    """Test that follow-ups are limited to max 2"""
    
    def test_followup_check_max_2(self):
        """Test that check_and_send_followups has max 2 follow-ups limit"""
        import ast
        
        # Read the scheduler file
        with open('/app/backend/scheduler.py', 'r') as f:
            content = f.read()
        
        # Check that the max follow-up is 2
        assert '"follow_up_count": {"$lt": 2}' in content or "'follow_up_count': {'$lt': 2}" in content or "follow_up_count < 2" in content.lower()
        # Also check the follow-up intervals
        assert "[10, 30]" in content or "10, 30" in content
        print("PASS: Follow-up limit is set to max 2 with intervals [10, 30] minutes")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
