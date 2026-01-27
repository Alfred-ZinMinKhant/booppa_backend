from django.contrib import admin
from .models import BlogPost, BlogImage, DemoBooking
from django.utils.html import format_html


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ("title", "slug", "author", "published", "published_at")
    search_fields = ("title", "slug", "author")
    list_filter = ("published",)
    # Auto-generated identifiers and timestamps should not be editable.
    readonly_fields = ("id", "slug", "published_at", "created_at", "updated_at")
    fieldsets = (
        ("Identifiers", {"fields": ("id", "slug")}),
        (None, {"fields": ("title", "author", "content")}),
        (
            "Call To Action (Buttons)",
            {"fields": ("cta1_text", "cta1_url", "cta2_text", "cta2_url")},
        ),
        ("Publication", {"fields": ("published", "published_at")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


class BlogImageInline(admin.TabularInline):
    model = BlogImage
    readonly_fields = ("thumbnail",)
    extra = 1

    def thumbnail(self, obj):
        if obj and obj.image:
            return format_html(
                "<img src='{}' style='max-height:100px;'/>", obj.image.url
            )
        return ""


BlogPostAdmin.inlines = [BlogImageInline]


@admin.register(DemoBooking)
class DemoBookingAdmin(admin.ModelAdmin):
    list_display = (
        "customer_name",
        "customer_email",
        "slot_date",
        "start_time",
        "status",
        "created_at",
    )
    search_fields = ("customer_name", "customer_email", "booking_token")
    list_filter = ("status", "slot_date")
    readonly_fields = (
        "id",
        "slot_id",
        "booking_token",
        "created_at",
        "updated_at",
    )
