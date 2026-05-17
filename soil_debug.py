import minimalmodbus
import serial
import time
import sys

# --- CONFIGURATION (Matches ble_server.py) ---
PORT = "/dev/ttyUSB0"
BAUDRATE = 4800
SLAVE_ID = 1

def run_debug():
    print("=" * 50)
    print("STRATA SOIL SENSOR DEBUGGER")
    print("=" * 50)
    print(f"Connecting to: {PORT} @ {BAUDRATE} baud (ID: {SLAVE_ID})")
    
    try:
        # Initialize instrument
        instrument = minimalmodbus.Instrument(PORT, SLAVE_ID)
        instrument.serial.baudrate = BAUDRATE
        instrument.serial.bytesize = 8
        instrument.serial.parity   = serial.PARITY_NONE
        instrument.serial.stopbits = 1
        instrument.serial.timeout  = 1.0
        instrument.mode            = minimalmodbus.MODE_RTU
        
        print("[SUCCESS] Instrument initialised.")
    except Exception as e:
        print(f"[ERROR] Could not initialise sensor: {e}")
        sys.exit(1)

    print("\nStarting continuous read (Press Ctrl+C to stop)...")
    print("-" * 50)
    print(f"{'Metric':<15} | {'Raw Reg':<10} | {'Converted Value'}")
    print("-" * 50)

    try:
        while True:
            try:
                # Read 7 registers starting at 0x0000 (Function code 0x03)
                regs = instrument.read_registers(0, 7, 3)
                
                # Conversions
                moisture   = regs[0] / 10.0
                temp       = regs[1] / 10.0
                ec         = regs[2]
                ph         = regs[3] / 10.0
                nitrogen   = regs[4]
                phosphorus = regs[5]
                potassium  = regs[6]

                # Output Table
                metrics = [
                    ("Moisture",   regs[0], f"{moisture:>5.1f} %"),
                    ("Temperature",regs[1], f"{temp:>5.1f} Â°C"),
                    ("EC Level",   regs[2], f"{ec:>5} ÂµS/cm"),
                    ("pH Level",   regs[3], f"{ph:>5.1f} pH"),
                    ("Nitrogen",   regs[4], f"{nitrogen:>5} mg/kg"),
                    ("Phosphorus", regs[5], f"{phosphorus:>5} mg/kg"),
                    ("Potassium",  regs[6], f"{potassium:>5} mg/kg"),
                ]

                # Move cursor up to overwrite previous data (cleaner terminal)
                # print("\033[H", end="") # Uncomment if you want it to stay at the top of the terminal
                
                print(f"\r--- Sample Time: {time.strftime('%H:%M:%S')} ---")
                for name, raw, conv in metrics:
                    print(f"{name:<15} | {raw:<10} | {conv}")
                print("-" * 50)

                time.sleep(2) # Wait 2 seconds before next poll

            except minimalmodbus.NoResponseError:
                print("\r[ERROR] No response from sensor. Check wiring/power.    ", end="")
            except minimalmodbus.InvalidResponseError:
                print("\r[ERROR] Invalid response (CRC error or noise).         ", end="")
            except Exception as e:
                print(f"\r[ERROR] Unexpected error: {e}")
            
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nDebugger stopped by user.")
        print("=" * 50)

if __name__ == "__main__":
    run_debug()
