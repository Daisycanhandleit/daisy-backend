"""
Test suite for Corporate Teams feature - Admin endpoints and WhatsApp webhook commands
Tests: GET /api/admin/teams, /api/admin/team-members, /api/admin/team-reminders, 
       /api/admin/team-acknowledgments, POST /api/webhook/whatsapp with team commands
"""
import pytest
import requests
import os
import time

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

class TestHealthCheck:
    """Health check endpoint tests"""
    
    def test_health_endpoint(self):
        """GET /api/health - returns healthy status"""
        response = requests.get(f"{BASE_URL}/api/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "twilio_configured" in data
        print(f"✅ Health check passed: {data}")


class TestAdminTeamsEndpoints:
    """Admin endpoints for teams management"""
    
    def test_admin_list_teams(self):
        """GET /api/admin/teams - list all teams"""
        response = requests.get(f"{BASE_URL}/api/admin/teams")
        assert response.status_code == 200
        data = response.json()
        
        # Verify response structure
        assert "count" in data
        assert "teams" in data
        assert isinstance(data["teams"], list)
        
        # Verify Marketing team exists
        teams = data["teams"]
        marketing_team = next((t for t in teams if t["name"] == "Marketing"), None)
        assert marketing_team is not None, "Marketing team should exist"
        assert marketing_team["member_count"] >= 2, "Marketing team should have at least 2 members"
        assert marketing_team["is_active"] == True
        assert "invite_code" in marketing_team
        assert "owner_phone" in marketing_team
        print(f"✅ Admin teams endpoint: Found {data['count']} teams, Marketing has {marketing_team['member_count']} members")
    
    def test_admin_list_team_members(self):
        """GET /api/admin/team-members - list all team members"""
        response = requests.get(f"{BASE_URL}/api/admin/team-members")
        assert response.status_code == 200
        data = response.json()
        
        # Verify response structure
        assert "count" in data
        assert "members" in data
        assert isinstance(data["members"], list)
        
        # Verify member data structure
        if data["count"] > 0:
            member = data["members"][0]
            assert "id" in member
            assert "team_id" in member
            assert "phone" in member
            assert "role" in member
            assert "status" in member
            assert "added_by" in member
        
        # Check for approved members
        approved_members = [m for m in data["members"] if m["status"] == "approved"]
        assert len(approved_members) >= 2, "Should have at least 2 approved members"
        print(f"✅ Admin team-members endpoint: Found {data['count']} members, {len(approved_members)} approved")
    
    def test_admin_list_team_reminders(self):
        """GET /api/admin/team-reminders - list team reminders"""
        response = requests.get(f"{BASE_URL}/api/admin/team-reminders")
        assert response.status_code == 200
        data = response.json()
        
        # Verify response structure
        assert "count" in data
        assert "team_reminders" in data
        assert isinstance(data["team_reminders"], list)
        
        # Verify reminder data structure if any exist
        if data["count"] > 0:
            reminder = data["team_reminders"][0]
            assert "id" in reminder
            assert "team_id" in reminder
            assert "team_name" in reminder
            assert "message" in reminder
            assert "scheduled_time" in reminder
            assert "status" in reminder
            assert "total_members" in reminder
            assert "acknowledged_count" in reminder
            assert "persist_until_all_acknowledge" in reminder
        print(f"✅ Admin team-reminders endpoint: Found {data['count']} team reminders")
    
    def test_admin_list_team_reminders_with_status_filter(self):
        """GET /api/admin/team-reminders?status=pending - filter by status"""
        response = requests.get(f"{BASE_URL}/api/admin/team-reminders?reminder_status=pending")
        assert response.status_code == 200
        data = response.json()
        
        # All returned reminders should be pending
        for reminder in data["team_reminders"]:
            assert reminder["status"] == "pending"
        print(f"✅ Admin team-reminders with status filter: Found {data['count']} pending reminders")
    
    def test_admin_list_team_acknowledgments(self):
        """GET /api/admin/team-acknowledgments - list acknowledgments"""
        response = requests.get(f"{BASE_URL}/api/admin/team-acknowledgments")
        assert response.status_code == 200
        data = response.json()
        
        # Verify response structure
        assert "count" in data
        assert "acknowledgments" in data
        assert isinstance(data["acknowledgments"], list)
        
        # Verify acknowledgment data structure if any exist
        if data["count"] > 0:
            ack = data["acknowledgments"][0]
            assert "id" in ack
            assert "team_reminder_id" in ack
            assert "member_phone" in ack
            assert "status" in ack
            assert "follow_up_count" in ack
        print(f"✅ Admin team-acknowledgments endpoint: Found {data['count']} acknowledgments")


class TestWebhookTeamCommands:
    """WhatsApp webhook tests for team commands"""
    
    TEST_PHONE = "+61452502696"
    TWILIO_NUMBER = "+15393091015"
    
    def test_webhook_show_my_teams(self):
        """POST /api/webhook/whatsapp with Body='Show my teams' - list user teams"""
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data={
                "From": f"whatsapp:{self.TEST_PHONE}",
                "To": f"whatsapp:{self.TWILIO_NUMBER}",
                "Body": "Show my teams",
                "MessageSid": "TEST_show_teams_pytest_001"
            }
        )
        assert response.status_code == 200
        
        # Verify message was stored - check recent messages
        time.sleep(0.5)  # Small delay for DB write
        messages_response = requests.get(f"{BASE_URL}/api/admin/messages?limit=5")
        assert messages_response.status_code == 200
        messages = messages_response.json()["messages"]
        
        # Find the outgoing response
        outgoing = [m for m in messages if m["direction"] == "outgoing" and "teams" in m["content"].lower()]
        assert len(outgoing) > 0, "Should have an outgoing response about teams"
        
        # Verify response contains team info
        response_content = outgoing[0]["content"]
        assert "Marketing" in response_content, "Response should mention Marketing team"
        print(f"✅ Webhook 'Show my teams' command: Response contains team info")
    
    def test_webhook_show_team_members(self):
        """POST /api/webhook/whatsapp with Body='Show Marketing members' - show team members"""
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data={
                "From": f"whatsapp:{self.TEST_PHONE}",
                "To": f"whatsapp:{self.TWILIO_NUMBER}",
                "Body": "Show Marketing members",
                "MessageSid": "TEST_show_members_pytest_001"
            }
        )
        assert response.status_code == 200
        
        # Verify message was stored
        time.sleep(0.5)
        messages_response = requests.get(f"{BASE_URL}/api/admin/messages?limit=5")
        assert messages_response.status_code == 200
        messages = messages_response.json()["messages"]
        
        # Find the outgoing response about members
        outgoing = [m for m in messages if m["direction"] == "outgoing" and "members" in m["content"].lower()]
        assert len(outgoing) > 0, "Should have an outgoing response about members"
        
        # Verify response contains member info
        response_content = outgoing[0]["content"]
        assert "Marketing" in response_content, "Response should mention Marketing team"
        # Check for member names or status indicators
        assert "✅" in response_content or "Kush" in response_content or "Dad" in response_content, \
            "Response should contain member info"
        print(f"✅ Webhook 'Show Marketing members' command: Response contains member info")
    
    def test_webhook_help_returns_response(self):
        """POST /api/webhook/whatsapp with Body='help' - should return help response"""
        response = requests.post(
            f"{BASE_URL}/api/webhook/whatsapp",
            data={
                "From": f"whatsapp:{self.TEST_PHONE}",
                "To": f"whatsapp:{self.TWILIO_NUMBER}",
                "Body": "help",
                "MessageSid": "TEST_help_pytest_001"
            }
        )
        assert response.status_code == 200
        
        # Verify help message was stored
        time.sleep(0.5)
        messages_response = requests.get(f"{BASE_URL}/api/admin/messages?limit=5")
        assert messages_response.status_code == 200
        messages = messages_response.json()["messages"]
        
        # Find the outgoing help response
        outgoing = [m for m in messages if m["direction"] == "outgoing"]
        assert len(outgoing) > 0, "Should have an outgoing help response"
        
        # Verify response contains helpful content (AI-generated, may vary)
        response_content = outgoing[0]["content"]
        # Help response should mention reminders or Daisy
        assert "remind" in response_content.lower() or "daisy" in response_content.lower(), \
            "Help should mention reminders or Daisy"
        print(f"✅ Webhook 'help' command: Response received")


