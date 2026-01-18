from django.shortcuts import render
from django.http import JsonResponse
from game.consumers import GAMES


def home(request):
    return render(request, "game/home.html")


def game(request, room_id):
    return render(request, "game/game.html", {"room_id": room_id})


def list_rooms(request):
    rooms = []
    for code, game in GAMES.items():
        if len(game["players"]) < 2 and not game.get("winner") and not game.get("private"):
            rooms.append({
                "code": code,
                "players": len(game["players"]),
            })
    return JsonResponse(rooms, safe=False)