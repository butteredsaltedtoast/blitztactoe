from collections import defaultdict
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings
import redis.asyncio as aioredis
import json
import asyncio
import time
import logging
from game.validators import validate_room_id, validate_move_index, validate_turn_time, validate_game_name, ValidationError

logger = logging.getLogger('game')

TURN_TIME = 5
COUNTDOWN_SECONDS = 3
GAMES = {}
GAME_CONNECTIONS = {}

GAME_LOCKS = defaultdict(asyncio.Lock)

redis_client = None


def get_redis_client():
    global redis_client
    if redis_client is None:
        redis_url = getattr(settings, 'REDIS_URL', 'redis://127.0.0.1:6379/0')
        try:
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
            logger.info("Redis client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Redis client: {e}")
            redis_client = None
    return redis_client



async def close_redis_client():
    global redis_client
    if redis_client is not None:
        try:
            await redis_client.close()
            logger.info("Redis client closed")
        except Exception as e:
            logger.warning(f"Error closing Redis client: {e}")
        finally:
            redis_client = None


async def save_game_to_redis(room_id, game):
    try:
        to_store = {
            "players": game.get("players", []),
            "board": game.get("board", [""] * 9),
            "turn": game.get("turn", "X"),
            "winner": game.get("winner"),
            "turn_started": game.get("turn_started"),
            "game_started": game.get("game_started", False),
            "last_starter": game.get("last_starter", "X"),
            "private": game.get("private", False),
            "name": game.get("name", ""),
            "rematch_votes": list(game.get("rematch_votes", [])),
            "countdown_started": game.get("countdown_started"),
            "countdown_seconds": game.get("countdown_seconds", None),
            "turn_time": game.get("turn_time", TURN_TIME),
            "ready_states": game.get("ready_states", {"X": False, "O": False}),
        }
        client = get_redis_client()
        if client:
            await client.set(f"game:{room_id}", json.dumps(to_store))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to serialize game state for {room_id}: {e}")
    except Exception as e:
        logger.warning(f"Failed to save game {room_id} to Redis: {e}")


async def load_game_from_redis(room_id):
    try:
        client = get_redis_client()
        if not client:
            return None
        data = await client.get(f"game:{room_id}")
        if not data:
            return None
        stored = json.loads(data)
        stored.setdefault("channels", [])
        stored.setdefault("timer_task", None)
        stored.setdefault("turn_started", stored.get("turn_started"))
        stored.setdefault("countdown_started", stored.get("countdown_started"))
        stored.setdefault("countdown_seconds", stored.get("countdown_seconds", None))
        stored.setdefault("turn_time", stored.get("turn_time", TURN_TIME))
        stored.setdefault("rematch_votes", stored.get("rematch_votes", []))
        stored.setdefault("ready_states", {"X": False, "O": False})
        return stored
    except json.JSONDecodeError as e:
        logger.error(f"Failed to deserialize game state for {room_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Failed to load game {room_id} from Redis: {e}")
        return None


async def delete_game_from_redis(room_id):
    try:
        client = get_redis_client()
        if client:
            await client.delete(f"game:{room_id}")
    except Exception as e:
        logger.warning(f"Failed to delete game {room_id} from Redis: {e}")


async def turn_timeout_task(room_id):
    try:
        game = GAMES.get(room_id)
        if not game:
            return
        sleep_time = game.get("turn_time", TURN_TIME)
        await asyncio.sleep(sleep_time)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error in turn_timeout_task for {room_id}: {e}")
        return

    game = GAMES.get(room_id)
    if not game or game.get("winner"):
        return

    game["turn"] = "O" if game["turn"] == "X" else "X"
    game["turn_started"] = time.time()

    game["timer_task"] = asyncio.create_task(turn_timeout_task(room_id))

    try:
        await save_game_to_redis(room_id, game)
    except Exception as e:
        logger.warning(f"Failed to save game state after timeout in {room_id}: {e}")

    message = {
        "type": "turn_timeout",
        "board": game["board"],
        "turn": game["turn"],
        "turn_started": game["turn_started"],
        "turn_time": game.get("turn_time", TURN_TIME),
    }

    try:
        channel_layer = get_channel_layer()
        await channel_layer.group_send(f"room_{room_id}", message)
    except Exception as e:
        logger.error(f"Failed to broadcast turn_timeout for {room_id}: {e}")


