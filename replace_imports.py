import os
import re

replacements = {
    r'"app\.api\.stripe_webhook\._activate_subscription"': r'"app.services.fulfillment.subscriptions._activate_subscription"',
    r'"app\.api\.stripe_webhook\._defer_rfp_to_intake"': r'"app.services.fulfillment.single_products._defer_rfp_to_intake"',
    r'"app\.api\.stripe_webhook\._fulfill_standalone_no_report"': r'"app.services.fulfillment.bundles._fulfill_standalone_no_report"',
    r'from app\.api\.stripe_webhook import SUBSCRIPTION_PRODUCT_TYPES': r'from app.services.fulfillment.helpers import SUBSCRIPTION_PRODUCT_TYPES',
    r'from app\.api\.stripe_webhook import _activate_subscription': r'from app.services.fulfillment.subscriptions import _activate_subscription',
    r'from app\.api\.stripe_webhook import BUNDLE_COMPONENTS': r'from app.services.fulfillment.helpers import BUNDLE_COMPONENTS',
    r'from app\.api\.stripe_webhook import _fulfill_vendor_proof': r'from app.services.fulfillment.single_products import _fulfill_vendor_proof',
    r'"app\.api\.stripe_webhook\._apply_subscription_score_lever"': r'"app.services.fulfillment.helpers._apply_subscription_score_lever"',
    r'"app\.api\.stripe_webhook\._log_purchase_activity"': r'"app.services.fulfillment.helpers._log_purchase_activity"',
    r'import app\.api\.stripe_webhook as wh': r'import app.services.fulfillment.helpers as wh', # Most likely helpers for email alerts, we can adjust if needed
}

def process_directory(directory):
    for root, _, files in os.walk(directory):
        for file in files:
            if file.endswith(".py"):
                filepath = os.path.join(root, file)
                with open(filepath, 'r') as f:
                    content = f.read()
                
                new_content = content
                for old_pattern, new_pattern in replacements.items():
                    new_content = re.sub(old_pattern, new_pattern, new_content)
                
                # specific fixes for test_vendor_proof_certificate.py
                new_content = new_content.replace('app/api/stripe_webhook.py:_fulfill_vendor_proof', 'app/services/fulfillment/single_products.py:_fulfill_vendor_proof')
                
                if new_content != content:
                    with open(filepath, 'w') as f:
                        f.write(new_content)
                    print(f"Updated {filepath}")

process_directory("tests")
