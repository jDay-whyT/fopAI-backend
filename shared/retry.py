import asyncio
import random


def jitter_delay(base_seconds: float, attempt: int, max_seconds: float = 60.0) -> float:
    delay = min(base_seconds * (2**attempt), max_seconds)
    return delay * (0.5 + random.random())


async def backoff_sleep(base_seconds: float, attempt: int, max_seconds: float = 60.0) -> None:
    delay = jitter_delay(base_seconds, attempt, max_seconds)
    await asyncio.sleep(delay)
