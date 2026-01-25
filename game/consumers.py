from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings
import redis.asyncio as aioredis
import json
import asyncio
import time

TURN_TIME = 5
COUNTDOWN_SECONDS = 3
GAMES = {}

redis_client = None


def get_redis_client():
    global redis_client
    if redis_client is None:
        redis_url = getattr(settings, 'REDIS_URL', 'redis://127.0.0.1:6379/0')
        redis_client = aioredis.from_url(redis_url, decode_responses=True)
    return redis_client


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
        }
        client = get_redis_client()
        await client.set(f"game:{room_id}", json.dumps(to_store))
    except Exception:
        pass


async def load_game_from_redis(room_id):
    try:
        client = get_redis_client()
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
        return stored
    except Exception:
        return None


async def delete_game_from_redis(room_id):
    try:
        client = get_redis_client()
        await client.delete(f"game:{room_id}")
    except Exception:
        pass


async def turn_timeout_task(room_id):
    try:
        # read the per-game turn_time if present
        game = GAMES.get(room_id)
        if not game:
            return
        sleep_time = game.get("turn_time", TURN_TIME)
        await asyncio.sleep(sleep_time)
    except asyncio.CancelledError:
        return

    game = GAMES.get(room_id)
    if not game or game.get("winner"):
        return

    game["turn"] = "O" if game["turn"] == "X" else "X"
    game["turn_started"] = time.time()

    game["timer_task"] = asyncio.create_task(turn_timeout_task(room_id))

    try:
        await save_game_to_redis(room_id, game)
    except Exception:
        pass

    message = {
        "type": "turn_timeout",
        "board": game["board"],
        "turn": game["turn"],
        "turn_started": game["turn_started"],
        "turn_time": game.get("turn_time", TURN_TIME),
    }

    channel_layer = get_channel_layer()
    await channel_layer.group_send(f"room_{room_id}", message)


async def countdown_task(room_id):
    try:
        await asyncio.sleep(COUNTDOWN_SECONDS)
    except asyncio.CancelledError:
        return

    game = GAMES.get(room_id)
    if not game:
        return

    if len(game.get("players", [])) < 2:
        game["countdown_started"] = None
        game["countdown_seconds"] = None
        game["countdown_task"] = None
        try:
            await save_game_to_redis(room_id, game)
        except Exception:
            pass
        return

    game["game_started"] = True
    game["countdown_started"] = None
    game["countdown_seconds"] = None
    game["countdown_task"] = None

    game["turn_started"] = time.time()
    game["timer_task"] = asyncio.create_task(turn_timeout_task(room_id))

    try:
        await save_game_to_redis(room_id, game)
    except Exception:
        pass

    message = {
        "type": "game_start",
        "turn": game["turn"],
        "turn_started": game["turn_started"],
        "turn_time": game.get("turn_time", TURN_TIME),
    }

    channel_layer = get_channel_layer()
    await channel_layer.group_send(f"room_{room_id}", message)


