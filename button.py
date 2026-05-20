import asyncio
import logging
import os

scan_button = None
try:
    from gpiozero import Button
    scan_button = Button(11, pull_up=True, bounce_time=0.1)
except ImportError:
    logging.warning("gpiozero not found. Physical button will not work unless on Pi.")
except Exception as e:
    logging.error(f"Failed to initialize GPIO11 button: {e}")


async def wait_for_press():
    if scan_button is None:
        await asyncio.sleep(3600)
        return 0.0
        
    while True:
        if scan_button.is_pressed:
            start_time = asyncio.get_event_loop().time()
            # Wait for release to avoid multiple triggers
            while scan_button.is_pressed:
                await asyncio.sleep(0.05)
            duration = asyncio.get_event_loop().time() - start_time
            return duration
        await asyncio.sleep(0.05)
