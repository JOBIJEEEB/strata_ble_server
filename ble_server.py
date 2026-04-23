import asyncio
import os
import logging
import subprocess
import signal
import serial
import psutil
from typing import Any

from bless import (  # type: ignore
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions
)
import smbus2
import minimalmodbus

# Configure logging at the very top before other local imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

import display
import buzzer

# Initialize I2C bus for Waveshare UPS HAT (E)
UPS_BUS = None
try:
    UPS_BUS = smbus2.SMBus(1)
except Exception as e:
    logging.warning(f"Could not initialize smbus2 for UPS HAT: {e}")

# ---------------------------------------------------------------------------
# RS485 SOIL SENSOR (Modbus RTU, ttyUSB0, 4800 baud, slave addr 0x01)
# Register map (7-in-1 JXCT-compatible):
#   0x0000 - Moisture     (raw / 10  -> %)
#   0x0001 - Temperature  (raw / 10  -> °C)
#   0x0002 - EC           (raw       -> µS/cm)
#   0x0003 - pH           (raw / 10  -> pH)
#   0x0004 - Nitrogen     (raw       -> mg/kg)
#   0x0005 - Phosphorus   (raw       -> mg/kg)
#   0x0006 - Potassium    (raw       -> mg/kg)
# ---------------------------------------------------------------------------
SOIL_PORT      = "/dev/ttyUSB0"
SOIL_BAUDRATE  = 4800
SOIL_SLAVE_ID  = 1

SOIL_INSTRUMENT = None
try:
    _inst = minimalmodbus.Instrument(SOIL_PORT, SOIL_SLAVE_ID)
    _inst.serial.baudrate = SOIL_BAUDRATE
    _inst.serial.bytesize = 8
    _inst.serial.parity   = serial.PARITY_NONE
    _inst.serial.stopbits = 1
    _inst.serial.timeout  = 1.0
    _inst.mode            = minimalmodbus.MODE_RTU
    SOIL_INSTRUMENT = _inst
    logging.info(f"Soil sensor initialised on {SOIL_PORT} @ {SOIL_BAUDRATE} baud")
except Exception as e:
    logging.warning(f"Could not initialise soil sensor on {SOIL_PORT}: {e}")

# ---------------------------------------------------------------------------
# BLE SERVICE & CHARACTERISTIC UUIDS
# ---------------------------------------------------------------------------
SERVICE_NAME = "Strata"
SERVICE_UUID = "56c36f56-da27-464a-952a-9e6631168f6d"

# Pi Diagnostics (Update every 5s)
CPU_USAGE_UUID     = "56c36f57-da27-464a-952a-9e6631168f6d"
CPU_TEMP_UUID      = "56c36f58-da27-464a-952a-9e6631168f6d"
BATTERY_UUID       = "56c36f59-da27-464a-952a-9e6631168f6d"

# Soil Sensor Readings
SOIL_PH_UUID       = "56c36f60-da27-464a-952a-9e6631168f6d"
SOIL_MOISTURE_UUID = "56c36f61-da27-464a-952a-9e6631168f6d"
SOIL_TEMP_UUID     = "56c36f62-da27-464a-952a-9e6631168f6d"
EC_LEVEL_UUID      = "56c36f63-da27-464a-952a-9e6631168f6d"
NITROGEN_UUID      = "56c36f64-da27-464a-952a-9e6631168f6d"
PHOSPHORUS_UUID    = "56c36f65-da27-464a-952a-9e6631168f6d"
POTASSIUM_UUID     = "56c36f66-da27-464a-952a-9e6631168f6d"

# Scan Control
SCAN_TRIGGER_UUID  = "56c36f67-da27-464a-952a-9e6631168f6d"


# ---------------------------------------------------------------------------
# SENSOR READING FUNCTIONS (WITH MOCKS)
# ---------------------------------------------------------------------------
def get_cpu_usage() -> str:
    try:
        return f"{psutil.cpu_percent(interval=None):.1f}"
    except Exception as e:
        logging.error(f"Failed to read CPU usage: {e}")
        return "Err"

