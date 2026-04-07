"""
Test Suite for Admin Dashboard APIs
Tests: /api/admin/overview, /api/admin/system-health, /api/admin/subscriptions,
       /api/admin/activity-log, /api/admin/analytics, /api/admin/users,
       /api/admin/whatsapp-users, /api/admin/users/{user_id}
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

class TestAdminOverview:
    """Test GET /api/admin/overview - System overview with all stats"""
    
    def test_overview_returns_200(self):
        """Verify overview endpoint returns success"""
        response = requests.get(f"{BASE_URL}/api/admin/overview")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print(f"✓ GET /api/admin/overview returned 200")
    
    def test_overview_structure(self):
        """Verify overview response has correct structure"""
        response = requests.get(f"{BASE_URL}/api/admin/overview")
        assert response.status_code == 200
        data = response.json()
        
        # Check top-level keys
        assert "overview" in data, "Missing 'overview' key"
        assert "reminders_breakdown" in data, "Missing 'reminders_breakdown' key"
        assert "contacts_breakdown" in data, "Missing 'contacts_breakdown' key"
        assert "habits_breakdown" in data, "Missing 'habits_breakdown' key"
        assert "teams_breakdown" in data, "Missing 'teams_breakdown' key"
        assert "database" in data, "Missing 'database' key"
        
        # Check overview structure
        overview = data["overview"]
        assert "total_registered_users" in overview, "Missing 'total_registered_users'"
        assert "total_whatsapp_users" in overview, "Missing 'total_whatsapp_users'"
        assert "total_contacts" in overview, "Missing 'total_contacts'"
        assert "total_reminders" in overview, "Missing 'total_reminders'"
        assert "total_messages" in overview, "Missing 'total_messages'"
        assert "total_teams" in overview, "Missing 'total_teams'"
        assert "total_habits" in overview, "Missing 'total_habits'"
        
        # Check reminders_breakdown structure
        reminders_breakdown = data["reminders_breakdown"]
        assert "pending" in reminders_breakdown, "Missing 'pending' in reminders_breakdown"
        assert "sent" in reminders_breakdown, "Missing 'sent' in reminders_breakdown"
        assert "acknowledged" in reminders_breakdown, "Missing 'acknowledged'"
        assert "awaiting_consent" in reminders_breakdown, "Missing 'awaiting_consent'"
        
        # Check habits_breakdown structure
        habits_breakdown = data["habits_breakdown"]
        assert "active" in habits_breakdown, "Missing 'active' in habits_breakdown"
        assert "paused" in habits_breakdown, "Missing 'paused' in habits_breakdown"
        assert "total_logs" in habits_breakdown, "Missing 'total_logs'"
        assert "completed_logs" in habits_breakdown, "Missing 'completed_logs'"
        assert "missed_logs" in habits_breakdown, "Missing 'missed_logs'"
        
        print(f"✓ Overview structure is correct with all required keys")
        print(f"  - Total users: {overview['total_registered_users']}")
        print(f"  - Total WhatsApp users: {overview['total_whatsapp_users']}")
        print(f"  - Active habits: {habits_breakdown['active']}")


class TestAdminSystemHealth:
    """Test GET /api/admin/system-health - Database, Twilio, OpenAI, Scheduler status"""
    
    def test_system_health_returns_200(self):
        """Verify system-health endpoint returns success"""
        response = requests.get(f"{BASE_URL}/api/admin/system-health")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print(f"✓ GET /api/admin/system-health returned 200")
    
    def test_system_health_structure(self):
        """Verify system-health response has correct structure"""
        response = requests.get(f"{BASE_URL}/api/admin/system-health")
        assert response.status_code == 200
        data = response.json()
        
        # Check top-level keys
        assert "status" in data, "Missing 'status' key"
        assert "timestamp" in data, "Missing 'timestamp' key"
        assert "database" in data, "Missing 'database' key"
        assert "integrations" in data, "Missing 'integrations' key"
        assert "scheduler" in data, "Missing 'scheduler' key"
        assert "environment" in data, "Missing 'environment' key"
        
        # Check database structure
        db = data["database"]
        assert "status" in db, "Missing 'status' in database"
        assert "name" in db, "Missing 'name' in database"
        
        # Check integrations structure
        integrations = data["integrations"]
        assert "twilio" in integrations, "Missing 'twilio' in integrations"
        assert "openai" in integrations, "Missing 'openai' in integrations"
        assert "configured" in integrations["twilio"], "Missing 'configured' in twilio"
        assert "configured" in integrations["openai"], "Missing 'configured' in openai"
        
        # Check scheduler structure
        scheduler = data["scheduler"]
        assert "running" in scheduler, "Missing 'running' in scheduler"
        
        print(f"✓ System health structure is correct")
        print(f"  - Status: {data['status']}")
        print(f"  - Database: {db['status']}")
        print(f"  - Twilio configured: {integrations['twilio']['configured']}")
        print(f"  - OpenAI configured: {integrations['openai']['configured']}")
        print(f"  - Scheduler running: {scheduler['running']}")
    
    def test_database_healthy(self):
        """Verify database is healthy"""
        response = requests.get(f"{BASE_URL}/api/admin/system-health")
        assert response.status_code == 200
        data = response.json()
        
        assert data["database"]["status"] == "healthy", f"Database not healthy: {data['database']['status']}"
        print(f"✓ Database is healthy")


class TestAdminSubscriptions:
    """Test GET /api/admin/subscriptions - Subscription breakdown (trial, active, expired)"""
    
    def test_subscriptions_returns_200(self):
        """Verify subscriptions endpoint returns success"""
        response = requests.get(f"{BASE_URL}/api/admin/subscriptions")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print(f"✓ GET /api/admin/subscriptions returned 200")
    
    def test_subscriptions_structure(self):
        """Verify subscriptions response has correct structure"""
        response = requests.get(f"{BASE_URL}/api/admin/subscriptions")
        assert response.status_code == 200
        data = response.json()
        
        # Check required keys
        assert "summary" in data, "Missing 'summary' key"
        assert "trial_users" in data, "Missing 'trial_users' key"
        assert "active_users" in data, "Missing 'active_users' key"
        assert "expired_users" in data, "Missing 'expired_users' key"
        assert "cancelled_users" in data, "Missing 'cancelled_users' key"
        
        # Check summary structure
        summary = data["summary"]
        assert "total_users" in summary, "Missing 'total_users' in summary"
        assert "trial" in summary, "Missing 'trial' in summary"
        assert "active" in summary, "Missing 'active' in summary"
        assert "expired" in summary, "Missing 'expired' in summary"
        assert "cancelled" in summary, "Missing 'cancelled' in summary"
        
        # Verify lists are actually lists
        assert isinstance(data["trial_users"], list), "trial_users should be a list"
        assert isinstance(data["active_users"], list), "active_users should be a list"
        assert isinstance(data["expired_users"], list), "expired_users should be a list"
        assert isinstance(data["cancelled_users"], list), "cancelled_users should be a list"
        
        print(f"✓ Subscriptions structure is correct")
        print(f"  - Total users: {summary['total_users']}")
        print(f"  - Trial: {summary['trial']}")
        print(f"  - Active: {summary['active']}")
        print(f"  - Expired: {summary['expired']}")


class TestAdminActivityLog:
    """Test GET /api/admin/activity-log - Recent system activities"""
    
    def test_activity_log_returns_200(self):
        """Verify activity-log endpoint returns success"""
        response = requests.get(f"{BASE_URL}/api/admin/activity-log")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print(f"✓ GET /api/admin/activity-log returned 200")
    
    def test_activity_log_with_limit(self):
        """Verify activity-log respects limit parameter"""
        response = requests.get(f"{BASE_URL}/api/admin/activity-log", params={"limit": 10})
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data, "Missing 'count' key"
        assert "activities" in data, "Missing 'activities' key"
        assert isinstance(data["activities"], list), "activities should be a list"
        assert len(data["activities"]) <= 10, "activities should respect limit"
        
        print(f"✓ Activity log returns correct structure with {data['count']} activities")
    
    def test_activity_log_structure(self):
        """Verify activity log entries have correct structure"""
        response = requests.get(f"{BASE_URL}/api/admin/activity-log", params={"limit": 30})
        assert response.status_code == 200
        data = response.json()
        
        if len(data["activities"]) > 0:
            activity = data["activities"][0]
            assert "type" in activity, "Missing 'type' in activity"
            assert "timestamp" in activity, "Missing 'timestamp' in activity"
            assert activity["type"] in ["message", "reminder", "habit"], f"Unknown activity type: {activity['type']}"
            print(f"✓ Activity log entries have correct structure (type: {activity['type']})")
        else:
            print(f"✓ Activity log endpoint working (no activities yet)")


class TestAdminAnalytics:
    """Test GET /api/admin/analytics - 30-day analytics data"""
    
    def test_analytics_returns_200(self):
        """Verify analytics endpoint returns success"""
        response = requests.get(f"{BASE_URL}/api/admin/analytics")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print(f"✓ GET /api/admin/analytics returned 200")
    
    def test_analytics_structure(self):
        """Verify analytics response has correct structure"""
        response = requests.get(f"{BASE_URL}/api/admin/analytics")
        assert response.status_code == 200
        data = response.json()
        
        # Check required keys
        assert "period" in data, "Missing 'period' key"
        assert "messages_by_day" in data, "Missing 'messages_by_day' key"
        assert "reminders_by_day" in data, "Missing 'reminders_by_day' key"
        assert "signups_by_day" in data, "Missing 'signups_by_day' key"
        assert "habits_by_day" in data, "Missing 'habits_by_day' key"
        assert "totals" in data, "Missing 'totals' key"
        
        # Check period structure
        period = data["period"]
        assert "start" in period, "Missing 'start' in period"
        assert "end" in period, "Missing 'end' in period"
        
        # Check totals structure
        totals = data["totals"]
        assert "messages" in totals, "Missing 'messages' in totals"
        assert "reminders" in totals, "Missing 'reminders' in totals"
        assert "signups" in totals, "Missing 'signups' in totals"
        assert "habit_logs" in totals, "Missing 'habit_logs' in totals"
        
        print(f"✓ Analytics structure is correct")
        print(f"  - Period: {period['start'][:10]} to {period['end'][:10]}")
        print(f"  - Totals - Messages: {totals['messages']}, Reminders: {totals['reminders']}, Signups: {totals['signups']}")


class TestAdminUsers:
    """Test GET /api/admin/users - List all registered users"""
    
    def test_users_returns_200(self):
        """Verify users endpoint returns success"""
        response = requests.get(f"{BASE_URL}/api/admin/users")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print(f"✓ GET /api/admin/users returned 200")
    
    def test_users_structure(self):
        """Verify users response has correct structure"""
        response = requests.get(f"{BASE_URL}/api/admin/users")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data, "Missing 'count' key"
        assert "users" in data, "Missing 'users' key"
        assert isinstance(data["users"], list), "users should be a list"
        
        print(f"✓ Users endpoint returned {data['count']} users")
        
        if len(data["users"]) > 0:
            user = data["users"][0]
            # Verify user doesn't contain password_hash
            assert "password_hash" not in user, "password_hash should not be returned"
            # Verify basic user fields
            assert "id" in user, "Missing 'id' in user"
            assert "email" in user, "Missing 'email' in user"
            assert "name" in user, "Missing 'name' in user"
            print(f"✓ User structure is correct (no password_hash)")


class TestAdminWhatsAppUsers:
    """Test GET /api/admin/whatsapp-users - List WhatsApp-only users"""
    
    def test_whatsapp_users_returns_200(self):
        """Verify whatsapp-users endpoint returns success"""
        response = requests.get(f"{BASE_URL}/api/admin/whatsapp-users")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        print(f"✓ GET /api/admin/whatsapp-users returned 200")
    
    def test_whatsapp_users_structure(self):
        """Verify whatsapp-users response has correct structure"""
        response = requests.get(f"{BASE_URL}/api/admin/whatsapp-users")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data, "Missing 'count' key"
        assert "whatsapp_users" in data, "Missing 'whatsapp_users' key"
        assert isinstance(data["whatsapp_users"], list), "whatsapp_users should be a list"
        
        print(f"✓ WhatsApp users endpoint returned {data['count']} users")


class TestAdminUserDetails:
    """Test GET /api/admin/users/{user_id} - Get detailed user info"""
    
    def test_user_details_for_nonexistent_user(self):
        """Verify 404 for non-existent user"""
        response = requests.get(f"{BASE_URL}/api/admin/users/nonexistent_user_id_12345")
        assert response.status_code == 404, f"Expected 404, got {response.status_code}"
        print(f"✓ GET /api/admin/users/nonexistent returns 404")
    
    def test_user_details_for_existing_user(self):
        """Verify user details for an existing user"""
        # First get list of users
        users_response = requests.get(f"{BASE_URL}/api/admin/users")
        assert users_response.status_code == 200
        users_data = users_response.json()
        
        if users_data["count"] > 0:
            user_id = users_data["users"][0]["id"]
            response = requests.get(f"{BASE_URL}/api/admin/users/{user_id}")
            assert response.status_code == 200, f"Expected 200, got {response.status_code}"
            
            data = response.json()
            assert "user" in data, "Missing 'user' key"
            assert "stats" in data, "Missing 'stats' key"
            assert "reminders" in data, "Missing 'reminders' key"
            assert "habits" in data, "Missing 'habits' key"
            assert "contacts" in data, "Missing 'contacts' key"
            
            # Verify stats structure
            stats = data["stats"]
            assert "total_reminders" in stats, "Missing 'total_reminders' in stats"
            assert "total_habits" in stats, "Missing 'total_habits' in stats"
            assert "total_contacts" in stats, "Missing 'total_contacts' in stats"
            assert "total_teams" in stats, "Missing 'total_teams' in stats"
            
            print(f"✓ User details returned for user: {data['user'].get('email')}")
            print(f"  - Reminders: {stats['total_reminders']}, Habits: {stats['total_habits']}")
        else:
            pytest.skip("No users available for testing user details")


class TestAdminAdditionalEndpoints:
    """Test additional admin endpoints"""
    
    def test_admin_contacts(self):
        """Test GET /api/admin/contacts"""
        response = requests.get(f"{BASE_URL}/api/admin/contacts")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "contacts" in data
        print(f"✓ Admin contacts returned {data['count']} contacts")
    
    def test_admin_reminders(self):
        """Test GET /api/admin/reminders"""
        response = requests.get(f"{BASE_URL}/api/admin/reminders")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "reminders" in data
        print(f"✓ Admin reminders returned {data['count']} reminders")
    
    def test_admin_reminders_with_status_filter(self):
        """Test GET /api/admin/reminders with status filter"""
        response = requests.get(f"{BASE_URL}/api/admin/reminders", params={"reminder_status": "pending"})
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "reminders" in data
        # All returned reminders should have pending status (if any returned)
        for rem in data["reminders"]:
            assert rem.get("status") == "pending", f"Expected pending, got {rem.get('status')}"
        print(f"✓ Admin reminders with status filter working")
    
    def test_admin_messages(self):
        """Test GET /api/admin/messages"""
        response = requests.get(f"{BASE_URL}/api/admin/messages")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "messages" in data
        print(f"✓ Admin messages returned {data['count']} messages")
    
    def test_admin_teams(self):
        """Test GET /api/admin/teams"""
        response = requests.get(f"{BASE_URL}/api/admin/teams")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "teams" in data
        print(f"✓ Admin teams returned {data['count']} teams")
    
    def test_admin_habits(self):
        """Test GET /api/admin/habits"""
        response = requests.get(f"{BASE_URL}/api/admin/habits")
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "habits" in data
        print(f"✓ Admin habits returned {data['count']} habits")
    
    def test_admin_habits_with_status_filter(self):
        """Test GET /api/admin/habits with status filter"""
        response = requests.get(f"{BASE_URL}/api/admin/habits", params={"habit_status": "active"})
        assert response.status_code == 200
        data = response.json()
        assert "count" in data
        assert "habits" in data
        # All returned habits should have active status (if any returned)
        for habit in data["habits"]:
            assert habit.get("status") == "active", f"Expected active, got {habit.get('status')}"
        print(f"✓ Admin habits with status filter working")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
