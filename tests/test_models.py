from app.core.models import User, Report
import uuid


def test_user_model(test_db):
    """Test User model creation — flushes the row so SQLAlchemy applies the
    column-level defaults (is_active, id) before the assertions."""
    user = User(
        email="test@example.com",
        hashed_password="hashed_password_123",
    )
    test_db.add(user)
    test_db.flush()  # triggers Python + DB-side defaults

    assert user.email == "test@example.com"
    assert user.is_active is True
    assert user.id is not None


def test_report_model(test_db):
    """Test Report model creation. Owner row required by the users FK."""
    owner = User(email="owner@example.com", hashed_password="x")
    test_db.add(owner)
    test_db.flush()

    report = Report(
        owner_id=owner.id,
        framework="MTCS",
        company_name="Test Company",
        assessment_data={"control": "test"},
    )
    test_db.add(report)
    test_db.flush()

    assert report.framework == "MTCS"
    assert report.company_name == "Test Company"
    assert report.status == "pending"
    assert report.id is not None
