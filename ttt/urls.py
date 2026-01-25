from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse
from django.conf import settings
from pathlib import Path

urlpatterns = [
    path('favicon.ico', lambda request: HttpResponse((Path(settings.BASE_DIR) / 'static' / 'favicon.svg').read_bytes(), content_type='image/svg+xml')),
    path("admin/", admin.site.urls),
    path("", include("game.urls")),
]