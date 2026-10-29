"""Multi Arcade — real-time, same-round multiplayer mini games.
Run: python server.py  (aiohttp required)
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiohttp import WSMsgType, web

ROOT = Path(__file__).parent
ROUND_LENGTH = int(os.environ.get("ROUND_LENGTH", "25"))
BREAK_LENGTH = int(os.environ.get("BREAK_LENGTH", "6"))
COUNTDOWN_LENGTH = int(os.environ.get("COUNTDOWN_LENGTH", "3"))
MAX_PLAYERS = 10
MAX_SCORE_PER_ROUND = 1000
ROUND_NAMES = ("두더지 잡기", "장애물 피하기", "순서 기억하기")
ROOMS: dict[str, "Room"] = {}
logger = logging.getLogger("multi_arcade")


def now_ms() -> int:
    return int(time.time() * 1000)


def random_code() -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(50):
        code = "".join(secrets.choice(letters) for _ in range(5))
        if code not in ROOMS:
            return code
    raise RuntimeError("방 코드를 만들 수 없습니다")


@dataclass
class Player:
    id: str
    name: str
    ws: web.WebSocketResponse
    score: int = 0
    round_score: int = 0
    lane: int = 1
    hearts: int = 3
    mole_cell: int = -1
    mole_expire: int = 0
    mole_hit: bool = True
    mole_combo: int = 0
    last_mole_action: int = 0
    last_lane_action: int = 0
    memory_sequence: list[int] = field(default_factory=list)
    memory_progress: int = 0
    last_memory_action: int = 0
    memory_round: int = 0
    memory_locked_until: int = 0

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "score": self.score,
            "roundScore": self.round_score, "lane": self.lane,
            "hearts": self.hearts,
        }


@dataclass
class Room:
    code: str
    players: dict[str, Player] = field(default_factory=dict)
    host_id: str = ""
    state: str = "lobby"
    round_index: int = 0
    ends_at: int = 0
    loop_task: Optional[asyncio.Task] = None
    minigame_task: Optional[asyncio.Task] = None
    created_at: int = field(default_factory=now_ms)

    def snapshot(self) -> dict:
        return {
            "type": "state", "code": self.code, "state": self.state,
            "roundIndex": self.round_index,
            "roundName": ROUND_NAMES[self.round_index] if self.round_index < len(ROUND_NAMES) else "최종 결과",
            "endsAt": self.ends_at, "hostId": self.host_id,
            "players": sorted((p.public() for p in self.players.values()),
                              key=lambda item: (-item["score"], item["name"].lower())),
            "serverTime": now_ms(),
        }

    async def send_to(self, player: Player, payload: dict):
        if not player.ws.closed:
            try:
                await player.ws.send_json(payload)
            except ConnectionError:
                pass

    async def broadcast(self, payload: dict):
        await asyncio.gather(*(self.send_to(p, payload) for p in list(self.players.values())),
                             return_exceptions=True)

    async def update(self):
        await self.broadcast(self.snapshot())

    async def reset(self):
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
        if self.minigame_task and not self.minigame_task.done():
            self.minigame_task.cancel()
        self.loop_task = None
        self.minigame_task = None
        self.state = "lobby"
        self.round_index = 0
        self.ends_at = 0
        for player in self.players.values():
            player.score = 0
            player.round_score = 0
        await self.update()

    async def start(self):
        if self.state != "lobby":
            return
        # Claim the room synchronously so rapid repeated clicks cannot create two loops.
        self.state = "countdown"
        self.round_index = 0
        for p in self.players.values():
            p.score = 0
            p.round_score = 0
        self.loop_task = asyncio.create_task(self.run_rounds())

    async def run_rounds(self):
        try:
            for idx in range(len(ROUND_NAMES)):
                self.round_index = idx
                self.state = "countdown"
                self.ends_at = now_ms() + COUNTDOWN_LENGTH * 1000
                await self.update()
                await asyncio.sleep(COUNTDOWN_LENGTH)
                self.state = "playing"
                self.ends_at = now_ms() + ROUND_LENGTH * 1000
                for p in self.players.values():
                    p.round_score = 0
                    p.mole_combo = 0
                    p.lane = 1
                    p.hearts = 3
                    p.memory_sequence = []
                    p.memory_progress = 0
                    p.memory_round = 0
                await self.update()
                if idx == 0:
                    self.minigame_task = asyncio.create_task(self.mole_loop())
                elif idx == 1:
                    self.minigame_task = asyncio.create_task(self.dodge_loop())
                else:
                    self.minigame_task = asyncio.create_task(self.memory_setup())
                await asyncio.sleep(ROUND_LENGTH)
                if self.minigame_task:
                    self.minigame_task.cancel()
                    try:
                        await self.minigame_task
                    except asyncio.CancelledError:
                        pass
                    self.minigame_task = None
                self.state = "between" if idx < len(ROUND_NAMES) - 1 else "final"
                self.ends_at = now_ms() + BREAK_LENGTH * 1000 if self.state == "between" else 0
                await self.update()
                if self.state == "between":
                    await asyncio.sleep(BREAK_LENGTH)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Room %s game loop failed", self.code)
            self.state = "lobby"
            self.ends_at = 0
            await self.update()

    def award(self, p: Player, amount: int) -> bool:
        if self.state != "playing" or amount <= 0:
            return False
        gained = min(amount, MAX_SCORE_PER_ROUND - p.round_score)
        if gained <= 0:
            return False
        p.score += gained
        p.round_score += gained
        return True

    async def mole_loop(self):
        while self.state == "playing" and self.round_index == 0:
            n = now_ms()
            for p in list(self.players.values()):
                # Same rules, individual targets so everyone can play independently.
                p.mole_cell = random.randrange(9)
                p.mole_hit = False
                p.mole_expire = n + 850
                await self.send_to(p, {"type": "mole", "cell": p.mole_cell,
                                       "expiresAt": p.mole_expire})
            await asyncio.sleep(0.98)

    async def dodge_loop(self):
        # Everyone gets the exact same obstacle sequence.
        seq = 0
        while self.state == "playing" and self.round_index == 1:
            seq += 1
            lane = random.randrange(3)
            impact_at = now_ms() + 850
            await self.broadcast({"type": "hazard", "lane": lane, "seq": seq,
                                  "impactAt": impact_at})
            await asyncio.sleep(0.85)
            changed = False
            for p in self.players.values():
                if p.hearts <= 0:
                    continue
                if p.lane == lane:
                    p.hearts -= 1
                    await self.send_to(p, {"type": "damage", "hearts": p.hearts})
                    changed = True
                else:
                    if self.award(p, 43):
                        changed = True
            if changed:
                await self.update()
            await asyncio.sleep(0.10)

    async def memory_setup(self):
        for p in list(self.players.values()):
            await self.new_memory(p, initial=True)

    async def new_memory(self, p: Player, initial: bool = False):
        if self.state != "playing" or self.round_index != 2:
            return
        if initial:
            p.memory_round = 0
        p.memory_round += 1
        length = min(2 + p.memory_round, 8)
        p.memory_sequence = [random.randrange(4) for _ in range(length)]
        p.memory_progress = 0
        # Prevent accepting input while the sequence is playing on client.
        p.memory_locked_until = now_ms() + length * 500 + 900
        await self.send_to(p, {"type": "memory", "sequence": p.memory_sequence,
                               "level": p.memory_round})

    async def handle_game_action(self, p: Player, data: dict):
        if self.state != "playing" or now_ms() >= self.ends_at:
            return
        action = data.get("type")
        n = now_ms()
        if action == "hit_mole" and self.round_index == 0:
            cell = data.get("cell")
            if type(cell) is not int or not 0 <= cell < 9:
                return
            if n - p.last_mole_action < 75:
                return
            p.last_mole_action = n
            if p.mole_hit or n > p.mole_expire or p.mole_cell != cell:
                if p.mole_cell != cell:
                    p.mole_combo = 0
                return
            p.mole_hit = True
            p.mole_combo += 1
            self.award(p, 45 + min(30, (p.mole_combo - 1) * 5))
            await self.send_to(p, {"type": "mole_hit", "combo": p.mole_combo})
            await self.update()
        elif action == "set_lane" and self.round_index == 1:
            lane = data.get("lane")
            if type(lane) is int and 0 <= lane < 3 and n - p.last_lane_action >= 55:
                p.lane = lane
                p.last_lane_action = n
                await self.send_to(p, {"type": "lane", "lane": lane})
        elif action == "memory_tap" and self.round_index == 2:
            cell = data.get("cell")
            if type(cell) is not int or not 0 <= cell <= 3 or n < p.memory_locked_until:
                return
            if n - p.last_memory_action < 100 or not p.memory_sequence:
                return
            p.last_memory_action = n
            expected = p.memory_sequence[p.memory_progress]
            if cell != expected:
                p.memory_progress = 0
                p.memory_locked_until = n + len(p.memory_sequence) * 500 + 900
                await self.send_to(p, {"type": "memory_miss",
                                       "sequence": p.memory_sequence, "level": p.memory_round})
                return
            p.memory_progress += 1
            if p.memory_progress == len(p.memory_sequence):
                self.award(p, 90 + 45 * len(p.memory_sequence))
                await self.send_to(p, {"type": "memory_clear", "level": p.memory_round})
                await self.update()
                # New sequence starts after a brief celebration on the client.
                p.memory_locked_until = now_ms() + 850
                asyncio.create_task(self.delayed_memory(p))
            else:
                await self.send_to(p, {"type": "memory_progress", "count": p.memory_progress})

    async def delayed_memory(self, p: Player):
        await asyncio.sleep(0.7)
        if p.id in self.players and self.players[p.id] is p:
            await self.new_memory(p)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=2048)
    await ws.prepare(request)
    room: Optional[Room] = None
    player: Optional[Player] = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type == WSMsgType.ERROR:
                    logger.warning("WebSocket failure: %s", ws.exception())
                continue
            try:
                data = msg.json()
            except (ValueError, TypeError):
                await ws.send_json({"type": "error", "message": "올바르지 않은 요청입니다."})
                continue
            if not isinstance(data, dict):
                continue
            typ = data.get("type")
            if player is None:
                if typ not in ("create", "join"):
                    continue
                name = data.get("name", "")
                if not isinstance(name, str):
                    name = ""
                name = re.sub(r"\s+", " ", name.strip())[:14]
                if not name:
                    await ws.send_json({"type": "error", "message": "닉네임을 입력해 주세요."})
                    continue
                if typ == "create":
                    if len(ROOMS) >= 200:
                        await ws.send_json({"type": "error", "message": "현재 방이 너무 많습니다."})
                        continue
                    room = Room(code=random_code())
                    ROOMS[room.code] = room
                else:
                    code = str(data.get("code", "")).upper().replace(" ", "")[:5]
                    room = ROOMS.get(code)
                    if not room:
                        await ws.send_json({"type": "error", "message": "존재하지 않는 방 코드입니다."})
                        continue
                    if room.state != "lobby":
                        await ws.send_json({"type": "error", "message": "게임 진행 중에는 입장할 수 없습니다."})
                        continue
                    if len(room.players) >= MAX_PLAYERS:
                        await ws.send_json({"type": "error", "message": "이 방은 최대 인원입니다."})
                        continue
                    if any(p.name.lower() == name.lower() for p in room.players.values()):
                        await ws.send_json({"type": "error", "message": "이미 사용 중인 닉네임입니다."})
                        continue
                player = Player(id=secrets.token_hex(8), name=name, ws=ws)
                room.players[player.id] = player
                if not room.host_id:
                    room.host_id = player.id
                await ws.send_json({"type": "welcome", "id": player.id, "code": room.code})
                await room.update()
            elif typ == "start" and player.id == room.host_id and room.state == "lobby":
                await room.start()
            elif typ == "restart" and player.id == room.host_id and room.state == "final":
                await room.reset()
            else:
                await room.handle_game_action(player, data)
    finally:
        if room is not None and player is not None:
            room.players.pop(player.id, None)
            if not room.players:
                ROOMS.pop(room.code, None)
                if room.loop_task:
                    room.loop_task.cancel()
                if room.minigame_task:
                    room.minigame_task.cancel()
            else:
                if room.host_id == player.id:
                    room.host_id = next(iter(room.players))
                await room.update()
    return ws


async def index(request):
    return web.FileResponse(ROOT / "static" / "index.html")


async def health(_):
    return web.json_response({"status": "ok", "activeRooms": len(ROOMS)})


def create_app():
    app = web.Application(client_max_size=2048)
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static/", ROOT / "static", show_index=False)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8080"))
    print(f"🎮 MULTI ARCADE http://localhost:{port}")
    web.run_app(create_app(), host="0.0.0.0", port=port)
