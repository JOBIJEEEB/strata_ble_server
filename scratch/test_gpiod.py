import gpiod
from gpiod.line import Direction, Value

def find_chip():
    for i in range(15):
        try:
            chip_path = f"/dev/gpiochip{i}"
            with gpiod.Chip(chip_path) as chip:
                info = chip.get_info()
                print(f"Chip {i}: {info.name} ({info.label}) - {info.num_lines} lines")
                # Try to find if GPIO 17 is here
                # This is just a guess, we can check line by line if needed
        except Exception:
            pass

find_chip()
