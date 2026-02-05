import asyncio

from app.orchestrator.engine import run_many


async def main() -> None:
    urls = [
        "https://example.com",
        "https://example.org",
    ]
    results = await run_many(urls)
    print(results)


if __name__ == "__main__":
    asyncio.run(main())
