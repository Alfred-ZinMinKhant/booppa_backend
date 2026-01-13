from app.core.models import User, Report
import uuid

def test_user_model():
    """Test User model creation"""
    user = User(
        email="test@example.com",
        hashed_password="hashed_password_123"
    )

    assert user.email == "test@example.com"
    assert user.is_active == True
    assert user.id is not None

def test_report_model():
    """Test Report model creation"""
    report = Report(
        owner_id=uuid.uuid4(),
        framework="MTCS",
        company_name="Test Company",
        assessment_data={"control": "test"}
    )

    assert report.framework == "MTCS"
    assert report.company_name == "Test Company"
    assert report.status == "pending"
    assert report.id is not None
