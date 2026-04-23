import asyncio
import buzzer

async def test():
    print("Testing play_finish_scan...")
    await buzzer.play_finish_scan()
    print("Done.")

asyncio.run(test())