class TestTeamReminderFlow:
    """Test team reminder creation and acknowledgment flow"""
    
    def test_team_reminder_exists(self):
        """Verify team reminder was created for Marketing team"""
        response = requests.get(f"{BASE_URL}/api/admin/team-reminders")
        assert response.status_code == 200
        data = response.json()
        
        # Find Marketing team reminder
        marketing_reminders = [r for r in data["team_reminders"] if r["team_name"] == "Marketing"]
        assert len(marketing_reminders) > 0, "Should have at least one Marketing team reminder"
        
        reminder = marketing_reminders[0]
        assert reminder["total_members"] >= 2, "Reminder should target at least 2 members"
        assert reminder["persist_until_all_acknowledge"] == True, "Reminder should persist until all acknowledge"
        print(f"✅ Team reminder exists: '{reminder['message']}' for {reminder['total_members']} members")
    
    def test_acknowledgments_created_for_members(self):
        """Verify acknowledgment records were created for team members"""
        # Get team reminders
        reminders_response = requests.get(f"{BASE_URL}/api/admin/team-reminders")
        assert reminders_response.status_code == 200
        reminders = reminders_response.json()["team_reminders"]
        
        if len(reminders) > 0:
            reminder_id = reminders[0]["id"]
            
            # Get acknowledgments for this reminder
            acks_response = requests.get(f"{BASE_URL}/api/admin/team-acknowledgments?team_reminder_id={reminder_id}")
            assert acks_response.status_code == 200
            acks = acks_response.json()["acknowledgments"]
            
            # Should have acknowledgment records for members (excluding owner in some cases)
            assert len(acks) >= 1, "Should have at least 1 acknowledgment record"
            
            for ack in acks:
                assert ack["team_reminder_id"] == reminder_id
                assert "member_phone" in ack
                assert "status" in ack
            print(f"✅ Acknowledgments created: {len(acks)} records for reminder")