async def countdown_task(room_id):
    try:
        await asyncio.sleep(COUNTDOWN_SECONDS)
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.error(f"Error in countdown_task for {room_id}: {e}")
        return

    async with GAME_LOCKS[room_id]:
        game = GAMES.get(room_id)
        if not game:
            return

        if len(game.get("players", [])) < 2:
            game["countdown_started"] = None
            game["countdown_seconds"] = None
            game["countdown_task"] = None
            try:
                await save_game_to_redis(room_id, game)
            except Exception as e:
                logger.warning(f"Failed to save game state in countdown_task for {room_id}: {e}")
            return

        game["game_started"] = True
        game["countdown_started"] = None
        game["countdown_seconds"] = None
        game["countdown_task"] = None
        game["ready_states"] = {"X": False, "O": False}

        game["turn_started"] = time.time()
        game["timer_task"] = asyncio.create_task(turn_timeout_task(room_id))

        try:
            await save_game_to_redis(room_id, game)
        except Exception as e:
            logger.warning(f"Failed to save game state after countdown in {room_id}: {e}")

    message = {
        "type": "game_start",
        "turn": game["turn"],
        "turn_started": game["turn_started"],
        "turn_time": game.get("turn_time", TURN_TIME),
    }

    try:
        channel_layer = get_channel_layer()
        await channel_layer.group_send(f"room_{room_id}", message)
    except Exception as e:
        logger.error(f"Failed to broadcast game_start for {room_id}: {e}")


