"""Charger serial syntax and explicit per-unit custom-number validation."""
import re


STANDARD_SERIAL = re.compile(r"WO-S[0-9]{5}-(?:[0-9]+|BS[0-9]+)-[0-9]{3}", re.ASCII)


def validate_serial(value, *, allow_custom=False):
    """Return a readable error, without changing the supplied number."""
    if not isinstance(value, str):
        return "Scan or enter the charger serial number."
    if type(allow_custom) is not bool:
        return "Choose standard format or Other number for this charger."
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "The charger number cannot contain control characters or line breaks."
    serial = value.strip()
    if not serial:
        return "Scan or enter the charger serial number."
    if len(serial) > 160:
        return "The charger serial number must be 160 characters or fewer."
    if not allow_custom and STANDARD_SERIAL.fullmatch(serial) is None:
        return "Use WO-S00430-1-001 or WO-S00261-BS1-019 format, or select Other number."
    return ""
