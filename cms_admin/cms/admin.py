from django.contrib import admin
from django import forms
from django.urls import reverse
from django.utils.html import format_html

from .models import BlogPost, BlogImage, DemoBooking, SupportTicket, SupportTicketReply
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


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_id", "email", "subject", "status", "priority", "created_at", "reply_link")
    search_fields = ("ticket_id", "email", "subject")
    list_filter = ("status", "priority", "category")
    readonly_fields = ("ticket_id", "tracking_token", "created_at", "updated_at", "reply_link")
    fields = (
        "ticket_id",
        "tracking_token",
        "name",
        "email",
        "category",
        "subject",
        "message",
        "status",
        "priority",
        "assigned_to",
        "ip_address",
        "user_agent",
        "created_at",
        "updated_at",
        "reply_link",
    )

    def reply_link(self, obj):
        url = reverse("admin:cms_supportticketreply_add")
        return format_html('<a href="{}?ticket_id={}">Reply to this ticket</a>', url, obj.ticket_id)

    reply_link.short_description = "Quick reply"


@admin.register(SupportTicketReply)
class SupportTicketReplyAdmin(admin.ModelAdmin):
    list_display = ("ticket_id", "author", "author_type", "is_internal", "created_at")
    search_fields = ("ticket_id", "author")
    list_filter = ("author_type", "is_internal")
    readonly_fields = ("id", "created_at")
    fields = ("ticket_id", "author", "author_type", "message", "is_internal")

    class ReplyForm(forms.ModelForm):
        ticket_id = forms.ChoiceField(choices=())

        class Meta:
            model = SupportTicketReply
            fields = ("ticket_id", "author", "author_type", "message", "is_internal")

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            tickets = SupportTicket.objects.order_by("-created_at").values_list(
                "ticket_id", "ticket_id"
            )
            self.fields["ticket_id"].choices = list(tickets)

    form = ReplyForm

    def get_changeform_initial_data(self, request):
        initial = {"author": "BOOPPA Support", "author_type": "staff"}
        ticket_id = request.GET.get("ticket_id")
        if ticket_id:
            initial["ticket_id"] = ticket_id
        return initial
