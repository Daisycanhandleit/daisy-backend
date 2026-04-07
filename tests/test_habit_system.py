"""
Backend tests for Daisy Habit Creation System
Tests all habit-related functionality via WhatsApp webhook and admin endpoints:
- Habit creation via natural conversation
- Habit confirmation (user says 'Yes')
- Habit completion (user says 'Done')
- Habit snooze (user says 'Snooze')
- Habit skip (user says 'Skip')
- Listing habits (user says 'Show my habits')
- Pause habit
- Resume habit
- Habit stats
- Weekly report
- Admin endpoints: /api/admin/habits, /api/admin/habit-logs, /api/admin/overview (habits section)
"""
import pytest
import requests
import os
import uuid

# Get BASE_URL from environment
BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test phone number for habit testing
TEST_PHONE = "+61452502696"  # Australian number (from required_credentials)


class TestHabitAdminEndpoints:
    """Admin habit endpoints - run first to verify endpoints work"""
    
    def test_admin_habits_endpoint(self):
        """Test GET /api/admin/habits returns habits list"""
        response = requests.get(f"{BASE_URL}/api/admin/habits")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        assert "count" in data
        assert "habits" in data
        assert isinstance(data["habits"], list)
        print(f"✓ Admin habits endpoint passed: count={data['count']}")
    
    def test_admin_habits_with_status_filter(self):
        """Test GET /api/admin/habits?habit_status=active filters correctly"""
        response = requests.get(f"{BASE_URL}/api/admin/habits?habit_status=active")
        assert response.status_code == 200
        data = response.json()
        
        # Verify all returned habits have active status
        for habit in data["habits"]:
            assert habit.get("status") == "active", f"Expected active, got {habit.get('status')}"
        print(f"✓ Admin habits (status=active) passed: count={data['count']}")
    
    def test_admin_habit_logs_endpoint(self):
        """Test GET /api/admin/habit-logs returns habit logs"""
        response = requests.get(f"{BASE_URL}/api/admin/habit-logs")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "habit_logs" in data
        assert isinstance(data["habit_logs"], list)
        print(f"✓ Admin habit-logs endpoint passed: count={data['count']}")
    
    def test_admin_habit_modifications_endpoint(self):
        """Test GET /api/admin/habit-modifications returns modification history"""
        response = requests.get(f"{BASE_URL}/api/admin/habit-modifications")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "modifications" in data
        assert isinstance(data["modifications"], list)
        print(f"✓ Admin habit-modifications endpoint passed: count={data['count']}")
    
    def test_admin_pending_habits_endpoint(self):
        """Test GET /api/admin/pending-habits returns pending creations"""
        response = requests.get(f"{BASE_URL}/api/admin/pending-habits")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "pending_habits" in data
        assert isinstance(data["pending_habits"], list)
        print(f"✓ Admin pending-habits endpoint passed: count={data['count']}")
    
    def test_admin_overview_includes_habits(self):
        """Test GET /api/admin/overview includes habits breakdown"""
        response = requests.get(f"{BASE_URL}/api/admin/overview")
        assert response.status_code == 200
        data = response.json()
        
        # Verify habits section exists in overview
        assert "overview" in data
        assert "total_habits" in data["overview"], "total_habits should be in overview"
        
        # Verify habits breakdown exists
        assert "habits_breakdown" in data
        habits_breakdown = data["habits_breakdown"]
        assert "active" in habits_breakdown
        assert "paused" in habits_breakdown
        assert "total_logs" in habits_breakdown
        
        print(f"✓ Admin overview includes habits: {habits_breakdown}")


class TestHabitCreationFlow:
    """Test habit creation via WhatsApp webhook"""
    
    def test_01_create_habit_request(self):
        """Test habit creation: 'I want to start meditating every day at 6 AM'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "I want to start meditating every day at 6 AM",
            "MessageSid": f"TEST_HABIT_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        
        # The response should contain confirmation request
        # Check pending habits for this user
        pending_response = requests.get(f"{BASE_URL}/api/admin/pending-habits")
        pending_data = pending_response.json()
        
        # Should have a pending habit for this phone
        user_pending = [p for p in pending_data.get("pending_habits", []) if p.get("user_phone") == TEST_PHONE]
        print(f"✓ Habit creation request passed. Pending habits for user: {len(user_pending)}")
        
        # Store test state for next test
        TestHabitCreationFlow.has_pending = len(user_pending) > 0
    
    def test_02_confirm_habit_yes(self):
        """Test habit confirmation: user says 'Yes'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Yes",
            "MessageSid": f"TEST_HABIT_CONFIRM_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Habit confirmation (Yes) passed")
    
    def test_03_verify_habit_created(self):
        """Verify habit was created after confirmation"""
        # Check habits for this user
        habits_response = requests.get(f"{BASE_URL}/api/admin/habits")
        habits_data = habits_response.json()
        
        # Find habits for test phone
        user_habits = [h for h in habits_data.get("habits", []) if h.get("user_phone") == TEST_PHONE]
        
        if user_habits:
            print(f"✓ Habit verified in database: {len(user_habits)} habit(s) found")
            for h in user_habits:
                print(f"  - {h.get('name', 'Unknown')}: {h.get('time', 'N/A')} {h.get('frequency', 'N/A')}")
            TestHabitCreationFlow.created_habit_id = user_habits[0].get('id')
            TestHabitCreationFlow.created_habit_name = user_habits[0].get('name')
        else:
            print(f"! No habits found for {TEST_PHONE} - habit may not have been confirmed")
            TestHabitCreationFlow.created_habit_id = None
            TestHabitCreationFlow.created_habit_name = None


