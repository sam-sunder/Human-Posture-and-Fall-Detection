# ESP32 Vitals Telemetry Node (MAX30102 + DS18B20)

This firmware connects an ESP32 micro-controller with a **MAX30102** (Pulse Oximeter & Heart Rate) and a **DS18B20** (Dallas 1-Wire Digital Body Temperature) sensor to the Human Posture and Fall Detection clinical dashboard.

---

## 1. Hardware Pinout & Wiring

### ESP32 DevKit V1 Pin Mapping

| Sensor | Sensor Pin | ESP32 GPIO | Description / Notes |
| :--- | :--- | :--- | :--- |
| **MAX30102** | `VIN` / `VCC` | `3.3V` | Powers the optical IC (Do NOT use 5V on 3.3V boards) |
| **MAX30102** | `GND` | `GND` | Common Ground |
| **MAX30102** | `SDA` | `GPIO 21` | Standard ESP32 I2C Data |
| **MAX30102** | `SCL` | `GPIO 22` | Standard ESP32 I2C Clock |
| **MAX30102** | `INT` | Not connected | (Optional interrupt, not required) |
| **DS18B20** | `VDD` / `VCC` | `3.3V` or `5V` | Sensor power |
| **DS18B20** | `GND` | `GND` | Common Ground |
| **DS18B20** | `DATA` | `GPIO 4` | **Requires 4.7kΩ pull-up resistor to 3.3V** |
| **Status LED** | Onboard | `GPIO 2` | Flashes during boot and heartbeat pulses |

> [!IMPORTANT]
> The DS18B20 requires a **4.7kΩ pull-up resistor** connected between the `DATA` line (GPIO 4) and `3.3V`. Without this resistor, the sensor will read `DEVICE_DISCONNECTED_C` (-127°C).

---

## 2. Required Arduino IDE Libraries

Install these four libraries via **Arduino IDE Library Manager** (`Ctrl+Shift+I` or `Cmd+Shift+I`):

1. **`SparkFun MAX3010x Pulse and Proximity Sensor Library`** by SparkFun Electronics
2. **`DallasTemperature`** by Miles Burton
3. **`OneWire`** by Paul Stoffregen
4. **`ArduinoJson`** by Benoit Blanchon (Version 6.x or 7.x)

---

## 3. Configuration

Open `esp32_vitals.ino` and update the parameters at the top:

```cpp
// Wi-Fi credentials
const char* WIFI_SSID     = "Your_WiFi_Name";
const char* WIFI_PASSWORD = "Your_WiFi_Password";

// Server host running the FastAPI app
const char* SERVER_HOST   = "192.168.1.100"; // IP address of your host PC
const int   SERVER_PORT   = 8000;

// Hardware Node Identification
const char* DEVICE_ID     = "ESP32-VITALS-01";
const char* CLIENT_ID     = "bed_01"; // Matches patient client_id in dashboard
```

---

## 4. Telemetry Endpoints Contacted

- **Device Announcement:** `POST /api/devices/announce`
  - Sends device ID, client ID, IP address, MAC address, firmware version, and sensor list on startup and every 60s.
- **Vitals Ingestion:** `POST /api/vitals`
  - Sends heart rate (BPM), SpO2 (%), body temperature (°C/°F), and finger presence every 1.5s.
