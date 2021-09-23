import aiohttp
import asyncio
import logging
import time
import ujson
import websockets

import hummingbot.connector.derivative.binance_perpetual.constants as CONSTANTS

from typing import (
    Any,
    AsyncIterable,
    Dict,
    Optional,
    Tuple,
)
from websockets import ConnectionClosed

from hummingbot.connector.derivative.binance_perpetual import binance_perpetual_utils as utils
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.utils.async_utils import safe_ensure_future
from hummingbot.logger import HummingbotLogger


class BinancePerpetualUserStreamDataSource(UserStreamTrackerDataSource):
    _bpusds_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._bpusds_logger is None:
            cls._bpusds_logger = logging.getLogger(__name__)
        return cls._bpusds_logger

    @property
    def last_recv_time(self) -> float:
        return self._last_recv_time

    def __init__(self, api_key: str, domain: str = "binance_perpetual", throttler: Optional[AsyncThrottler] = None):
        super().__init__()
        self._api_key: str = api_key
        self._current_listen_key = None
        self._listen_for_user_stream_task = None
        self._last_recv_time: float = 0
        self._domain = domain
        self._throttler = throttler or self._get_throttler_instance()

    @classmethod
    def _get_throttler_instance(cls) -> AsyncThrottler:
        return AsyncThrottler(CONSTANTS.RATE_LIMITS)

    async def get_listen_key(self):
        async with aiohttp.ClientSession() as client:
            async with self._throttler.execute_task(limit_id=CONSTANTS.BINANCE_USER_STREAM_ENDPOINT):
                response: aiohttp.ClientResponse = await client.post(
                    url=utils.rest_url(CONSTANTS.BINANCE_USER_STREAM_ENDPOINT, self._domain),
                    headers={"X-MBX-APIKEY": self._api_key}
                )
                if response.status != 200:
                    raise IOError(f"Error fetching Binance Perpetual user stream listen key. "
                                  f"HTTP status is {response.status}.")
                data: Dict[str, str] = await response.json()
                return data["listenKey"]

    async def ping_listen_key(self, listen_key: str) -> bool:
        async with aiohttp.ClientSession() as client:
            async with self._throttler.execute_task(limit_id=CONSTANTS.BINANCE_USER_STREAM_ENDPOINT):
                response: aiohttp.ClientResponse = await client.put(
                    url=utils.rest_url(CONSTANTS.BINANCE_USER_STREAM_ENDPOINT, self._domain),
                    headers={"X-MBX-APIKEY": self._api_key},
                    params={"listenKey": listen_key})
                data: Tuple[str, Any] = await response.json()
                if "code" in data:
                    self.logger().warning(f"Failed to refresh the listen key {listen_key}: {data}")
                    return False
                return True

    async def ws_messages(self, client: websockets.WebSocketClientProtocol) -> AsyncIterable[str]:
        try:
            while True:
                try:
                    raw_msg: str = await asyncio.wait_for(client.recv(), timeout=50.0)
                    self._last_recv_time = time.time()
                    yield raw_msg
                except asyncio.TimeoutError:
                    self._last_recv_time = time.time()
                    pong_waiter = await client.ping()
                    await asyncio.wait_for(pong_waiter, timeout=50.0)
        except asyncio.TimeoutError:
            self.logger().warning("Websocket ping timed out. Going to reconnect... ")
            return
        except ConnectionClosed:
            return
        finally:
            await client.close()

    async def log_user_stream(self, output: asyncio.Queue):
        while True:
            try:
                stream_url: str = f"{utils.wss_url(CONSTANTS.PRIVATE_WS_ENDPOINT, self._domain)}/{self._current_listen_key}"
                ws: websockets.WebSocketClientProtocol = await websockets.connect(stream_url)
                async for raw_msg in self.ws_messages(ws):
                    msg_json: Dict[str, any] = ujson.loads(raw_msg)
                    output.put_nowait(msg_json)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error. Retrying after 5 seconds... ", exc_info=True)
                await asyncio.sleep(5)

    async def listen_for_user_stream(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        try:
            while True:
                try:
                    if self._current_listen_key is None:
                        self._current_listen_key = await self.get_listen_key()
                        self.logger().debug(f"Obtained listen key {self._current_listen_key}.")
                        if self._listen_for_user_stream_task is not None:
                            self._listen_for_user_stream_task.cancel()
                        self._listen_for_user_stream_task = safe_ensure_future(self.log_user_stream(output))
                        await self.wait_til_next_tick(seconds=3600)
                    success: bool = await self.ping_listen_key(self._current_listen_key)
                    if not success:
                        self._current_listen_key = None
                        if self._listen_for_user_stream_task is not None:
                            self._listen_for_user_stream_task.cancel()
                            self._listen_for_user_stream_task = None
                        continue
                    self.logger().debug(f"Refreshed listen key {self._current_listen_key}.")
                    await self.wait_til_next_tick(seconds=60)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger().error("Unexpected error while maintaning the user event listen key. Retrying after "
                                        "5 seconds...", exc_info=True)
                    await asyncio.sleep(5)
        finally:
            if self._listen_for_user_stream_task is not None:
                self._listen_for_user_stream_task.cancel()
                self._listen_for_user_stream_task = None
            self._current_listen_key = None
