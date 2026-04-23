import gpiod
from gpiod.line import Direction, Value
import time

CHIP_PATH = "/dev/gpiochip4"
LINE_OFFSET = 17

try:
    with gpiod.request_lines(
        CHIP_PATH,
        consumer="buzzer_test",
        config={
            LINE_OFFSET: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.INACTIVE
            )
        },
    ) as request:
        print("Toggling buzzer...")
        for _ in range(10):
            request.set_value(LINE_OFFSET, Value.ACTIVE)
            time.sleep(0.1)
            request.set_value(LINE_OFFSET, Value.INACTIVE)
            time.sleep(0.1)
except Exception as e:
    print(f"Error: {e}")