def get_cpu_temp() -> str:
    try:
        output = subprocess.check_output(["vcgencmd", "measure_temp"]).decode("utf-8")
        temp = output.replace("temp=", "").replace("'C", "").strip()
        return temp
    except Exception as e:
        logging.error(f"Failed to read CPU temp: {e}")
        return "Err"

def get_battery() -> str:
    try:
        if UPS_BUS is not None:
            data = UPS_BUS.read_i2c_block_data(0x2d, 0x20, 12)
            percent = data[4] | (data[5] << 8)
            return str(min(100, percent))
        return "Err"
    except Exception as e:
        logging.error(f"Failed to read battery via I2C: {e}")
        return "Err"

def read_all_soil_sensors() -> dict:
    """Read all 7 parameters from the RS485 Modbus soil sensor."""
    err_result = {k: "Err" for k in ["ph", "moisture", "temp", "ec", "nitrogen", "phosphorus", "potassium"]}

    if SOIL_INSTRUMENT is None:
        logging.error("Soil sensor instrument not initialised.")
        return err_result

    try:
        # Read 7 holding registers starting at address 0 (function code 0x03)
        regs = SOIL_INSTRUMENT.read_registers(0, 7, 3)

        moisture   = regs[0] / 10.0   # %
        temp       = regs[1] / 10.0   # °C
        ec         = regs[2]           # µS/cm
        ph         = regs[3] / 10.0   # pH
        nitrogen   = regs[4]           # mg/kg
        phosphorus = regs[5]           # mg/kg
        potassium  = regs[6]           # mg/kg

        data = {
            "moisture":   f"{moisture:.1f}",
            "temp":       f"{temp:.1f}",
            "ec":         str(ec),
            "ph":         f"{ph:.1f}",
            "nitrogen":   str(nitrogen),
            "phosphorus": str(phosphorus),
            "potassium":  str(potassium),
        }
        logging.info(f"Soil sensor read OK: {data}")
        return data

    except minimalmodbus.NoResponseError:
        logging.error("Soil sensor: no response (check wiring / power).")
        return err_result
    except Exception as e:
        logging.error(f"Failed to read soil sensors: {e}")
        return err_result


def check_any_ble_connected() -> bool:
    try:
        out = subprocess.check_output(["hcitool", "con"], stderr=subprocess.DEVNULL).decode("utf-8")
        return "> LE" in out or "> ACL" in out
    except Exception:
        return False

