from django.shortcuts import render
from django.http import JsonResponse
from game.consumers import GAMES, TURN_TIME
from game.validators import validate_room_id, ValidationError
import logging
import time

logger = logging.getLogger('game')

VIEW_COUNT = 0

def get_views(request):
    global VIEW_COUNT
    VIEW_COUNT += 1
    return JsonResponse({"views": VIEW_COUNT})


def home(request):
    return render(request, "game/home.html")


def game(request, room_id):
    try:
        validate_room_id(room_id)
    except ValidationError as e:
        logger.warning(f"Invalid room_id in game view: {e}")
        return JsonResponse({"error": "Invalid room ID"}, status=400)
    
    try:
        return render(request, "game/game.html", {"room_id": room_id})
    except Exception as e:
        logger.error(f"Error rendering game view for {room_id}: {e}", exc_info=True)
        return JsonResponse({"error": "Error loading game"}, status=500)


def list_rooms(request):
    try:
        rooms = []
        for code, game_state in GAMES.items():
            try:
                if len(game_state["players"]) < 2 and not game_state.get("winner"):
                    rooms.append({
                        "code": code,
                        "players": len(game_state["players"]),
                        "private": game_state.get("private", False),
                        "name": game_state.get("name", ""),
                        "turn_time": game_state.get("turn_time", TURN_TIME),
                    })
            except Exception as e:
                logger.warning(f"Error processing game {code} in list_rooms: {e}")
                continue
        
        return JsonResponse(rooms, safe=False)
    except Exception as e:
        logger.error(f"Error in list_rooms: {e}", exc_info=True)
        return JsonResponse({"error": "Error listing rooms"}, status=500)


def health_check(request):
    try:
        from game.consumers import get_redis_client
        
        status = {
            "status": "healthy",
            "timestamp": time.time(),
            "active_games": len(GAMES),
            "redis": "unknown"
        }
        
        try:
            redis_client = get_redis_client()
            if redis_client:
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                redis_ok = loop.run_until_complete(redis_client.ping())
                loop.close()
                
                status["redis"] = "connected" if redis_ok else "disconnected"
            else:
                status["redis"] = "not_initialized"
                status["status"] = "degraded"
        except Exception as e:
            logger.warning(f"Redis health check failed: {e}")
            status["redis"] = "error"
            status["status"] = "degraded"
        
        http_status = 200 if status["status"] == "healthy" else 503
        return JsonResponse(status, status=http_status)
        
    except Exception as e:
        logger.error(f"Health check error: {e}", exc_info=True)
        return JsonResponse({
            "status": "error",
            "error": str(e),
            "timestamp": time.time()
        }, status=500)