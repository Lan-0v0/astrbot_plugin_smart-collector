import asyncio

from smart_collector.fetcher import AntiBotFetcher


def test_negative_limits_disable_timeout_and_concurrency_limit() -> None:
    async def scenario() -> None:
        fetcher = AntiBotFetcher(concurrency=-1, timeout=-1)
        try:
            assert fetcher._semaphore is None
            assert fetcher.timeout is None
        finally:
            await fetcher.close()

    asyncio.run(scenario())


def test_nonnegative_concurrency_still_uses_a_semaphore() -> None:
    async def scenario() -> None:
        fetcher = AntiBotFetcher(concurrency=3, timeout=10)
        try:
            assert fetcher._semaphore is not None
            assert fetcher._semaphore._value == 3
            assert fetcher.timeout == 10
        finally:
            await fetcher.close()

    asyncio.run(scenario())
