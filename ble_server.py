import asyncio
import os
import logging
import subprocess
import signal
import serial
import psutil
import time
import sqlite3
import json
from typing import Any
import numpy as np

# Load ML model bundle
import joblib
MODEL_PATH = '/home/strata/strata/STRATA_FINAL_FINAL3.pkl'
model_bundle = None
try:
    model_bundle = joblib.load(MODEL_PATH)
    logging.info("ML Model bundle loaded successfully.")
except Exception as e:
    logging.error(f"Could not load ML Model bundle: {e}")

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
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True
)

import display
import buzzer
import button

def init_raw_scans_db():
    conn = sqlite3.connect('/home/strata/strata/raw_scans.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS raw_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            crop_name TEXT,
            soil_type TEXT,
            temperature REAL,
            moisture REAL,
            ec REAL,
            ph REAL,
            nitrogen REAL,
            phosphorus REAL,
            potassium REAL
        )
    ''')
    conn.commit()
    conn.close()

def log_strato_scan(data, crop_name="Unknown", soil_type="Unknown"):
    try:
        def get_val(key):
            val = data.get(key, "Err")
            return None if val == "Err" else float(val)

        conn = sqlite3.connect('/home/strata/strata/raw_scans.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO raw_scans (crop_name, soil_type, temperature, moisture, ec, ph, nitrogen, phosphorus, potassium)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (crop_name, soil_type, get_val("temp"), get_val("moisture"), get_val("ec"), 
              get_val("ph"), get_val("nitrogen"), get_val("phosphorus"), get_val("potassium")))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Failed to log strato scan to DB: {e}")

init_raw_scans_db()

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
#   0x0001 - Temperature  (raw / 10  -> Â°C)
#   0x0002 - EC           (raw       -> ÂµS/cm)
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

# ML Diagnostics Output
ML_OUTPUT_UUID     = "56c36f68-da27-464a-952a-9e6631168f6d"

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
        temp       = regs[1] / 10.0   # Â°C
        ec         = regs[2]           # ÂµS/cm
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
        logging.info(f"BLE Write: Characteristic {characteristic.uuid} received value: {value.hex() if isinstance(value, (bytes, bytearray)) else value}")
        characteristic.value = value

        if characteristic.uuid.lower() == SCAN_TRIGGER_UUID.lower():
            if value == b'\x01' or value == bytearray(b'\x01'):
                logging.info("Strata Trigger (0x01) matched! Tasking perform_soil_scan...")
                self.loop.create_task(self.perform_soil_scan())

            elif value == b'\x02' or value == bytearray(b'\x02'):
                logging.info("Strato Trigger (0x02) matched! Tasking perform_strato_scan...")
                self.loop.create_task(self.perform_strato_scan())
            else:
                logging.warning(f"Unknown trigger value received: {value}")


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
        await add_char(ML_OUTPUT_UUID, notify_props, notify_perms)

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

    async def _perform_averaged_scan(self, num_scans: int, interval: float) -> dict:
        """Helper to perform multiple scans and return averaged data."""
        readings = {
            "ph": [], "moisture": [], "temp": [], "ec": [],
            "nitrogen": [], "phosphorus": [], "potassium": []
        }

        for i in range(num_scans):
            start_iter = time.time()
            logging.info(f"Taking scan {i+1}/{num_scans}...")
            
            # Play beep sound as a 'during scan' indicator
            await buzzer.play_scan_beep()
            
            data = read_all_soil_sensors()

            for key in readings.keys():
                if data[key] != "Err":
                    try:
                        readings[key].append(float(data[key]))
                    except (ValueError, TypeError):
                        pass
            
            # Precisely sleep for the remainder of the interval to hit exactly 3s/6s per scan
            elapsed = time.time() - start_iter
            sleep_time = max(0, interval - elapsed)
            await asyncio.sleep(sleep_time)

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
        
        return avg_data

    async def perform_soil_scan(self):
        """Strata Scan: 10 scans in 1 minute."""
        logging.info("Initiating Strata soil scan sequence (10 scans in 60s)...")
        
        # 1. page scanning
        display.show_scanning_page()
        
        # 2. Perform 10 scans with 2s intervals (total ~20s)
        data = await self._perform_averaged_scan(num_scans=10, interval=2.0)
        
        
        # 3. Play the buzzer finish scan melody
        await buzzer.play_finish_scan()
        
        # 4. Run ML model if available
        ml_json = "{}"
        if model_bundle is not None:
            try:
                le_soil = model_bundle['le_soil']
                le_crop = model_bundle['le_crop']
                rfc = model_bundle['model']
                thresholds = model_bundle.get('thresholds', {})
                tier_map = model_bundle.get('tier_map', {1: 'OPTIMAL', 2: 'OPTIMAL', 3: 'HIGHLY SUITABLE', 4: 'SUITABLE', 5: 'SUITABLE'})
                
                # Default "any" encoded value for soil type
                soil_encoded = le_soil.transform(['any'])[0]
                
                # ['n', 'p', 'k', 'temp', 'soil_moisture', 'ph', 'ec', 'soil_type_enc']
                n_val = float(data.get("nitrogen", 0)) if data.get("nitrogen") != "Err" else 0.0
                p_val = float(data.get("phosphorus", 0)) if data.get("phosphorus") != "Err" else 0.0
                k_val = float(data.get("potassium", 0)) if data.get("potassium") != "Err" else 0.0
                t_val = float(data.get("temp", 0)) if data.get("temp") != "Err" else 0.0
                m_val = float(data.get("moisture", 0)) if data.get("moisture") != "Err" else 0.0
                ph_val = float(data.get("ph", 0)) if data.get("ph") != "Err" else 0.0
                ec_val = float(data.get("ec", 0)) if data.get("ec") != "Err" else 0.0
                
                features = [[n_val, p_val, k_val, t_val, m_val, ph_val, ec_val, soil_encoded]]

                # --- Pre-flight checks ---
                # Path A: Empty soil reading — NPK, EC, and Moisture all zero.
                #         Sensor is likely not inserted or chamber is empty.
                #         Skip ML, go straight to rehab with watering guidance.
                empty_reading = (
                    n_val == 0.0 and p_val == 0.0 and k_val == 0.0
                    and ec_val == 0.0 and m_val == 0.0
                )

                # Path B: Any NPK parameter is zero (nutrient gap) → rehab required.
                npk_has_zero = (n_val == 0.0 or p_val == 0.0 or k_val == 0.0)

                # Path C: Moisture is zero → strict rehab regardless of NPK values.
                moisture_zero = (m_val == 0.0)

                # Path D: Hard sensor error — physically impossible values.
                hard_error_reasons = []
                if ph_val < 3.0:
                    hard_error_reasons.append(f"pH={ph_val} (below minimum 3.0 — sensor error)")
                if t_val < 5.0:
                    hard_error_reasons.append(f"Temp={t_val}°C (below minimum 5°C — sensor error)")

                if empty_reading:
                    logging.warning("Empty soil reading: NPK, EC, and Moisture all zero. No rehab shown.")
                    output_dict = {
                        "crops": [],
                        "has_crop_match": False,
                        "soil_type": "any",
                        "ml_flags": ["No nutrients detected"],
                        "ml_subtext": "No soil readings were detected. Make sure the sensor probe is fully inserted into the soil and that the soil chamber contains enough sample before rescanning.",
                        "ml_deficiencies": {},
                        "rehab": []
                    }
                    ml_json = json.dumps(output_dict)
                    logging.info(f"ML Output (empty reading): {ml_json}")
                    self.update_char(SOIL_PH_UUID,        data["ph"])
                    self.update_char(SOIL_MOISTURE_UUID,  data["moisture"])
                    self.update_char(SOIL_TEMP_UUID,      data["temp"])
                    self.update_char(EC_LEVEL_UUID,       data["ec"])
                    self.update_char(NITROGEN_UUID,       data["nitrogen"])
                    self.update_char(PHOSPHORUS_UUID,     data["phosphorus"])
                    self.update_char(POTASSIUM_UUID,      data["potassium"])
                    self.update_char(ML_OUTPUT_UUID,      ml_json)
                    display.show_scan_done_page()
                    return

                if moisture_zero:
                    # Moisture = 0 overrides everything — strictly send to rehab.
                    logging.warning("Moisture is 0. Strict rehab override triggered regardless of NPK values.")
                    rehab_recs = ["WATER"]
                    if npk_has_zero:
                        if n_val == 0.0: rehab_recs.append("FPJ")
                        if p_val == 0.0: rehab_recs.append("CalPhos")
                        if k_val == 0.0: rehab_recs.append("FFJ")
                    output_dict = {
                        "crops": [],
                        "has_crop_match": False,
                        "soil_type": "any",
                        "ml_flags": ["Dry Soil", "Nutrient deficiency detected"] if npk_has_zero else ["Dry Soil"],
                        "ml_subtext": "Moisture level is zero. The soil must be watered before a reliable crop recommendation can be made.",
                        "ml_deficiencies": {
                            k: 0 for k in (["N"] if n_val == 0.0 else []) +
                                          (["P"] if p_val == 0.0 else []) +
                                          (["K"] if k_val == 0.0 else [])
                        },
                        "rehab": list(dict.fromkeys(rehab_recs))  # deduplicate, preserve order
                    }
                    ml_json = json.dumps(output_dict)
                    logging.info(f"ML Output (moisture zero): {ml_json}")
                    self.update_char(SOIL_PH_UUID,        data["ph"])
                    self.update_char(SOIL_MOISTURE_UUID,  data["moisture"])
                    self.update_char(SOIL_TEMP_UUID,      data["temp"])
                    self.update_char(EC_LEVEL_UUID,       data["ec"])
                    self.update_char(NITROGEN_UUID,       data["nitrogen"])
                    self.update_char(PHOSPHORUS_UUID,     data["phosphorus"])
                    self.update_char(POTASSIUM_UUID,      data["potassium"])
                    self.update_char(ML_OUTPUT_UUID,      ml_json)
                    display.show_scan_done_page()
                    return

                if npk_has_zero and not moisture_zero:
                    # At least one NPK is zero — soil has nutrient gap, route to rehab.
                    # Order: IMO → WATER → FPJ / CalPhos / FFJ (per missing nutrient)
                    logging.warning(f"NPK has zero value (N={n_val}, P={p_val}, K={k_val}). Routing to rehab.")
                    npk_flags = {}
                    npk_treatments = []
                    if n_val == 0.0: npk_treatments.append("FPJ");    npk_flags["N"] = 0
                    if p_val == 0.0: npk_treatments.append("CalPhos"); npk_flags["P"] = 0
                    if k_val == 0.0: npk_treatments.append("FFJ");    npk_flags["K"] = 0
                    rehab_recs = ["IMO", "WATER"] + npk_treatments
                    output_dict = {
                        "crops": [],
                        "has_crop_match": False,
                        "soil_type": "any",
                        "ml_flags": ["Nutrient deficiency detected"],
                        "ml_subtext": "One or more NPK values are zero. Soil rehabilitation is required before a reliable crop recommendation can be made.",
                        "ml_deficiencies": npk_flags,
                        "rehab": rehab_recs
                    }
                    ml_json = json.dumps(output_dict)
                    logging.info(f"ML Output (NPK zero): {ml_json}")
                    self.update_char(SOIL_PH_UUID,        data["ph"])
                    self.update_char(SOIL_MOISTURE_UUID,  data["moisture"])
                    self.update_char(SOIL_TEMP_UUID,      data["temp"])
                    self.update_char(EC_LEVEL_UUID,       data["ec"])
                    self.update_char(NITROGEN_UUID,       data["nitrogen"])
                    self.update_char(PHOSPHORUS_UUID,     data["phosphorus"])
                    self.update_char(POTASSIUM_UUID,      data["potassium"])
                    self.update_char(ML_OUTPUT_UUID,      ml_json)
                    display.show_scan_done_page()
                    return

                if hard_error_reasons:
                    logging.warning(f"Hard sensor error: {'; '.join(hard_error_reasons)}. Skipping ML prediction.")
                    output_dict = {
                        "crops": [],
                        "has_crop_match": False,
                        "soil_type": "any",
                        "ml_flags": ["No nutrients detected"],
                        "ml_subtext": "No soil readings were detected. Make sure the sensor probe is fully inserted into the soil and that the soil chamber contains enough sample before rescanning.",
                        "ml_deficiencies": {},
                        "rehab": ["WATER", "IMO"]
                    }
                    ml_json = json.dumps(output_dict)
                    logging.info(f"ML Output (hard sensor error): {ml_json}")
                    self.update_char(SOIL_PH_UUID,        data["ph"])
                    self.update_char(SOIL_MOISTURE_UUID,  data["moisture"])
                    self.update_char(SOIL_TEMP_UUID,      data["temp"])
                    self.update_char(EC_LEVEL_UUID,       data["ec"])
                    self.update_char(NITROGEN_UUID,       data["nitrogen"])
                    self.update_char(PHOSPHORUS_UUID,     data["phosphorus"])
                    self.update_char(POTASSIUM_UUID,      data["potassium"])
                    self.update_char(ML_OUTPUT_UUID,      ml_json)
                    display.show_scan_done_page()
                    return

                # --- Step 1: Predict and rank top 5 crops (no confidence filter — pass all to app) ---
                probas = rfc.predict_proba(features)[0]
                top5_indices = np.argsort(probas)[::-1][:5]
                
                crops = []
                for rank, idx in enumerate(top5_indices, start=1):
                    prob = float(probas[idx])
                    crops.append({
                        "name": str(le_crop.classes_[idx]),
                        "match": round(prob * 100, 2),
                        "tier": tier_map.get(rank, 'SUITABLE')
                    })
                
                # --- Step 2: Crop match is always true if model ran ---
                has_crop_match = len(crops) > 0
                
                # --- Step 3: Always compute soil health flags and rehab recommendations ---
                ml_flags = []
                ml_deficiencies = {}
                rehab = []
                
                if thresholds:
                    if n_val < thresholds.get('n', {}).get('min', 0): ml_deficiencies['N'] = int(thresholds['n']['min'] - n_val)
                    if p_val < thresholds.get('p', {}).get('min', 0): ml_deficiencies['P'] = int(thresholds['p']['min'] - p_val)
                    if k_val < thresholds.get('k', {}).get('min', 0): ml_deficiencies['K'] = int(thresholds['k']['min'] - k_val)
                    
                    ph_opt_low = thresholds.get('ph', {}).get('optimal_low', 4.9)
                    ph_opt_high = thresholds.get('ph', {}).get('optimal_high', 8.0)
                    if ph_val < ph_opt_low: ml_flags.append("High Acidity")
                    elif ph_val > ph_opt_high: ml_flags.append("High Alkalinity")
                    
                    ec_opt_low = thresholds.get('ec', {}).get('optimal_low', 56)
                    ec_opt_high = thresholds.get('ec', {}).get('optimal_high', 255)
                    if ec_val < ec_opt_low: ml_flags.append("Low EC")
                    elif ec_val > ec_opt_high: ml_flags.append("High EC")
                    
                    m_opt_low = thresholds.get('soil_moisture', {}).get('optimal_low', 15.19)
                    m_opt_high = thresholds.get('soil_moisture', {}).get('optimal_high', 57.55)
                    if m_val < m_opt_low: ml_flags.append("Dry Soil")
                    elif m_val > m_opt_high: ml_flags.append("Waterlogged Soil")
                    
                    if 'N' in ml_deficiencies: rehab.append("FPJ")
                    if 'P' in ml_deficiencies: rehab.append("CalPhos")
                    if 'K' in ml_deficiencies: rehab.append("FFJ")
                    if any(f in ml_flags for f in ("High Acidity", "High Alkalinity", "Low EC", "High EC")): rehab.append("LABS")
                    if "Dry Soil" in ml_flags: rehab.append("WATER")
                    if not rehab: rehab.append("IMO")
                
                output_dict = {
                    "crops": crops,
                    "has_crop_match": has_crop_match,
                    "soil_type": "any",
                    "ml_flags": ml_flags,
                    "ml_deficiencies": ml_deficiencies,
                    "rehab": rehab
                }
                
                ml_json = json.dumps(output_dict)
                logging.info(f"ML Output generated: {ml_json}")
            except Exception as e:
                logging.error(f"ML Prediction failed: {e}")
        
        # 5. Update BLE characteristics
        self.update_char(SOIL_PH_UUID,        data["ph"])
        self.update_char(SOIL_MOISTURE_UUID,  data["moisture"])
        self.update_char(SOIL_TEMP_UUID,      data["temp"])
        self.update_char(EC_LEVEL_UUID,       data["ec"])
        self.update_char(NITROGEN_UUID,       data["nitrogen"])
        self.update_char(PHOSPHORUS_UUID,     data["phosphorus"])
        self.update_char(POTASSIUM_UUID,      data["potassium"])
        self.update_char(ML_OUTPUT_UUID,      ml_json)
        
        logging.info(f"Strata scan complete. Results: {data}")
        
        # 6. Finally, show the results page on the LCD
        display.show_scan_done_page()

    async def perform_strato_scan(self):
        """Strato Scan: 10 scans, 1 every 2 seconds, no averaging."""
        logging.info("Initiating Strato scan sequence (10 scans, 2s interval)...")
        
        # Show status bar feedback
        display.show_gathering_data()

        # 1. Perform 10 scans with 2s intervals
        for i in range(10):
            start_iter = time.time()
            logging.info(f"Taking Strato scan {i+1}/10...")
            
            # Play beep sound as a 'during scan' indicator
            await buzzer.play_scan_beep()
            
            data = read_all_soil_sensors()
            
            # Update BLE characteristics immediately for each scan
            self.update_char(SOIL_PH_UUID,       data["ph"])
            self.update_char(SOIL_MOISTURE_UUID,  data["moisture"])
            self.update_char(SOIL_TEMP_UUID,      data["temp"])
            self.update_char(EC_LEVEL_UUID,       data["ec"])
            self.update_char(NITROGEN_UUID,       data["nitrogen"])
            self.update_char(PHOSPHORUS_UUID,     data["phosphorus"])
            self.update_char(POTASSIUM_UUID,      data["potassium"])

            # Log this individual scan to the SQLite database
            log_strato_scan(data)

            logging.info(f"Strato scan {i+1} updated: {data}")

            # Wait for remainder of 2 second interval
            elapsed = time.time() - start_iter
            sleep_time = max(0, 2.0 - elapsed)
            await asyncio.sleep(sleep_time)

        logging.info("Strato scan complete.")
        
        # 3. 3 fast beeps for finish
        await buzzer.play_finish_scan()

        # Reset the status bar
        is_connected = check_any_ble_connected()
        display._last_bt_status = None 
        display.update_bluetooth_status(is_connected)

    async def perform_offline_scan(self):
        """Offline Scan: Triggered by physical GPIO button."""
        logging.info("Initiating Offline Scan (Physical Button)...")
        
        # 1. UI feedback
        display.show_scanning_page()
        
        # 2. Perform 10 scans (2s interval)
        data = await self._perform_averaged_scan(num_scans=10, interval=2.0)
        
        # 3. Predict health using ML — same pre-flight logic as perform_soil_scan
        is_healthy = False
        if model_bundle is not None:
            try:
                le_soil = model_bundle['le_soil']
                rfc = model_bundle['model']

                soil_encoded = le_soil.transform(['any'])[0]
                n_val  = float(data.get("nitrogen",   0)) if data.get("nitrogen")   != "Err" else 0.0
                p_val  = float(data.get("phosphorus", 0)) if data.get("phosphorus") != "Err" else 0.0
                k_val  = float(data.get("potassium",  0)) if data.get("potassium")  != "Err" else 0.0
                t_val  = float(data.get("temp",       0)) if data.get("temp")       != "Err" else 0.0
                m_val  = float(data.get("moisture",   0)) if data.get("moisture")   != "Err" else 0.0
                ph_val = float(data.get("ph",         0)) if data.get("ph")         != "Err" else 0.0
                ec_val = float(data.get("ec",         0)) if data.get("ec")         != "Err" else 0.0

                # Path A — empty reading: NPK + EC + Moisture all zero → sensor not inserted
                empty_reading = (n_val == 0.0 and p_val == 0.0 and k_val == 0.0
                                 and ec_val == 0.0 and m_val == 0.0)

                # Path B — any NPK is zero → nutrient gap, unhealthy
                npk_has_zero = (n_val == 0.0 or p_val == 0.0 or k_val == 0.0)

                # Path C — moisture is zero → strictly unhealthy regardless of NPK
                moisture_zero = (m_val == 0.0)

                # Path D — hard sensor error: physically impossible values
                hard_error = ph_val < 3.0 or t_val < 5.0

                if empty_reading or hard_error or moisture_zero or npk_has_zero:
                    logging.warning(f"Offline scan: soil not viable (empty={empty_reading}, hard_error={hard_error}, moisture_zero={moisture_zero}, npk_has_zero={npk_has_zero}). Marking as unhealthy.")
                    is_healthy = False
                else:
                    features = [[n_val, p_val, k_val, t_val, m_val, ph_val, ec_val, soil_encoded]]
                    probas = rfc.predict_proba(features)[0]
                    top5 = np.argsort(probas)[::-1][:5]
                    # Healthy = model ran and top crop has any probability
                    is_healthy = len(top5) > 0 and float(probas[top5[0]]) > 0.0
            except Exception as e:
                logging.error(f"Offline ML Prediction failed: {e}")
        
        # 4. Play success beep
        await buzzer.play_finish_scan()
        
        # 5. Log to DB
        log_strato_scan(data, crop_name="Offline", soil_type="any")
        
        # 6. Display raw results
        display.show_raw_scan_results(data, is_healthy)
        logging.info(f"Offline scan complete. Results: {data}, Healthy: {is_healthy}")

    async def monitor_physical_button(self):
        logging.info("Starting physical button monitor (GPIO11) with long-press calibration recovery")
        while True:
            try:
                duration = await button.wait_for_press()
                if duration > 0.05:
                    if duration >= 8.0:
                        logging.info(f"Button held for {duration:.1f}s. Triggering screen touch calibration!")
                        await buzzer.play_error()
                        display.trigger_touch_calibration()
                    else:
                        logging.info(f"Button held for {duration:.1f}s. Triggering offline scan.")
                        # Only start scan if one isn't currently running
                        await self.perform_offline_scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error checking physical button: {e}")
                await asyncio.sleep(1)

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

    diag_task     = loop.create_task(server.periodic_system_update())
    shutdown_task = loop.create_task(server.monitor_display_commands())
    button_task   = loop.create_task(server.monitor_physical_button())

    # --- SIGTERM handler (covers J2 jumper, `sudo shutdown`, `systemctl stop`, etc.) ---
    def _on_sigterm():
        logging.info("SIGTERM received (external shutdown â€“ e.g. J2 jumper). Updating display.")
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
        button_task.cancel()
        await server.cleanup()
        buzzer.cleanup()
        logging.info("Server shutdown completed seamlessly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt triggered. Exiting application.")