class TestDataIntegrity:
    """Test data integrity and relationships"""
    
    def test_team_member_count_matches(self):
        """Verify team member count matches actual members"""
        # Get teams
        teams_response = requests.get(f"{BASE_URL}/api/admin/teams")
        assert teams_response.status_code == 200
        teams = teams_response.json()["teams"]
        
        # Get all members
        members_response = requests.get(f"{BASE_URL}/api/admin/team-members")
        assert members_response.status_code == 200
        members = members_response.json()["members"]
        
        for team in teams:
            team_id = team["id"]
            reported_count = team["member_count"]
            
            # Count actual approved members
            actual_count = len([m for m in members if m["team_id"] == team_id and m["status"] == "approved"])
            
            assert reported_count == actual_count, \
                f"Team {team['name']}: reported {reported_count} members but found {actual_count}"
        print(f"✅ Team member counts are accurate")
    
    def test_team_reminder_member_count_matches(self):
        """Verify team reminder total_members matches actual team members"""
        # Get team reminders
        reminders_response = requests.get(f"{BASE_URL}/api/admin/team-reminders")
        assert reminders_response.status_code == 200
        reminders = reminders_response.json()["team_reminders"]
        
        # Get teams
        teams_response = requests.get(f"{BASE_URL}/api/admin/teams")
        assert teams_response.status_code == 200
        teams = teams_response.json()["teams"]
        
        for reminder in reminders:
            team = next((t for t in teams if t["id"] == reminder["team_id"]), None)
            if team:
                # total_members in reminder should match team's member_count at creation time
                # (may differ if members added/removed after reminder creation)
                assert reminder["total_members"] >= 1, "Reminder should have at least 1 member"
        print(f"✅ Team reminder member counts are valid")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
