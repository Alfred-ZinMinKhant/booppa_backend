with open("app/workers/celery_app.py", "r") as f:
    content = f.read()

# Replace the queues
content = content.replace('task_default_queue="default"', 'task_default_queue="fast_queue"')
content = content.replace('"queue": "default"', '"queue": "fast_queue"')
content = content.replace('"queue": "reports"', '"queue": "heavy_queue"')

# Fix comments
content = content.replace('celery" queue — otherwise explicitly-named tasks that', 'celery" queue — otherwise explicitly-named tasks that')
content = content.replace('consume via `-Q reports,default`', 'consume via `-Q fast_queue`')

with open("app/workers/celery_app.py", "w") as f:
    f.write(content)