# ---------------------------------------------------------------------------
# BLE SERVER CLASS
# ---------------------------------------------------------------------------
class StrataBLEServer:
    def __init__(self, loop):
        self.loop = loop
        self.server = BlessServer(name=SERVICE_NAME, loop=loop)
        self.server.read_request_func = self.on_read_request
        self.server.write_request_func = self.on_write_request

    def on_read_request(self, characteristic: BlessGATTCharacteristic, **kwargs) -> bytearray:
        logging.debug(f"Read request for {characteristic.uuid}")
        return characteristic.value

    def on_write_request(self, characteristic: BlessGATTCharacteristic, value: Any, **kwargs):
        logging.debug(f"Write request for {characteristic.uuid} with value: {value}")
        characteristic.value = value

        if characteristic.uuid.lower() == SCAN_TRIGGER_UUID.lower():
            if value == b'\x01' or value == bytearray(b'\x01'):
                logging.info("Scan Trigger sequence (0x01) received! Initiating soil scan.")
                self.loop.create_task(self.perform_soil_scan())

            elif value == b'\x02' or value == bytearray(b'\x02'):
                logging.info("Strato Scan Trigger (0x02) received! Initiating 10-second extended scan.")
                self.loop.create_task(self.perform_strato_scan())

    async def setup(self):
        logging.info("Setting up BLE Server and initializing characteristics...")
        await self.server.add_new_service(SERVICE_UUID)
        subprocess.run(["bluetoothctl", "pairable", "off"], check=False)
        display.update_bluetooth_status(False)

        async def add_char(uuid, props, perms):
            await self.server.add_new_characteristic(
                SERVICE_UUID,
                uuid,
                props,
                b"0", 
                perms
            )

        notify_props = GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify
        notify_perms = GATTAttributePermissions.readable
        
        await add_char(CPU_USAGE_UUID, notify_props, notify_perms)
        await add_char(CPU_TEMP_UUID, notify_props, notify_perms)
        await add_char(BATTERY_UUID, notify_props, notify_perms)

        await add_char(SOIL_PH_UUID, notify_props, notify_perms)
        await add_char(SOIL_MOISTURE_UUID, notify_props, notify_perms)
        await add_char(SOIL_TEMP_UUID, notify_props, notify_perms)
        await add_char(EC_LEVEL_UUID, notify_props, notify_perms)
        await add_char(NITROGEN_UUID, notify_props, notify_perms)
        await add_char(PHOSPHORUS_UUID, notify_props, notify_perms)
        await add_char(POTASSIUM_UUID, notify_props, notify_perms)

        trigger_props = GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response
        trigger_perms = GATTAttributePermissions.writeable
        await add_char(SCAN_TRIGGER_UUID, trigger_props, trigger_perms)

        logging.info("Starting BLE Server advertising now...")
        await self.server.start()
        logging.info(f"Service '{SERVICE_NAME}' is now successfully advertising {SERVICE_UUID}")

    def update_char(self, uuid: str, val_str: str):
        try:
            val_bytes = val_str.encode("utf-8")
            self.server.get_characteristic(uuid).value = val_bytes
            self.server.update_value(SERVICE_UUID, uuid)
        except Exception as e:
            logging.error(f"Failed to update characteristic {uuid} with value '{val_str}': {e}")

    async def perform_soil_scan(self):
        logging.info("Initiating soil scan sequence...")
        
        # 0. beep play_start_scan
        await buzzer.play_start_scan()
        
        # 1. page scanning
        display.show_scanning_page()
        
        # Wait a moment for the scanning animation/page to be visible
        await asyncio.sleep(2)
        
        logging.info("Reading soil sensors complete. Playing finish melody...")
        data = read_all_soil_sensors()
        
        # 1. Play the buzzer finish scan melody first (awaiting it to finish)
        await buzzer.play_finish_scan()
        
        # 2. Update BLE characteristics (notifies the app to pull results)
        self.update_char(SOIL_PH_UUID, data["ph"])
        self.update_char(SOIL_MOISTURE_UUID, data["moisture"])
        self.update_char(SOIL_TEMP_UUID, data["temp"])
        self.update_char(EC_LEVEL_UUID, data["ec"])
        self.update_char(NITROGEN_UUID, data["nitrogen"])
        self.update_char(PHOSPHORUS_UUID, data["phosphorus"])
        self.update_char(POTASSIUM_UUID, data["potassium"])
        
        logging.info(f"Soil metrics scan complete. Results: {data}")
        
        # 3. Finally, show the results page on the LCD
        display.show_scan_done_page()

    async def perform_strato_scan(self):
        logging.info("Initiating 10-second Strato scan sequence...")
        
        # 0. beep play_start_scan
        await buzzer.play_start_scan()
        
        # Show status bar feedback instead of a full page change
        display.show_gathering_data()

        # 1. Prepare lists to hold 10 seconds worth of metrics
        readings = {"ph": [], "moisture": [], "temp": [], "ec": [], "nitrogen": [], "phosphorus": [], "potassium": []}

        # 2. Start the R2-D2 chatter in the background
        chatter_task = asyncio.create_task(buzzer.play_r2d2_chatter(duration=10))

        # 3. Loop precisely 10 times, taking 1 second interval pauses
        for i in range(10):
            data = read_all_soil_sensors()
            for key in readings.keys():
                if data[key] != "Err":
                    readings[key].append(float(data[key]))
            await asyncio.sleep(1.0)

        # 3. Calculate the averages of successfully captured data points
        avg_data = {}
        for key, vals in readings.items():
            if vals:
                avg = sum(vals) / len(vals)
                if key in ["moisture", "temp", "ph"]:
                    avg_data[key] = f"{avg:.1f}"
                else:
                    avg_data[key] = str(int(round(avg)))
            else:
                avg_data[key] = "Err"

        # 4. Blast the final averages back up to the Bluetooth characteristics
        self.update_char(SOIL_PH_UUID,       avg_data["ph"])
        self.update_char(SOIL_MOISTURE_UUID,  avg_data["moisture"])
        self.update_char(SOIL_TEMP_UUID,      avg_data["temp"])
        self.update_char(EC_LEVEL_UUID,       avg_data["ec"])
        self.update_char(NITROGEN_UUID,       avg_data["nitrogen"])
        self.update_char(PHOSPHORUS_UUID,     avg_data["phosphorus"])
        self.update_char(POTASSIUM_UUID,      avg_data["potassium"])

        logging.info(f"Strato 10s scan complete. Averages: {avg_data}")
        
        await chatter_task
        
        # 2. beep play_finish_scan (Wait for beep to finish)
        await buzzer.play_finish_scan()
        
        # No page change for Strato scan done, just keep dashboard visible
        
        # Reset the status bar back to connected/waiting status (this will take effect when returning to main page)
        is_connected = check_any_ble_connected()
        display._last_bt_status = None # Force update
        display.update_bluetooth_status(is_connected)

    async def periodic_system_update(self):
        logging.info("Starting periodic diagnostic updates (every 5 seconds)")
        while True:
            try:
                cpu_u = get_cpu_usage()
                cpu_t = get_cpu_temp()
                batt = get_battery()
                
                self.update_char(CPU_USAGE_UUID, cpu_u)
                self.update_char(CPU_TEMP_UUID, cpu_t)
                self.update_char(BATTERY_UUID, batt)
                
                # Update connection status based on active clients directly bypassing bless's subscription limit checks
                is_connected = check_any_ble_connected()
                display.update_bluetooth_status(is_connected)
                
                # Push the live stats to the Nextion display
                display.update_system_stats(batt, cpu_t, cpu_u)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error encountered during periodic_system_update: {e}")
            
            await asyncio.sleep(2)

    async def monitor_display_commands(self):
        logging.info("Starting display command monitor")
        while True:
            try:
                command = display.check_for_commands()
                
                if command:
                    if "SHUTDOWN" in command:
                        logging.info("Shutdown command received from screen. Powering off Strata in 5 seconds.")
                        display.show_shutdown_page()
                        
                        # Give the user time to see the shutdown page
                        await asyncio.sleep(5)
                        
                        display.send_nextion_command('sleep=1') # Turn off the LCD backlight/sleep
                        os.system("sudo shutdown -h now")
                        
                    elif "SCANDONE" in command:
                        logging.info("Done button pressed. Returning to main dashboard.")
                        display.show_main_page()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error checking display commands: {e}")
            
            await asyncio.sleep(0.5)

    async def cleanup(self):
        logging.info("Stopping BLE server services gracefully...")
        try:
            await self.server.stop()
        except TypeError:
            self.server.stop()
        except Exception as e:
            logging.error(f"Error while stopping server: {e}")


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------
async def main():
    # Set high process priority to ensure smooth buzzer and serial performance
    try:
        os.nice(-10)
        logging.info("Set process priority to -10")
    except Exception as e:
        logging.warning(f"Could not set process priority: {e}")

    loop = asyncio.get_running_loop()

    server = StrataBLEServer(loop)

    await server.setup()

    diag_task = loop.create_task(server.periodic_system_update())
    shutdown_task = loop.create_task(server.monitor_display_commands())

    # --- SIGTERM handler (covers J2 jumper, `sudo shutdown`, `systemctl stop`, etc.) ---
    def _on_sigterm():
        logging.info("SIGTERM received (external shutdown – e.g. J2 jumper). Updating display.")
        display.show_shutdown_page()
        display.send_nextion_command('sleep=1')
        # Cancel the main wait so the finally block runs and cleans up
        for task in asyncio.all_tasks(loop):
            task.cancel()

    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    # ---------------------------------------------------------------------------------

    logging.info("Strata BLE Server is active. Press Ctrl+C to terminate.")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logging.info("Main loop cancelled gracefully.")
    finally:
        diag_task.cancel()
        shutdown_task.cancel()
        await server.cleanup()
        buzzer.cleanup()
        logging.info("Server shutdown completed seamlessly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt triggered. Exiting application.")