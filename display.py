import serial
import logging

try:
    ser = serial.Serial('/dev/ttyAMA0', 9600, timeout=1)
except Exception as e:
    logging.error(f"Could not open serial port: {e}")
    ser = None

def send_nextion_command(command):
    if ser is not None:
        try:
            logging.info(f"Sending Nextion command: {command}")
            ser.write(command.encode('iso-8859-1'))
            ser.write(b'\xff\xff\xff')
            ser.flush()
        except Exception as e:
            logging.error(f"Nextion write error: {e}")

def initialize_display():
    # Wake up the display and set full brightness in case it was sleeping
    send_nextion_command('sleep=0')
    send_nextion_command('dim=100')
    
    # Force go to main page
    send_nextion_command('page page0')
    import time
    time.sleep(0.1)
    
    # Set default stats placeholders
    send_nextion_command('t0.txt="---"')
    send_nextion_command('t1.txt="---"')
    send_nextion_command('t2.txt="---"')
    
    # Set server status to Initializing using the offline color
    send_nextion_command('b0.bco=10565')
    send_nextion_command('t3.bco=10565')
    send_nextion_command('t3.txt="Initializing"')

def update_system_stats(battery, cpu_temp, cpu_load):
    send_nextion_command(f't0.txt="{battery}%"')
    send_nextion_command(f't1.txt="{cpu_temp}°C"')
    send_nextion_command(f't2.txt="{cpu_load}%"')

_last_bt_status = None
_resend_counter = 0

_last_bt_status = None

def update_bluetooth_status(is_client_connected):
    global _last_bt_status
    
    # Do absolutely nothing if the connection state has not changed
    if is_client_connected == _last_bt_status:
        return
        
    _last_bt_status = is_client_connected
    
    if is_client_connected:
        send_nextion_command('b0.bco=11910')
        send_nextion_command('t3.txt="App Connected"')
        send_nextion_command('t3.bco=11910')
        send_nextion_command('t3.pco=65535') # White text
    else:
        send_nextion_command('b0.bco=10565')
        send_nextion_command('t3.txt="Waiting for App"')
        send_nextion_command('t3.bco=10565')
        send_nextion_command('t3.pco=65535') # White text

def show_gathering_data():
    # Dark Purple Background: 25616, Yellow Text: 65504
    send_nextion_command('b0.bco=25616')
    send_nextion_command('t3.bco=25616')
    send_nextion_command('t3.pco=65504')
    send_nextion_command('t3.txt="Gathering Data..."')

def check_for_commands():
    if ser is not None and ser.in_waiting > 0:
        try:
            # Read all available bytes from the buffer immediately without blocking
            raw_bytes = ser.read(ser.in_waiting)
            if raw_bytes:
                logging.info(f"RAW BYTES FROM SCREEN (HEX): {raw_bytes.hex()}")
                incoming_data = raw_bytes.decode('utf-8', errors='ignore').strip()
                if incoming_data:
                    logging.info(f"RAW DATA FROM SCREEN: '{incoming_data}'")
                return incoming_data
        except Exception as e:
            logging.error(f"Serial read error: {e}")
    return None
def show_scanning_page():
    send_nextion_command('page scanning')

def show_main_page():
    send_nextion_command('page page0')
    import time
    time.sleep(0.1)
    global _last_bt_status, _resend_counter
    _last_bt_status = None
    _resend_counter = 0
    update_bluetooth_status(True)

def show_scan_done_page():
    send_nextion_command('page scan_done')

def show_shutdown_page():
    # Switch to the shutdown page mentioned by the user
    send_nextion_command('page sd_2')
    # Optional: Turn off the backlight after a short delay to "turn off" the screen
    # send_nextion_command('dim=0') 

def show_raw_scan_results(data, is_healthy):
    send_nextion_command('page raw_scan')
    
    import time
    time.sleep(0.1)
    
    # Update parameters
    send_nextion_command(f'n.txt="{data.get("nitrogen", 0)}"')
    send_nextion_command(f'p.txt="{data.get("phosphorus", 0)}"')
    send_nextion_command(f'k.txt="{data.get("potassium", 0)}"')
    send_nextion_command(f'm.txt="{data.get("moisture", 0)}%"')
    send_nextion_command(f'ph.txt="{data.get("ph", 0)}"')
    send_nextion_command(f't.txt="{data.get("temp", 0)}"')
    send_nextion_command(f'ec.txt="{data.get("ec", 0)}"')
    
    # Update status banner (bco 2016 = green, 63488 = red)
    if is_healthy:
        send_nextion_command('status.txt="Healthy"')
        send_nextion_command('status.bco=2016')
    else:
        send_nextion_command('status.txt="Unhealthy"')
        send_nextion_command('status.bco=63488')

def trigger_touch_calibration():
    send_nextion_command('touch_j')

initialize_display()
