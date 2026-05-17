import asyncio
import logging
import os

scan_button = None
try:
    from gpiozero import Button
    scan_button = Button(11, pull_up=False, bounce_time=0.1)
except ImportError:
    logging.warning("gpiozero not found. Physical button will not work unless on Pi.")
except Exception as e:
    logging.error(f"Failed to initialize GPIO11 button: {e}")


async def wait_for_press():
    if scan_button is None:
        await asyncio.sleep(3600)
        return False
        
    while True:
        if scan_button.is_pressed:
            # Wait for release to avoid multiple triggers
            while scan_button.is_pressed:
                await asyncio.sleep(0.1)
            return True
        await asyncio.sleep(0.1)