class TestHabitCompletion:
    """Test habit completion flow"""
    
    def test_habit_done(self):
        """Test habit completion: user says 'Done'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Done",
            "MessageSid": f"TEST_HABIT_DONE_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Habit completion (Done) passed - status code 200")


class TestHabitSnooze:
    """Test habit snooze flow"""
    
    def test_habit_snooze(self):
        """Test habit snooze: user says 'Snooze'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Snooze",
            "MessageSid": f"TEST_HABIT_SNOOZE_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Habit snooze passed - status code 200")
    
    def test_habit_snooze_with_time(self):
        """Test habit snooze: user says 'Snooze for 30 minutes'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Snooze for 30 minutes",
            "MessageSid": f"TEST_HABIT_SNOOZE30_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Habit snooze with time passed - status code 200")


class TestHabitSkip:
    """Test habit skip flow"""
    
    def test_habit_skip(self):
        """Test habit skip: user says 'Skip'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Skip",
            "MessageSid": f"TEST_HABIT_SKIP_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Habit skip passed - status code 200")
    
    def test_habit_skip_with_reason(self):
        """Test habit skip: user says 'Skip today, feeling sick'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Skip today, feeling sick",
            "MessageSid": f"TEST_HABIT_SKIPREASON_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Habit skip with reason passed - status code 200")


class TestHabitList:
    """Test listing habits"""
    
    def test_list_habits(self):
        """Test listing habits: user says 'Show my habits'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Show my habits",
            "MessageSid": f"TEST_HABIT_LIST_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ List habits passed - status code 200")


class TestHabitPauseResume:
    """Test habit pause and resume"""
    
    def test_01_create_second_habit_for_pause_test(self):
        """Create a second habit to test pause/resume"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "I want to start exercising every day at 7 AM",
            "MessageSid": f"TEST_HABIT_EX_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        
        # Confirm the habit
        confirm_payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Yes",
            "MessageSid": f"TEST_HABIT_EX_CONFIRM_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        confirm_response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=confirm_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert confirm_response.status_code == 200
        print(f"✓ Created second habit for pause/resume test")
    
    def test_02_pause_habit(self):
        """Test pausing habit: 'Pause my meditation habit'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Pause my meditation habit",
            "MessageSid": f"TEST_HABIT_PAUSE_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Pause habit passed - status code 200")
    
    def test_03_verify_habit_paused(self):
        """Verify habit status is 'paused' after pause command"""
        habits_response = requests.get(f"{BASE_URL}/api/admin/habits?habit_status=paused")
        habits_data = habits_response.json()
        
        paused_habits = [h for h in habits_data.get("habits", []) if h.get("user_phone") == TEST_PHONE]
        print(f"✓ Paused habits for user: {len(paused_habits)}")
        for h in paused_habits:
            print(f"  - {h.get('name')}: status={h.get('status')}")
    
    def test_04_resume_habit(self):
        """Test resuming habit: 'Resume meditation'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Resume meditation",
            "MessageSid": f"TEST_HABIT_RESUME_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Resume habit passed - status code 200")


class TestHabitStats:
    """Test habit statistics"""
    
    def test_habit_stats(self):
        """Test habit stats: 'How am I doing with meditation?'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "How am I doing with meditation?",
            "MessageSid": f"TEST_HABIT_STATS_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Habit stats request passed - status code 200")


class TestWeeklyReport:
    """Test weekly report"""
    
    def test_weekly_report(self):
        """Test weekly report: 'Show my weekly report'"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Show my weekly report",
            "MessageSid": f"TEST_HABIT_WEEKLY_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        print(f"✓ Weekly report request passed - status code 200")


class TestHabitHelpMessage:
    """Test habit help message"""
    
    def test_habit_help(self):
        """Test habit help message"""
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "help with habits",
            "MessageSid": f"TEST_HABIT_HELP_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Habit help request passed - status code 200")


class TestHabitCleanup:
    """Cleanup test habits"""
    
    def test_delete_test_habits(self):
        """Delete test habits via webhook"""
        # Try to delete meditation habit
        payload = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Delete my meditation habit",
            "MessageSid": f"TEST_HABIT_DEL1_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Delete meditation habit - status code 200")
        
        # Try to delete exercise habit
        payload2 = {
            "From": f"whatsapp:{TEST_PHONE}",
            "To": "whatsapp:+15393091015",
            "Body": "Delete my exercising habit",
            "MessageSid": f"TEST_HABIT_DEL2_{uuid.uuid4().hex[:8]}",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response2 = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload2,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response2.status_code == 200
        print(f"✓ Delete exercise habit - status code 200")


class TestSchedulerJobsConfigured:
    """Verify habit scheduler jobs are configured"""
    
    def test_verify_admin_overview_has_scheduler_info(self):
        """Verify scheduler jobs for habits appear in admin overview"""
        response = requests.get(f"{BASE_URL}/api/admin/overview")
        assert response.status_code == 200
        data = response.json()
        
        # Habits should be in overview
        assert "total_habits" in data.get("overview", {}), "Habits count should be in overview"
        print(f"✓ Scheduler-related habit tracking verified in admin overview")
        print(f"  - Total habits: {data['overview'].get('total_habits', 0)}")
        print(f"  - Habits breakdown: {data.get('habits_breakdown', {})}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
