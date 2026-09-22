import asyncio
import json
from typing import Dict, Any, Optional

try:
    import aioredis
except Exception:
    try:
        import redis.asyncio as aioredis
    except Exception:
        aioredis = None

from config import get_settings


class EventBroadcaster:
    """Broadcaster that supports local in-process queues and optional Redis pub/sub.

    If `redis_url` is configured, a single Redis subscription listens for messages
    and fans them out to local subscribers. Publishing will publish to Redis so
    other app instances receive the event.
    """
    REDIS_CHANNEL = "campusio:qr_events"

    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._redis = None
        self._redis_listener_task: Optional[asyncio.Task] = None
        self._redis_init_attempted = False
        # Deliberately NOT touching asyncio.get_event_loop()/create_task() here:
        # `broadcaster` is a module-level singleton, constructed at import time,
        # before uvicorn's event loop exists. On Python 3.12+ (3.14 hard-fails
        # instead of the old auto-create-with-warning), that crashes the whole
        # app on startup — every router import chain hits this. Redis setup is
        # deferred to first actual use, from inside an async call (publish/
        # subscribe), where a running loop is guaranteed to exist.

    def _ensure_redis_initialized(self):
        if self._redis_init_attempted:
            return
        self._redis_init_attempted = True

        settings = get_settings()
        redis_url = getattr(settings, 'redis_url', None) or None
        if not redis_url or aioredis is None:
            return
        try:
            self._redis = aioredis.from_url(redis_url)
            self._redis_listener_task = asyncio.get_running_loop().create_task(self._redis_listener())
        except Exception:
            # fall back to in-process only
            self._redis = None

    async def _redis_listener(self):
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(self.REDIS_CHANNEL)
            async for message in pubsub.listen():
                # message is a dict-like with type and data
                if message is None:
                    continue
                mtype = message.get('type')
                if mtype != 'message':
                    continue
                raw = message.get('data')
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                # fan-out to local subscribers
                for q in list(self._subscribers):
                    try:
                        q.put_nowait(raw)
                    except asyncio.QueueFull:
                        continue
        except asyncio.CancelledError:
            return
        except Exception:
            # swallow errors — listener can be restarted on next publish
            return

    def subscribe(self) -> asyncio.Queue:
        self._ensure_redis_initialized()
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except KeyError:
            pass

    async def publish(self, event: Dict[str, Any]):
        self._ensure_redis_initialized()
        data = json.dumps(event, default=str)
        # publish to local subscribers first
        for q in list(self._subscribers):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                continue

        # publish to redis so other processes can pick it up
        if self._redis:
            try:
                await self._redis.publish(self.REDIS_CHANNEL, data)
            except Exception:
                # ignore redis errors — local delivery already happened
                pass

    async def close(self):
        # Cancel listener
        if self._redis_listener_task:
            try:
                self._redis_listener_task.cancel()
            except Exception:
                pass
        if self._redis:
            try:
                await self._redis.close()
            except Exception:
                pass


# Module-level broadcaster instance
broadcaster = EventBroadcaster()
