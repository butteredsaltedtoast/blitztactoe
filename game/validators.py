import re
import logging
from django.conf import settings

logger = logging.getLogger('game')


class ValidationError(Exception):
    pass


def validate_room_id(room_id):
    if not room_id:
        raise ValidationError("Room ID cannot be empty")
    
    if not isinstance(room_id, str):
        raise ValidationError("Room ID must be a string")
    
    if not re.match(r'^[a-zA-Z0-9_-]+$', room_id):
        raise ValidationError("Room ID contains invalid characters")
    
    if len(room_id) > 100:
        raise ValidationError("Room ID is too long (max 100 characters)")
    
    return True


def validate_move_index(index):
    if not isinstance(index, int):
        raise ValidationError("Move index must be an integer")
    
    if not (0 <= index <= 8):
        raise ValidationError("Move index must be between 0 and 8")
    
    return True


def validate_turn_time(turn_time):
    if turn_time is None:
        return getattr(settings, 'TURN_TIME', 5)
    
    try:
        parsed = float(turn_time)
    except (TypeError, ValueError):
        logger.warning(f"Invalid turn_time: {turn_time}, using default")
        return getattr(settings, 'TURN_TIME', 5)
    
    parsed = max(0.5, min(5.0, parsed))
    return parsed


def validate_game_name(name):
    if not name:
        return ""
    
    if not isinstance(name, str):
        raise ValidationError("Game name must be a string")
    
    name = name[:100]
    
    name = re.sub(r'[^\w\s\-.]', '', name) # maybe improve later?
    
    return name.strip()


def is_game_idle(game, current_time):
    max_idle = getattr(settings, 'MAX_GAME_IDLE_SECONDS', 3600)
    
    if len(game.get('players', [])) == 0:
        return True
    
    game_started = game.get('game_started', False)
    if not game_started:
        return False
    
    return False