class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.room_group_name = f"room_{self.room_id}"

        from urllib.parse import parse_qs
        query_string = self.scope.get('query_string', b"").decode()
        params = parse_qs(query_string)
        is_private = params.get('private', ['false'])[0] == 'true'
        room_name = params.get('name', [''])[0] or ''
        # parse optional per-room turn time (in seconds)
        turn_time_raw = params.get('turn_time', [None])[0]
        turn_time = TURN_TIME
        if turn_time_raw is not None:
            try:
                parsed = float(turn_time_raw)
                # clamp between 0.5 and 5.0
                parsed = max(0.5, min(5.0, parsed))
                turn_time = parsed
            except Exception:
                turn_time = TURN_TIME

        
        if self.room_id not in GAMES:
            loaded = await load_game_from_redis(self.room_id)
            if loaded:
                GAMES[self.room_id] = loaded
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
                }
                await save_game_to_redis(self.room_id, GAMES[self.room_id])

        game = GAMES[self.room_id]

        if len(game["players"]) >= 2:
            await self.close()
            return

        self.symbol = "X" if "X" not in game["players"] else "O"
        game["players"].append(self.symbol)
        game.setdefault("channels", []).append(self.channel_name)
        await save_game_to_redis(self.room_id, game)

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
        }))

        if len(game["players"]) == 2 and not game["game_started"]:
            if game.get("countdown_task"):
                try:
                    game["countdown_task"].cancel()
                except Exception:
                    pass

            game["countdown_started"] = time.time()
            game["countdown_seconds"] = COUNTDOWN_SECONDS
            game["countdown_task"] = asyncio.create_task(countdown_task(self.room_id))

            await save_game_to_redis(self.room_id, game)

            await self._broadcast({
                "type": "countdown_start",
                "countdown_started": game["countdown_started"],
                "countdown_seconds": game["countdown_seconds"],
            })

    async def disconnect(self, close_code):
        game = GAMES.get(self.room_id)

        if game:
            leaving_symbol = getattr(self, 'symbol', None)
            any_moves_made = any(cell != "" for cell in game.get("board", []))
            was_game_active = game.get("game_started", False) and not game.get("winner") and any_moves_made

            if(hasattr(self, 'symbol') and self.symbol in game["players"]):
                game["players"].remove(self.symbol)
            if self.channel_name in game.get("channels", []):
                game["channels"].remove(self.channel_name)
            
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

                if was_game_active and len(game["players"]) == 1:
                    remaining = game["players"][0]
                    game["winner"] = remaining
                    
                    await self._broadcast({
                        "type": "game_over",
                        "winner": remaining,
                        "board": game["board"],
                        "forfeit": True,
                    })
                else:
                    await self._broadcast({
                        "type": "player_left",
                        "symbol": leaving_symbol or '?',
                    })

            if len(game.get("players", [])) > 0:
                await save_game_to_redis(self.room_id, game)
            else:
                await delete_game_from_redis(self.room_id)
                if len(game.get("players", [])) == 0:
                    del GAMES[self.room_id]
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        game = GAMES.get(self.room_id)
        if not game:
            return

        if data.get("action") == "rematch":
            game.setdefault("rematch_votes", [])
            if self.symbol not in game["rematch_votes"]:
                game["rematch_votes"].append(self.symbol)
            votes_needed = 2

            if len(game["rematch_votes"]) >= votes_needed:
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

                    await self._broadcast({
                        "type": "game_reset",
                        "board": game["board"],
                        "turn": game["turn"],
                        "turn_started": game.get("turn_started"),
                        "turn_time": game.get("turn_time", TURN_TIME),
                        "countdown_started": game["countdown_started"],
                        "countdown_seconds": game["countdown_seconds"],
                    })
                    
                    await self._broadcast({
                        "type": "countdown_start",
                        "countdown_started": game["countdown_started"],
                        "countdown_seconds": game["countdown_seconds"],
                    })
            else:
                await save_game_to_redis(self.room_id, game)
                await self._broadcast({
                    "type": "rematch_requested",
                    "symbol": self.symbol
                })
            return

        index = data.get("index")
        if index is None:
            return

        if game["winner"]:
            return
        if game["turn"] != self.symbol:
            return
        if not (0 <= index <= 8):
            return
        if game["board"][index] != "":
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

            await self._broadcast({
                "type": "game_over",
                "winner": "draw",
                "board": game["board"],
            })

            await asyncio.sleep(0.1)

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

            await self._broadcast({
                "type": "game_reset",
                "board": game["board"],
                "turn": game["turn"],
                "turn_started": game.get("turn_started"),
                "turn_time": game.get("turn_time", TURN_TIME),
                "countdown_started": game["countdown_started"],
                "countdown_seconds": game["countdown_seconds"],
            })

            await self._broadcast({
                "type": "countdown_start",
                "countdown_started": game["countdown_started"],
                "countdown_seconds": game["countdown_seconds"],
            })

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

    async def _broadcast(self, message):
        try:
            await self.channel_layer.group_send(self.room_group_name, message)
        except Exception:
            pass

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
        except Exception:
            pass

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