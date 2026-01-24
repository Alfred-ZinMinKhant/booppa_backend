from django.contrib import admin
from .models import BlogPost, BlogImage
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


BlogPostAdmin.inlines = []  # [BlogImageInline]
