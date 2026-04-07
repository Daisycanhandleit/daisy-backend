"""
Backend tests for Daisy Admin Panel and WhatsApp Webhook endpoints
Tests: Admin overview, users, whatsapp-users, contacts, reminders, messages
       WhatsApp webhook with phone number extraction
"""
import pytest
import requests
import os

# Get BASE_URL from environment
BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

class TestHealthCheck:
    """Health check endpoint tests - run first"""
    
    def test_health_endpoint(self):
        """Test /api/health returns healthy status"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "twilio_configured" in data
        print(f"✓ Health check passed: {data}")

    def test_root_endpoint(self):
        """Test /api/ returns API info"""
        response = requests.get(f"{BASE_URL}/api/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "Daisy" in data["message"]
        print(f"✓ Root endpoint passed: {data}")


class TestAdminOverview:
    """Admin overview endpoint tests"""
    
    def test_admin_overview_returns_statistics(self):
        """Test GET /api/admin/overview returns system statistics"""
        response = requests.get(f"{BASE_URL}/api/admin/overview")
        assert response.status_code == 200
        data = response.json()
        
        # Verify overview section exists
        assert "overview" in data
        overview = data["overview"]
        assert "total_registered_users" in overview
        assert "total_whatsapp_users" in overview
        assert "total_contacts" in overview
        assert "total_reminders" in overview
        assert "total_messages" in overview
        
        # Verify reminders breakdown
        assert "reminders_breakdown" in data
        reminders = data["reminders_breakdown"]
        assert "pending" in reminders
        assert "sent" in reminders
        assert "acknowledged" in reminders
        assert "awaiting_consent" in reminders
        
        # Verify contacts breakdown
        assert "contacts_breakdown" in data
        contacts = data["contacts_breakdown"]
        assert "approved" in contacts
        assert "pending" in contacts
        
        # Verify database info
        assert "database" in data
        assert "name" in data["database"]
        assert "collections" in data["database"]
        
        print(f"✓ Admin overview passed: {data}")


class TestAdminUsers:
    """Admin users endpoint tests"""
    
    def test_admin_list_users(self):
        """Test GET /api/admin/users returns list of registered users"""
        response = requests.get(f"{BASE_URL}/api/admin/users")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "users" in data
        assert isinstance(data["users"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["users"])
        
        # Verify no password_hash is exposed
        for user in data["users"]:
            assert "password_hash" not in user
            
        print(f"✓ Admin users passed: count={data['count']}")


class TestAdminWhatsAppUsers:
    """Admin WhatsApp users endpoint tests"""
    
    def test_admin_list_whatsapp_users(self):
        """Test GET /api/admin/whatsapp-users returns WhatsApp-only users"""
        response = requests.get(f"{BASE_URL}/api/admin/whatsapp-users")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "whatsapp_users" in data
        assert isinstance(data["whatsapp_users"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["whatsapp_users"])
        
        print(f"✓ Admin WhatsApp users passed: count={data['count']}")


class TestAdminContacts:
    """Admin contacts endpoint tests"""
    
    def test_admin_list_contacts(self):
        """Test GET /api/admin/contacts returns all contacts"""
        response = requests.get(f"{BASE_URL}/api/admin/contacts")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "contacts" in data
        assert isinstance(data["contacts"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["contacts"])
        
        print(f"✓ Admin contacts passed: count={data['count']}")


class TestAdminReminders:
    """Admin reminders endpoint tests"""
    
    def test_admin_list_reminders_no_filter(self):
        """Test GET /api/admin/reminders returns all reminders"""
        response = requests.get(f"{BASE_URL}/api/admin/reminders")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "reminders" in data
        assert isinstance(data["reminders"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["reminders"])
        
        print(f"✓ Admin reminders (no filter) passed: count={data['count']}")
    
    def test_admin_list_reminders_with_status_filter(self):
        """Test GET /api/admin/reminders?status=pending filters correctly"""
        response = requests.get(f"{BASE_URL}/api/admin/reminders?status=pending")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "reminders" in data
        
        # Verify all returned reminders have pending status
        for reminder in data["reminders"]:
            assert reminder.get("status") == "pending", f"Expected pending, got {reminder.get('status')}"
        
        print(f"✓ Admin reminders (status=pending) passed: count={data['count']}")
    
    def test_admin_list_reminders_with_limit(self):
        """Test GET /api/admin/reminders?limit=5 respects limit"""
        response = requests.get(f"{BASE_URL}/api/admin/reminders?limit=5")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "reminders" in data
        assert len(data["reminders"]) <= 5
        
        print(f"✓ Admin reminders (limit=5) passed: count={data['count']}")


class TestAdminMessages:
    """Admin messages endpoint tests"""
    
    def test_admin_list_messages(self):
        """Test GET /api/admin/messages returns recent messages"""
        response = requests.get(f"{BASE_URL}/api/admin/messages")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "messages" in data
        assert isinstance(data["messages"], list)
        assert isinstance(data["count"], int)
        assert data["count"] == len(data["messages"])
        
        print(f"✓ Admin messages passed: count={data['count']}")
    
    def test_admin_list_messages_with_limit(self):
        """Test GET /api/admin/messages?limit=10 respects limit"""
        response = requests.get(f"{BASE_URL}/api/admin/messages?limit=10")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "messages" in data
        assert len(data["messages"]) <= 10
        
        print(f"✓ Admin messages (limit=10) passed: count={data['count']}")


class TestWhatsAppWebhook:
    """WhatsApp webhook endpoint tests"""
    
    def test_webhook_with_phone_number_message(self):
        """Test POST /api/webhook/whatsapp with phone number like '+919582790310'"""
        # Simulate Twilio webhook format
        payload = {
            "From": "whatsapp:+61412345678",
            "To": "whatsapp:+15393091015",
            "Body": "+919582790310",
            "MessageSid": "TEST_SM123456789",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        # Webhook should return 200 with empty or TwiML response
        assert response.status_code == 200
        print(f"✓ Webhook with phone number passed: status={response.status_code}")
    
    def test_webhook_with_reminder_message(self):
        """Test POST /api/webhook/whatsapp with reminder request"""
        payload = {
            "From": "whatsapp:+61412345678",
            "To": "whatsapp:+15393091015",
            "Body": "Remind me to call mom in 30 minutes",
            "MessageSid": "TEST_SM987654321",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Webhook with reminder message passed: status={response.status_code}")
    
    def test_webhook_with_greeting(self):
        """Test POST /api/webhook/whatsapp with greeting message"""
        payload = {
            "From": "whatsapp:+61412345678",
            "To": "whatsapp:+15393091015",
            "Body": "Hello",
            "MessageSid": "TEST_SM111111111",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Webhook with greeting passed: status={response.status_code}")
    
    def test_webhook_with_consent_yes(self):
        """Test POST /api/webhook/whatsapp with consent approval"""
        payload = {
            "From": "whatsapp:+919582790310",
            "To": "whatsapp:+15393091015",
            "Body": "YES",
            "MessageSid": "TEST_SM222222222",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Webhook with consent YES passed: status={response.status_code}")
    
    def test_webhook_with_help_request(self):
        """Test POST /api/webhook/whatsapp with help request"""
        payload = {
            "From": "whatsapp:+61412345678",
            "To": "whatsapp:+15393091015",
            "Body": "help",
            "MessageSid": "TEST_SM333333333",
            "AccountSid": "TEST_AC123456789",
            "NumMedia": "0"
        }
        
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        
        assert response.status_code == 200
        print(f"✓ Webhook with help request passed: status={response.status_code}")


class TestAdminPendingActions:
    """Admin pending actions endpoint tests"""
    
    def test_admin_list_pending_actions(self):
        """Test GET /api/admin/pending-actions returns pending conversational actions"""
        response = requests.get(f"{BASE_URL}/api/admin/pending-actions")
        assert response.status_code == 200
        data = response.json()
        
        assert "count" in data
        assert "pending_actions" in data
        assert isinstance(data["pending_actions"], list)
        
        print(f"✓ Admin pending actions passed: count={data['count']}")


class TestTwilioSettings:
    """Twilio settings endpoint tests"""
    
    def test_twilio_status(self):
        """Test GET /api/settings/twilio returns configuration status"""
        response = requests.get(f"{BASE_URL}/api/settings/twilio")
        assert response.status_code == 200
        data = response.json()
        
        assert "configured" in data
        assert isinstance(data["configured"], bool)
        
        print(f"✓ Twilio status passed: configured={data['configured']}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
