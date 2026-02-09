from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("game/<str:room_id>/", views.game, name="game"),
    path("api/rooms/", views.list_rooms, name="list_rooms"),
    path("api/views/", views.get_views, name="get_views"),
    path("health/", views.health_check, name="health_check"),
]