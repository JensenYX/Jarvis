import os
import re


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]"
            if re.search(
                r"(?i)(api.?key|authorization|password|secret|access.?token)", str(key)
            )
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    for key, secret in os.environ.items():
        if (
            re.search(r"(?i)(api.?key|password|secret|access.?token)", key)
            and len(secret) >= 8
        ):
            value = value.replace(secret, "[redacted]")
    value = re.sub(r'(?i)(bearer\s+)[^\s"\x27,}]+', r"\1[redacted]", value)
    return re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", value)
