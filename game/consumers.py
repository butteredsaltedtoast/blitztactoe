from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
import json
import asyncio
import time

TURN_TIME = 5
GAMES = {}


async def turn_timeout_task(room_id):
    try:
        await asyncio.sleep(TURN_TIME)
    except asyncio.CancelledError:
        return

    game = GAMES.get(room_id)
    if not game or game.get("winner"):
        return

    game["turn"] = "O" if game["turn"] == "X" else "X"
    game["turn_started"] = time.time()

    game["timer_task"] = asyncio.create_task(turn_timeout_task(room_id))

    message = {
        "type": "turn_timeout",
        "board": game["board"],
        "turn": game["turn"],
        "turn_started": game["turn_started"],
        "turn_time": TURN_TIME,
    }

    channel_layer = get_channel_layer()
    for channel_name in game.get("channels", []):
        try:
            await channel_layer.send(channel_name, message)
        except Exception:
            pass


class GameConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.room_group_name = f"room_{self.room_id}"

        if self.room_id not in GAMES:
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
            }

        game = GAMES[self.room_id]

        if len(game["players"]) >= 2:
            await self.close()
            return

        self.symbol = "X" if "X" not in game["players"] else "O"
        game["players"].append(self.symbol)
        game["channels"].append(self.channel_name)

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

        await self.send(text_data=json.dumps({
            "type": "init",
            "symbol": self.symbol,
            "board": game["board"],
            "turn": game["turn"],
            "turn_time": TURN_TIME,
            "game_started": game["game_started"],
            "turn_started": game["turn_started"],
        }))

        if len(game["players"]) == 2 and not game["game_started"]:
            game["game_started"] = True
            self._start_new_turn(game)

            await self._broadcast({
                "type": "game_start",
                "turn": game["turn"],
                "turn_started": game["turn_started"],
                "turn_time": TURN_TIME,
            })

    async def disconnect(self, close_code):
        game = GAMES.get(self.room_id)
        if game:
            if hasattr(self, 'symbol') and self.symbol in game["players"]:
                game["players"].remove(self.symbol)
            if self.channel_name in game.get("channels", []):
                game["channels"].remove(self.channel_name)

            if len(game["players"]) < 2:
                if game.get("timer_task"):
                    game["timer_task"].cancel()
                    game["timer_task"] = None
                game["game_started"] = False

                await self._broadcast({
                    "type": "player_left",
                    "symbol": getattr(self, 'symbol', '?')
                })

            if len(game["players"]) == 0:
                del GAMES[self.room_id]

        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        game = GAMES.get(self.room_id)
        if not game:
            return

        if data.get("action") == "rematch":
            game.setdefault("rematch_votes", set()).add(self.symbol)
            votes_needed = 1 if game.get("winner") == "draw" else 2

            if len(game["rematch_votes"]) >= votes_needed:
                game["board"] = [""] * 9
                game["turn"] = "O" if game.get("last_starter") == "X" else "X"
                game["last_starter"] = game["turn"]
                game["winner"] = None
                game["rematch_votes"] = set()

                if game.get("timer_task"):
                    game["timer_task"].cancel()

                self._start_new_turn(game)

                await self._broadcast({
                    "type": "game_reset",
                    "board": game["board"],
                    "turn": game["turn"],
                    "turn_started": game["turn_started"],
                    "turn_time": TURN_TIME,
                })
            else:
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

            await self._broadcast({
                "type": "game_over",
                "winner": "draw",
                "board": game["board"],
            })
        else:
            game["turn"] = "O" if game["turn"] == "X" else "X"
            self._start_new_turn(game)

            await self._broadcast({
                "type": "turn_change",
                "board": game["board"],
                "turn": game["turn"],
                "turn_started": game["turn_started"],
                "turn_time": TURN_TIME,
            })

    async def _broadcast(self, message):
        game = GAMES.get(self.room_id)
        if not game:
            return
        for channel_name in game.get("channels", []):
            try:
                await self.channel_layer.send(channel_name, message)
            except Exception:
                pass

    def _start_new_turn(self, game):
        if game.get("timer_task"):
            game["timer_task"].cancel()

        game["turn_started"] = time.time()
        game["timer_task"] = asyncio.create_task(turn_timeout_task(self.room_id))

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