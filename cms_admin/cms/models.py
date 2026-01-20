from django.db import models
from ckeditor.fields import RichTextField
import uuid
from django.utils import timezone


class BlogPost(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    content = RichTextField()
    author = models.CharField(max_length=255, null=True, blank=True)
    # CTA buttons editable in admin for non-technical users
    cta1_text = models.CharField(max_length=255, null=True, blank=True)
    cta1_url = models.CharField(max_length=1024, null=True, blank=True)
    cta2_text = models.CharField(max_length=255, null=True, blank=True)
    cta2_url = models.CharField(max_length=1024, null=True, blank=True)
    published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "blog_posts"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        # Auto-set published_at when publishing
        if self.published and not self.published_at:
            self.published_at = timezone.now()
        # If unpublishing, optionally clear published_at (keep history if desired)
        if not self.published:
            self.published_at = None
        # Auto-generate slug from title if not provided
        if not self.slug and self.title:
            # simple slugify fallback
            from django.utils.text import slugify

            base = slugify(self.title)[:240]
            slug = base
            # ensure uniqueness only if managed by Django; when managed=False, rely on DB unique constraint
            self.slug = slug

        super().save(*args, **kwargs)


class BlogImage(models.Model):
    id = models.AutoField(primary_key=True)
    blog_post = models.ForeignKey(
        BlogPost, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(upload_to="blog_images/")
    caption = models.CharField(max_length=255, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "blog_post_images"

    def __str__(self):
        return f"Image for {self.blog_post_id}"
