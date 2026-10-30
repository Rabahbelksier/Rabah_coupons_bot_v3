import asyncio
import unittest

from services.ai_priority import AIRequestPriority


class AIRequestPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_waits_for_active_user_request(self):
        priority = AIRequestPriority()
        user_release = asyncio.Event()
        channel_started = asyncio.Event()

        async def user_work():
            async with priority.user_request():
                await user_release.wait()

        async def channel_work():
            async with priority.channel_request():
                channel_started.set()

        user_task = asyncio.create_task(user_work())
        await asyncio.sleep(0)
        channel_task = asyncio.create_task(channel_work())
        await asyncio.sleep(0.02)
        self.assertFalse(channel_started.is_set())

        user_release.set()
        await asyncio.gather(user_task, channel_task)
        self.assertTrue(channel_started.is_set())

    async def test_user_does_not_wait_for_channel_work_already_started(self):
        priority = AIRequestPriority()
        channel_release = asyncio.Event()
        channel_started = asyncio.Event()
        user_started = asyncio.Event()

        async def channel_work():
            async with priority.channel_request():
                channel_started.set()
                await channel_release.wait()

        async def user_work():
            async with priority.user_request():
                user_started.set()

        channel_task = asyncio.create_task(channel_work())
        await channel_started.wait()
        user_task = asyncio.create_task(user_work())
        await asyncio.wait_for(user_started.wait(), timeout=0.2)

        channel_release.set()
        await asyncio.gather(channel_task, user_task)