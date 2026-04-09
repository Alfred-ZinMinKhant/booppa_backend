import os

from django.contrib import admin
from django.urls import path, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve as static_serve

from cms import views as cms_views

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
