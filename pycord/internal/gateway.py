# Copyright (c) 2021-2022 VincentRPS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import asyncio
import logging
import platform
import traceback
import zlib
from random import random
from typing import Any

from aiohttp import WSMsgType

from pycord import utils

from ..state import ConnectionState
from .events import EventDispatcher

ZLIB_SUFFIX = b'\x00\x00\xff\xff'
url = 'wss://gateway.discord.gg/?v={version}&encoding=json&compress=zlib-stream'
_log = logging.getLogger(__name__)


class Shard:
    def __init__(
        self,
        shard_id: int,
        shard_count: int,
        state: ConnectionState,
        events: EventDispatcher,
        version: int = 10,
    ) -> None:
        self.id = shard_id
        self.shard_count = shard_count
        self._events = events
        self.version = version
        self._state = state
        self._session_id: str = None
        self.requests_this_minute: int = 0
        self._stop_clock: bool = False
        self._sequence: int = None
        self._buf = bytearray()
        self._inf = zlib.decompressobj()
        self._ratelimit_lock: asyncio.Event = None
        self._rot_done: asyncio.Event = asyncio.Event()
        self.current_rotation_done: bool = False

    async def start_clock(self):
        self._rot_done.clear()
        await asyncio.sleep(60)
        self.requests_this_minute = 0

        if self._stop_clock:
            return

        self._rot_done.set()

        try:
            await asyncio.create_task(self.start_clock())
        except:
            # user has most likely existed this process.
            return

    async def send(self, data: dict[Any, Any]) -> None:
        if self.requests_this_minute == 119:
            if self._ratelimit_lock:
                await self._ratelimit_lock.wait()
            else:
                self._ratelimit_lock = asyncio.Event()
                await self._rot_done.wait()
                self._ratelimit_lock.set()
                self._ratelimit_lock: asyncio.Event = None

        _log.debug(f'shard:{self.id}:> {data}')

        payload = utils.dumps(data)
        payload = payload.encode('utf-8')  # type: ignore

        await self._ws.send_bytes(payload)

        self.requests_this_minute += 1

    async def heartbeat(self, interval: float, is_hello: bool = False) -> None:
        if self._stop_clock:
            return

        if is_hello:
            init = interval * random()
            await asyncio.sleep(init)
        else:
            if not self._ws.closed:
                await asyncio.sleep(interval)
                await self.send({'op': 1, 'd': self._sequence})

        await asyncio.create_task(self.heartbeat(interval=interval, is_hello=False))

    async def connect(self, token: str) -> None:
        _log.debug(f'shard:{self.id}: Connecting to the Gateway.')
        self._ws = await self._state._app.http._session.ws_connect(url.format(version=self.version))

        self.token = token

        if self._session_id is None:
            await self.identify()
            asyncio.create_task(self.receive())
            asyncio.create_task(self.start_clock())
        else:
            _log.debug(f'shard:{self.id}:Reconnecting to Gateway')
            await self.resume()
            asyncio.create_task(self.receive())
            asyncio.create_task(self.start_clock())

    async def receive(self) -> None:
        async for message in self._ws:
            if message.type == WSMsgType.BINARY:
                if len(message.data) < 4 or message.data[-4:] != ZLIB_SUFFIX:
                    continue

                self._buf.extend(message.data)

                try:
                    encoded = self._inf.decompress(self._buf).decode('utf-8')
                except:
                    # This data was most likely corrupted
                    self._buf = bytearray()  # clear buffer
                    continue

                self._buf = bytearray()

                data: dict[Any, Any] = utils.loads(encoded)

                _log.debug(f'shard:{self.id}:< {encoded}')

                self._sequence: int = data['s']

                try:
                    self._events.dispatch('WEBSOCKET_MESSAGE_RECEIVE', data)
                except:
                    traceback.print_exc()

                op = data.get('op')
                t = data.get('t')

                if op == 0:
                    if t == 'READY':
                        _log.info(f'shard:{self.id}:Connected to Gateway')
                        self._events.dispatch('ready')
                        self._session_id = data['d']['session_id']
                elif op == 10:
                    interval = data['d']['heartbeat_interval'] / 1000
                    await self.heartbeat(interval=interval, is_hello=True)

            elif message.type == WSMsgType.CLOSED:
                # TODO: Handle the connection being closed.
                self._stop_clock = True
                break

    async def identify(self):
        await self.send(
            {
                'op': 2,
                'd': {
                    'token': self.token,
                    'intents': self._state._app.intents,
                    'properties': {
                        'os': platform.system(),
                        'browser': 'pycord',
                        'device': 'pycord',
                    },
                    'shard': (self.id, self.shard_count),
                    'compress': True,
                },
            }
        )

    async def resume(self) -> None:
        await self.send(
            {
                'op': 6,
                'd': {
                    'token': self.token,
                    'session_id': self._session_id,
                    'seq': self._sequence,
                },
            }
        )


class ShardManager:
    def __init__(self, shards: int, state: ConnectionState, events: EventDispatcher, version: int = 10) -> None:
        self._shards = shards
        self.shards: list[Shard] = []
        self.state = state
        self.version = version
        self.events = events

    async def connect(self, token: str) -> None:
        for i in range(self._shards):
            shard = Shard(i, self._shards, self.state, self.events, self.version)
            await shard.connect(token=token)
            self.shards.append(shard)
            await asyncio.sleep(5)
