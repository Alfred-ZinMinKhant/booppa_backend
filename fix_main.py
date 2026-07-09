with open("app/main.py", "r") as f:
    content = f.read()

# Imports to add
old_import = "from app.api.websocket import socket_app, start_event_relay"
new_import = "from app.api.websocket import socket_app, start_event_relay\nfrom app.core.json_logger import setup_json_logging"
if new_import not in content:
    content = content.replace(old_import, new_import)

# Setup logging
old_log = "logging.basicConfig(level=settings.LOG_LEVEL)"
new_log = "setup_json_logging(level=settings.LOG_LEVEL)"
if old_log in content:
    content = content.replace(old_log, new_log)

with open("app/main.py", "w") as f:
    f.write(content)
