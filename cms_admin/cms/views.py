from django.http import JsonResponse
from .models import BlogPost


def health(request):
    return JsonResponse({"status": "healthy", "service": "booppa-cms"})


def public_blogs(request):
    posts = (
        BlogPost.objects.filter(published=True)
        .order_by("-published_at")
        .values(
            "id",
            "title",
            "slug",
            "content",
            "author",
            "cta1_text",
            "cta1_url",
            "cta2_text",
            "cta2_url",
            "published_at",
            "created_at",
            "updated_at",
        )
    )

    results = []
    for p in posts:
        # collect image URLs
        images = []
        try:
            blog = BlogPost.objects.get(pk=p["id"])
            for img in blog.images.all():
                images.append(request.build_absolute_uri(img.image.url))
        except BlogPost.DoesNotExist:
            images = []

        p["images"] = images
        results.append(p)

    return JsonResponse({"results": results})


def public_blog_detail(request, slug):
    try:
        blog = BlogPost.objects.get(slug=slug, published=True)
    except BlogPost.DoesNotExist:
        return JsonResponse({"detail": "Not found"}, status=404)

    images = [request.build_absolute_uri(img.image.url) for img in blog.images.all()]
    data = {
        "id": str(blog.id),
        "title": blog.title,
        "slug": blog.slug,
        "content": blog.content,
        "author": blog.author,
        "cta1_text": blog.cta1_text,
        "cta1_url": blog.cta1_url,
        "cta2_text": blog.cta2_text,
        "cta2_url": blog.cta2_url,
        "published_at": blog.published_at,
        "created_at": blog.created_at,
        "updated_at": blog.updated_at,
        "images": images,
    }
    return JsonResponse(data)
