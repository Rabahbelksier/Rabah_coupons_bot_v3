import asyncio
from contextlib import asynccontextmanager


class AIRequestPriority:
    """Give user requests admission priority without cancelling channel work."""

    def __init__(self):
        self._condition = None
        self._loop = None
        self._waiting_users = 0
        self._active_users = 0
        self._active_channels = 0

    def _condition_for_current_loop(self):
        loop = asyncio.get_running_loop()
        if self._condition is None or self._loop is not loop:
            self._loop = loop
            self._condition = asyncio.Condition()
            self._waiting_users = 0
            self._active_users = 0
            self._active_channels = 0
        return self._condition

    @asynccontextmanager
    async def user_request(self):
        condition = self._condition_for_current_loop()
        async with condition:
            self._waiting_users += 1
            self._waiting_users -= 1
            self._active_users += 1
            condition.notify_all()
        try:
            yield
        finally:
            async with condition:
                self._active_users -= 1
                condition.notify_all()

    @asynccontextmanager
    async def channel_request(self):
        condition = self._condition_for_current_loop()
        async with condition:
            while self._waiting_users or self._active_users:
                await condition.wait()
            self._active_channels += 1
            condition.notify_all()
        try:
            yield
        finally:
            async with condition:
                self._active_channels -= 1
                condition.notify_all()


ai_request_priority = AIRequestPriority()