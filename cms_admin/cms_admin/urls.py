from django.contrib import admin
from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from django.urls import re_path, include

from cms import views as cms_views

urlpatterns = [
    path("django-admin/", admin.site.urls),
    path("health/", cms_views.health),
    path("api/public/blogs/", cms_views.public_blogs),
    path("api/public/blogs/<slug:slug>/", cms_views.public_blog_detail),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
