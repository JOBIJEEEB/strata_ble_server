import os
import asyncio
import time
BUZZER_PIN = 17

_line_request = None
try:
    import gpiod
    from gpiod.line import Direction, Value
    _chip = gpiod.Chip("/dev/gpiochip0")
    _line_settings = gpiod.LineSettings(
        direction=Direction.OUTPUT, 
        output_value=Value.INACTIVE
    )
    _line_request = _chip.request_lines(
        consumer="strata_buzzer",
        config={BUZZER_PIN: _line_settings}
    )
except (ImportError, Exception) as e:
    os.system(f"pinctrl set {BUZZER_PIN} op dl")
    _line_request = None

def set_buzzer(state):
    if _line_request:
        _line_request.set_value(BUZZER_PIN, Value.ACTIVE if state else Value.INACTIVE)
    else:
        val = "dh" if state else "dl"
        os.system(f"pinctrl set {BUZZER_PIN} {val}")

async def async_beep(duration=0.1, duty_cycle=1.0):
    try:
        if duty_cycle >= 1.0:
            set_buzzer(1)
            await asyncio.sleep(duration)
            set_buzzer(0)
            return

        start_time = time.time()
        while (time.time() - start_time) < duration:
            set_buzzer(1)
            time.sleep(0.0005 * duty_cycle)
            set_buzzer(0)
            time.sleep(0.0005 * (1 - duty_cycle))
        
        await asyncio.sleep(0)
    except Exception:
        set_buzzer(0)

async def play_r2d2_chatter(duration=10):
    for i in range(6):
        await async_beep(0.2, duty_cycle=1.0)
        await asyncio.sleep(2.0)

async def play_start_scan():

    await async_beep(0.1, duty_cycle=1.0)

async def play_finish_scan():
    await async_beep(0.12, duty_cycle=1.0)
    await asyncio.sleep(0.1)
    await async_beep(0.06, duty_cycle=1.0)
    await asyncio.sleep(0.06)
    await async_beep(0.06, duty_cycle=1.0)
    await asyncio.sleep(0.1)
    await async_beep(0.12, duty_cycle=1.0)
    await asyncio.sleep(0.1)
    await async_beep(0.12, duty_cycle=1.0)
    
    await asyncio.sleep(0.35)
    await async_beep(0.12, duty_cycle=1.0)
    await asyncio.sleep(0.15)
    await async_beep(0.18, duty_cycle=1.0)

async def play_error():
    for _ in range(3):
        await async_beep(0.3, duty_cycle=1.0)
        await asyncio.sleep(0.3)

def cleanup():
    set_buzzer(0)