class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        try:
            self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
            
            try:
                validate_room_id(self.room_id)
            except ValidationError as e:
                logger.warning(f"Invalid room_id: {e}")
                await self.close()
                return
            
            self.room_group_name = f"room_{self.room_id}"

            from urllib.parse import parse_qs
            query_string = self.scope.get('query_string', b"").decode()
            params = parse_qs(query_string)
            
            is_private = params.get('private', ['false'])[0] == 'true'
            room_name = params.get('name', [''])[0] or ''
            try:
                room_name = validate_game_name(room_name)
            except ValidationError as e:
                logger.warning(f"Invalid game name: {e}")
                room_name = ""
            
            turn_time_raw = params.get('turn_time', [None])[0]
            turn_time = validate_turn_time(turn_time_raw)

            max_connections = getattr(settings, 'MAX_CONNECTIONS_PER_ROOM', 2)
            current_connections = len(GAME_CONNECTIONS.get(self.room_id, []))
            if current_connections >= max_connections:
                logger.warning(f"Room {self.room_id} has reached max connections limit")
                await self.close()
                return
            
            max_games = getattr(settings, 'MAX_CONCURRENT_GAMES', 10000)
            if len(GAMES) > max_games:
                logger.warning(f"Maximum concurrent games ({max_games}) reached")
                await self.close()
                return
            
            async with GAME_LOCKS[self.room_id]:

                if self.room_id not in GAMES:
                    loaded = await load_game_from_redis(self.room_id)
                    if loaded:
                        GAMES[self.room_id] = loaded
                        logger.info(f"Loaded game {self.room_id} from Redis")
                    else:
                        GAMES[self.room_id] = {
                            "players": [],
                            "board": [""] * 9,
                            "turn": "X",
                            "winner": None,
                            "timer_task": None,
                            "turn_started": None,
                            "game_started": False,
                            "channels": [],
                            "last_starter": "X",
                            "private" : is_private,
                            "name": room_name,
                            "turn_time": turn_time,
                            "rematch_votes": [],
                            "created_at": time.time(),
                            "ready_states": {"X": False, "O": False},
                        }
                        logger.info(f"Created new game {self.room_id}")
                        await save_game_to_redis(self.room_id, GAMES[self.room_id])

                game = GAMES[self.room_id]

                if len(game["players"]) >= 2:
                    logger.info(f"Room {self.room_id} is full, rejecting connection")
                    await self.close()
                    return

                self.symbol = "X" if "X" not in game["players"] else "O"
                game["players"].append(self.symbol)
                game.setdefault("channels", []).append(self.channel_name)
                
                GAME_CONNECTIONS.setdefault(self.room_id, []).append(self.channel_name)
                
                await save_game_to_redis(self.room_id, game)
                logger.info(f"Player {self.symbol} joined room {self.room_id}")

            await self.channel_layer.group_add(self.room_group_name, self.channel_name)
            await self.accept()

            await self.send(text_data=json.dumps({
                "type": "init",
                "symbol": self.symbol,
                "board": game["board"],
                "turn": game["turn"],
                "turn_time": game.get("turn_time", TURN_TIME),
                "game_started": game["game_started"],
                "turn_started": game["turn_started"],
                "countdown_started": game.get("countdown_started"),
                "countdown_seconds": game.get("countdown_seconds"),
                "ready_states": game.get("ready_states"),
            }))

        except Exception as e:
            logger.error(f"Error in connect for room {self.room_id}: {e}", exc_info=True)
            await self.close()

    async def disconnect(self, close_code):
        from game.middleware import rate_limiter
        rate_limiter.cleanup_channel(self.channel_name)

        should_broadcast_forfeit = False
        forfeit_winner = None
        forfeit_board = None
        should_broadcast_left = False
        leaving_symbol = None
        
        try:
            async with GAME_LOCKS[self.room_id]:
                game = GAMES.get(self.room_id)

                if game:
                    leaving_symbol = getattr(self, 'symbol', None)
                    any_moves_made = any(cell != "" for cell in game.get("board", []))
                    was_game_active = game.get("game_started", False) and not game.get("winner") and any_moves_made

                    if(hasattr(self, 'symbol') and self.symbol in game["players"]):
                        game["players"].remove(self.symbol)
                        logger.info(f"Player {self.symbol} left room {self.room_id}")
                    
                    if self.channel_name in game.get("channels", []):
                        game["channels"].remove(self.channel_name)
                    
                    if self.room_id in GAME_CONNECTIONS:
                        GAME_CONNECTIONS[self.room_id] = [c for c in GAME_CONNECTIONS[self.room_id] if c != self.channel_name]
                        if not GAME_CONNECTIONS[self.room_id]:
                            del GAME_CONNECTIONS[self.room_id]
                    
                    if len(game["players"]) < 2:
                        if game.get("timer_task"):
                            try:
                                game["timer_task"].cancel()
                            except Exception:
                                pass
                            game["timer_task"] = None
                            game["game_started"] = False
                            if game.get("countdown_task"):
                                try:
                                    game["countdown_task"].cancel()
                                except Exception:
                                    pass
                                game["countdown_task"] = None
                            game["countdown_started"] = None
                            game["countdown_seconds"] = None
                            game["ready_states"] = {"X": False, "O": False}

                        if was_game_active and len(game["players"]) == 1:
                            remaining = game["players"][0]
                            game["winner"] = remaining
                            should_broadcast_forfeit = True
                            forfeit_winner = remaining
                            forfeit_board = game["board"].copy()
                            logger.info(f"Player {remaining} won by forfeit in room {self.room_id}")
                        else:
                            should_broadcast_left = True

                    if len(game.get("players", [])) > 0:
                        await save_game_to_redis(self.room_id, game)
                    else:
                        await delete_game_from_redis(self.room_id)
                        if len(game.get("players", [])) == 0:
                            del GAMES[self.room_id]
                            logger.info(f"Cleaned up empty room {self.room_id}")
            
            if should_broadcast_forfeit:
                await self._broadcast({
                    "type": "game_over",
                    "winner": forfeit_winner,
                    "board": forfeit_board,
                    "forfeit": True,
                })
            elif should_broadcast_left:
                await self._broadcast({
                    "type": "player_left",
                    "symbol": leaving_symbol or '?',
                })
            
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        except Exception as e:
            logger.error(f"Error in disconnect for room {self.room_id}: {e}", exc_info=True)

    async def receive(self, text_data):
        from game.middleware import rate_limiter
        if await rate_limiter.check_rate_limit(self.channel_name):
            logger.warning(f"Rate limit exceeded for channel {self.channel_name} in room {self.room_id}")
            await self.send(text_data=json.dumps({
                "type": "error",
                "message": "Too many messages. Please slow down."
            }))
            return
        
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in receive for room {self.room_id}: {e}")
            await self.send(text_data=json.dumps({
                "type": "error",
                "message": "Invalid message format"
            }))
            return
        except Exception as e:
            logger.error(f"Error parsing message in room {self.room_id}: {e}")
            return
        
        try:
            game = GAMES.get(self.room_id)
            if not game:
                logger.warning(f"Game not found for room {self.room_id}")
                return
            
            if(data.get("type") == "player_ready"):
                await self.handle_player_ready()
                return

            if data.get("action") == "rematch":
                from game.schemas import validate_rematch_message, MessageValidationError
                try:
                    validate_rematch_message(data)
                    await self._handle_rematch(game)
                except MessageValidationError as e:
                    logger.warning(f"Invalid rematch message in room {self.room_id}: {e}")
                    await self.send(text_data=json.dumps({
                        "type": "error",
                        "message": str(e)
                    }))
                return

            from game.schemas import validate_move_message, MessageValidationError
            try:
                index = validate_move_message(data)
            except MessageValidationError as e:
                logger.warning(f"Invalid move message in room {self.room_id}: {e}")
                await self.send(text_data=json.dumps({
                    "type": "error",
                    "message": str(e)
                }))
                return

            if game["winner"]:
                logger.debug(f"Game already has winner in room {self.room_id}")
                return
            if game["turn"] != self.symbol:
                logger.debug(f"Not player's turn in room {self.room_id}")
                return
            if game["board"][index] != "":
                logger.debug(f"Cell {index} already occupied in room {self.room_id}")
                return

            game["board"][index] = self.symbol
            winner = self.check_winner(game["board"])
            draw = not winner and "" not in game["board"]

            if winner:
                game["winner"] = winner
                if game.get("timer_task"):
                    game["timer_task"].cancel()
                    game["timer_task"] = None
                await save_game_to_redis(self.room_id, game)
                logger.info(f"Player {winner} won in room {self.room_id}")

                await self._broadcast({
                    "type": "game_over",
                    "winner": winner,
                    "board": game["board"],
                })
            elif draw:
                game["winner"] = "draw"
                if game.get("timer_task"):
                    game["timer_task"].cancel()
                    game["timer_task"] = None
                await save_game_to_redis(self.room_id, game)
                logger.info(f"Draw in room {self.room_id}")

                await self._broadcast({
                    "type": "game_over",
                    "winner": "draw",
                    "board": game["board"],
                })

                await asyncio.sleep(0.1)
                await self._handle_draw_reset()
            else:
                game["turn"] = "O" if game["turn"] == "X" else "X"
                await self._start_new_turn(game)

                await save_game_to_redis(self.room_id, game)

                await self._broadcast({
                    "type": "turn_change",
                    "board": game["board"],
                    "turn": game["turn"],
                    "turn_started": game["turn_started"],
                    "turn_time": game.get("turn_time", TURN_TIME),
                })
        except Exception as e:
            logger.error(f"Error in receive for room {self.room_id}: {e}", exc_info=True)

    async def _handle_draw_reset(self):
        """Handle automatic game reset after a draw with lock protection"""
        reset_data = {}
        
        async with GAME_LOCKS[self.room_id]:
            game = GAMES.get(self.room_id)
            if not game or len(game["players"]) < 2:
                return
            
            game["board"] = [""] * 9
            game["turn"] = "O" if game.get("last_starter") == "X" else "X"
            game["last_starter"] = game["turn"]
            game["winner"] = None
            game["rematch_votes"] = []

            if game.get("countdown_task"):
                try:
                    game["countdown_task"].cancel()
                except Exception:
                    pass

            game["countdown_started"] = time.time()
            game["countdown_seconds"] = COUNTDOWN_SECONDS
            game["countdown_task"] = asyncio.create_task(countdown_task(self.room_id))

            await save_game_to_redis(self.room_id, game)
            
            reset_data = {
                "board": game["board"],
                "turn": game["turn"],
                "turn_started": game.get("turn_started"),
                "turn_time": game.get("turn_time", TURN_TIME),
                "countdown_started": game["countdown_started"],
                "countdown_seconds": game["countdown_seconds"],
            }
        
        await self._broadcast({
            "type": "game_reset",
            **reset_data
        })

        await self._broadcast({
            "type": "countdown_start",
            "countdown_started": reset_data["countdown_started"],
            "countdown_seconds": reset_data["countdown_seconds"],
        })

    async def _handle_rematch(self, game):
        should_reset = False
        reset_data = {}
        
        async with GAME_LOCKS[self.room_id]:
            game.setdefault("rematch_votes", [])
            if self.symbol not in game["rematch_votes"]:
                game["rematch_votes"].append(self.symbol)
            votes_needed = 2

            await save_game_to_redis(self.room_id, game)

            if len(game["rematch_votes"]) >= votes_needed:
                should_reset = True
                try:
                    game["board"] = [""] * 9
                    game["turn"] = "O" if game.get("last_starter") == "X" else "X"
                    game["last_starter"] = game["turn"]
                    game["winner"] = None
                    game["rematch_votes"] = []

                    if game.get("timer_task"):
                        try:
                            game["timer_task"].cancel()
                        except Exception:
                            pass
                        game["timer_task"] = None

                    if game.get("countdown_task"):
                        try:
                            game["countdown_task"].cancel()
                        except Exception:
                            pass
                        game["countdown_task"] = None

                    game["turn_started"] = None
                    game["countdown_started"] = time.time()
                    game["countdown_seconds"] = COUNTDOWN_SECONDS
                    game["countdown_task"] = asyncio.create_task(countdown_task(self.room_id))

                    await save_game_to_redis(self.room_id, game)
                    logger.info(f"Rematch started in room {self.room_id}")
                    
                    reset_data = {
                        "board": game["board"].copy(),
                        "turn": game["turn"],
                        "turn_started": game.get("turn_started"),
                        "turn_time": game.get("turn_time", TURN_TIME),
                        "countdown_started": game["countdown_started"],
                        "countdown_seconds": game["countdown_seconds"],
                    }
                except Exception as e:
                    logger.error(f"Error handling rematch in room {self.room_id}: {e}", exc_info=True)
                    should_reset = False
        
        if should_reset:
            await self._broadcast({
                "type": "game_reset",
                "board": reset_data["board"],
                "turn": reset_data["turn"],
                "turn_started": reset_data["turn_started"],
                "turn_time": reset_data["turn_time"],
                "countdown_started": reset_data["countdown_started"],
                "countdown_seconds": reset_data["countdown_seconds"],
            })
            
            await self._broadcast({
                "type": "countdown_start",
                "countdown_started": reset_data["countdown_started"],
                "countdown_seconds": reset_data["countdown_seconds"],
            })
        else:
            if len(game["rematch_votes"]) >= 2:
                await self._broadcast({
                    "type": "error",
                    "message": "Rematch failed - server error. Please try again."
                })
                logger.warning(f"Rematch reset failed for room {self.room_id} after both players voted")
            else:
                await self._broadcast({
                    "type": "rematch_requested",
                    "symbol": self.symbol
                })
                logger.debug(f"Rematch vote from {self.symbol} in room {self.room_id} ({len(game['rematch_votes'])}/2)")

    async def _broadcast(self, message):
        try:
            await self.channel_layer.group_send(self.room_group_name, message)
        except Exception as e:
            logger.error(f"Failed to broadcast message in room {self.room_id}: {e}")

    async def _start_new_turn(self, game):
        if game.get("timer_task"):
            try:
                game["timer_task"].cancel()
            except Exception:
                pass

        game["turn_started"] = time.time()
        game["timer_task"] = asyncio.create_task(turn_timeout_task(self.room_id))
        try:
            await save_game_to_redis(self.room_id, game)
        except Exception as e:
            logger.warning(f"Failed to save game state in _start_new_turn for {self.room_id}: {e}")

    async def handle_player_ready(self):
        async with GAME_LOCKS[self.room_id]:
            game = GAMES.get(self.room_id)
            if not game:
                return
            
            game["ready_states"][self.symbol] = True
            await save_game_to_redis(self.room_id, game)
            
            await self._broadcast({
                "type": "player_ready",
                "ready_states": game["ready_states"],
            })
            
            if all(game["ready_states"].values()) and not game.get("countdown_started"):
                game["countdown_started"] = time.time()
                game["countdown_seconds"] = COUNTDOWN_SECONDS
                game["countdown_task"] = asyncio.create_task(countdown_task(self.room_id))
                await save_game_to_redis(self.room_id, game)
                
                await self._broadcast({
                    "type": "countdown_start",
                    "countdown_started": game["countdown_started"],
                    "countdown_seconds": game["countdown_seconds"],
                })
                logger.info(f"Both players ready, starting countdown in room {self.room_id}")

    def check_winner(self, board):
        wins = [
            (0, 1, 2), (3, 4, 5), (6, 7, 8),
            (0, 3, 6), (1, 4, 7), (2, 5, 8),
            (0, 4, 8), (2, 4, 6),
        ]
        for a, b, c in wins:
            if board[a] and board[a] == board[b] == board[c]:
                return board[a]
        return None

    async def game_start(self, event):
        await self.send(text_data=json.dumps(event))

    async def turn_change(self, event):
        await self.send(text_data=json.dumps(event))

    async def turn_timeout(self, event):
        await self.send(text_data=json.dumps(event))

    async def game_over(self, event):
        await self.send(text_data=json.dumps(event))

    async def game_reset(self, event):
        await self.send(text_data=json.dumps(event))

    async def rematch_requested(self, event):
        await self.send(text_data=json.dumps(event))

    async def player_left(self, event):
        await self.send(text_data=json.dumps(event))

    async def countdown_start(self, event):
        await self.send(text_data=json.dumps(event))

    async def player_ready(self, event):
        await self.send(text_data=json.dumps(event))


def cleanup_inactive_games():
    current_time = time.time()
    max_idle_seconds = getattr(settings, 'MAX_GAME_IDLE_SECONDS', 3600)
    
    games_to_remove = []
    
    for room_id, game in GAMES.items():
        if len(game.get("players", [])) == 0:
            games_to_remove.append(room_id)
            continue
        
        created_at = game.get("created_at", current_time)
        if not game.get("game_started") and (current_time - created_at) > max_idle_seconds:
            games_to_remove.append(room_id)
            continue
    
    for room_id in games_to_remove:
        try:
            del GAMES[room_id]
            if room_id in GAME_CONNECTIONS:
                del GAME_CONNECTIONS[room_id]
            logger.info(f"Cleaned up inactive game {room_id}")
        except Exception as e:
            logger.warning(f"Error cleaning up game {room_id}: {e}")
    
    if games_to_remove:
        logger.info(f"Cleanup removed {len(games_to_remove)} inactive games, {len(GAMES)} games remaining")