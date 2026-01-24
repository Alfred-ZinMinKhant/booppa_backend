import os
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
DEBUG = os.environ.get("DEBUG", "0") == "1"

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "*").split(",")

CSRF_TRUSTED_ORIGINS = os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if os.environ.get("CSRF_TRUSTED_ORIGINS") else []

# CORS settings (allow all origins in DEBUG for local Next.js dev)
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOWED_ORIGINS = (
        os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if os.environ.get("CORS_ALLOWED_ORIGINS")
        else []
    )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "cms",
    "corsheaders",
    "ckeditor",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "cms_admin.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "cms_admin.wsgi.application"

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/postgres"
)
# Normalize SQLAlchemy-style URLs (e.g. 'postgresql+psycopg2://') to a
# scheme that dj_database_url understands ('postgresql://').
if DATABASE_URL.startswith("postgresql+psycopg2://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://", 1)
db_config = dj_database_url.parse(DATABASE_URL)
db_config.setdefault("CONN_MAX_AGE", 600)
DATABASES = {"default": db_config}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_L10N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "static"

# Use WhiteNoise to serve static files from Gunicorn in production
STATICFILES_STORAGE = "whitenoise.storage.CompressedStaticFilesStorage"

# Media (user uploaded files)
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# CKEditor configuration for a rich toolbar
CKEDITOR_CONFIGS = {
    "default": {
        "toolbar": "Custom",
        "toolbar_Custom": [
            ["Bold", "Italic", "Underline", "Strike", "Subscript", "Superscript"],
            ["NumberedList", "BulletedList", "Blockquote"],
            ["Link", "Unlink", "Anchor"],
            ["Image", "Table", "HorizontalRule", "Smiley", "SpecialChar"],
            ["RemoveFormat", "Source"],
        ],
        "height": 300,
        "width": "100%",
        "extraPlugins": ",".join(
            [
                "uploadimage",
                "div",
                "autolink",
                "autoembed",
                "embedsemantic",
                "autogrow",
                "widget",
                "lineutils",
                "clipboard",
                "dialog",
                "dialogui",
                "elementspath",
            ]
        ),
    }
}
