from sqlalchemy.orm import Session
from app.core.models import Subscription

class SubscriptionRepository:
    @staticmethod
    def get_by_stripe_subscription_id(db: Session, stripe_sub_id: str) -> Subscription | None:
        return (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == stripe_sub_id)
            .first()
        )

    @staticmethod
    def get_active_by_user_and_products(db: Session, user_id: str, product_types: list[str]) -> Subscription | None:
        return (
            db.query(Subscription)
            .filter(
                Subscription.user_id == user_id,
                Subscription.product_type.in_(product_types),
                Subscription.status.in_(("active", "trialing")),
            )
            .first()
        )
