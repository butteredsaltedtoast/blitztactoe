from django.shortcuts import render
from django.http import JsonResponse
from game.consumers import GAMES, TURN_TIME

VIEW_COUNT = 0

def get_views(request):
    global VIEW_COUNT
    VIEW_COUNT += 1
    return JsonResponse({"views": VIEW_COUNT})


def home(request):
    return render(request, "game/home.html")


def game(request, room_id):
    return render(request, "game/game.html", {"room_id": room_id})


def list_rooms(request):
    rooms = []
    for code, game in GAMES.items():
        if len(game["players"]) < 2 and not game.get("winner"):
            rooms.append({
                "code": code,
                "players": len(game["players"]),
                "private": game.get("private", False),
                "name": game.get("name", ""),
                "turn_time": game.get("turn_time", TURN_TIME),
            })
    return JsonResponse(rooms, safe=False)