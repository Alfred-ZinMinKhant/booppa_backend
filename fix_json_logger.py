with open("app/core/json_logger.py", "r") as f:
    content = f.read()

replacement = """
    root_logger = logging.getLogger()
    
    # Remove existing handlers to avoid duplicate logs
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
        
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Intercept uvicorn loggers
    for logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        l = logging.getLogger(logger_name)
        l.handlers = []
        l.propagate = True
"""

if "root_logger = logging.getLogger()" in content:
    # replace everything from root_logger onwards
    lines = content.split("    root_logger = logging.getLogger()")[0]
    content = lines + replacement
    with open("app/core/json_logger.py", "w") as f:
        f.write(content)
