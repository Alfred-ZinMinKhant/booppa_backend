import os

from django.contrib import admin
from django.urls import path, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve as static_serve

from cms import views as cms_views
from cms import admin_api as cms_admin_api

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("health/", cms_views.health),
    # Blogs
    path("api/public/blogs/", cms_views.public_blogs),
    path("api/public/blogs/<slug:slug>/", cms_views.public_blog_detail),
    # RFP Tips
    path("api/public/rfp-tips/", cms_views.public_rfp_tips),
    path("api/public/rfp-tips/<slug:slug>/", cms_views.public_rfp_tip_detail),
    # Compliance Education
    path("api/public/compliance/", cms_views.public_compliance),
    path("api/public/compliance/<slug:slug>/", cms_views.public_compliance_detail),
    # Vendor Guides
    path("api/public/vendor-guides/", cms_views.public_vendor_guides),
    path("api/public/vendor-guides/<slug:slug>/", cms_views.public_vendor_guide_detail),
    # ── Authenticated CRUD (X-Admin-Token header required) ───────────────────
    # Specific routes FIRST — generic <str:kind> would otherwise shadow these
    path("api/admin/blogs/<uuid:pk>/images/", cms_admin_api.blog_images),
    path("api/admin/blogs/<uuid:pk>/images/<int:image_id>/", cms_admin_api.blog_image_detail),
    path("api/admin/bookings/", cms_admin_api.bookings_list),
    path("api/admin/bookings/<uuid:pk>/", cms_admin_api.bookings_detail),
    path("api/admin/tickets/", cms_admin_api.tickets_list),
    path("api/admin/tickets/<uuid:pk>/replies/", cms_admin_api.tickets_replies),
    path("api/admin/tickets/<uuid:pk>/", cms_admin_api.tickets_detail),
    # Generic catch-all LAST
    path("api/admin/<str:kind>/", cms_admin_api.content_list),
    path("api/admin/<str:kind>/<uuid:pk>/", cms_admin_api.content_detail),
]

# Serve media in DEBUG, or when explicitly enabled (e.g. behind a reverse proxy)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    urlpatterns += [
        re_path(
            r"^media/(?P<path>.*)$",
            static_serve,
            {"document_root": settings.MEDIA_ROOT},
        ),
    ]
