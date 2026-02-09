class MessageValidationError(Exception):
    pass


def validate_move_message(data):
    if not isinstance(data, dict):
        raise MessageValidationError("Message must be an object")
    
    if "index" not in data:
        raise MessageValidationError("Missing 'index' field")
    
    index = data["index"]
    
    if not isinstance(index, int):
        raise MessageValidationError("'index' must be an integer")
    
    if not (0 <= index <= 8):
        raise MessageValidationError("'index' must be between 0 and 8")
    
    return index


def validate_rematch_message(data):
    if not isinstance(data, dict):
        raise MessageValidationError("Message must be an object")
    
    if data.get("action") != "rematch":
        raise MessageValidationError("Invalid action")
    
    return True
