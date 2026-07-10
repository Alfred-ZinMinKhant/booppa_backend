import re

with open("app/api/stripe_webhook.py", "r") as f:
    content = f.read()

if "from app.core.idempotency import IdempotencyGuard" not in content:
    content = "from app.core.idempotency import IdempotencyGuard\n" + content

# Replace @router.post("/webhook")
content = re.sub(
    r'@router\.post\("/webhook"\)\s*async def stripe_webhook\(\s*request: Request,\s*\):',
    r'@router.post("/webhook", dependencies=[Depends(IdempotencyGuard(key_header="Stripe-Signature"))])\nasync def stripe_webhook(\n    request: Request,\n):',
    content
)

# Alternative signature format just in case
content = re.sub(
    r'@router\.post\("/webhook"\)\s*async def stripe_webhook\(request: Request\):',
    r'@router.post("/webhook", dependencies=[Depends(IdempotencyGuard(key_header="Stripe-Signature"))])\nasync def stripe_webhook(request: Request):',
    content
)

with open("app/api/stripe_webhook.py", "w") as f:
    f.write(content)

print("Stripe webhook patched")
