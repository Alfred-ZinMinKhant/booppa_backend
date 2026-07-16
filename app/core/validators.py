from typing import Any

# A small blocklist of obvious test placeholders that should never reach customer-facing
# documents or emails (e.g., PDF generation, AI greetings).
TEST_PLACEHOLDERS = {
    "test", 
    "demo", 
    "spqr", 
    "foo", 
    "bar", 
    "qa",
    "null",
    "undefined"
}

def validate_name_field(name: Any) -> Any:
    """
    Basic sanity validation for company names and full names.
    Intended to be used with Pydantic's @field_validator.
    """
    if not isinstance(name, str):
        return name
        
    cleaned = name.strip()
    if not cleaned:
        return cleaned

    lower_name = cleaned.lower()

    if lower_name in TEST_PLACEHOLDERS:
        raise ValueError(f"'{name}' is an invalid placeholder name.")

    # Reject names consisting of a single character.
    if len(cleaned) < 2:
        raise ValueError("Name must be at least 2 characters long.")

    return cleaned
