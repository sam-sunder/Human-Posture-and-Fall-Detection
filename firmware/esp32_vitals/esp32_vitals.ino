/*
 * ESP32 Patient Vitals Node
 * Sensors:
 *   - MAX30102: Optical Pulse Oximeter & Heart Rate Sensor (I2C)
 *   - DS18B20: High-Precision 1-Wire Digital Body Temperature Sensor
 *
 * Capabilities:
 *   - Device Auto-Announcement to central FastAPI server (/api/devices/announce)
 *   - Periodic telemetry streaming via HTTP JSON (/api/vitals)
 *   - Finger presence detection & signal conditioning
 *   - Real-time heartbeat detection and SpO2 computation
 *   - Periodic health check & automatic Wi-Fi reconnection
 *
 * Pin Connections:
 *   ESP32 DevKit V1:
 *     - MAX30102:
 *         VIN  -> 3.3V
 *         GND  -> GND
 *         SDA  -> GPIO 21
 *         SCL  -> GPIO 22
 *     - DS18B20:
 *         VDD  -> 3.3V (or 5V)
 *         GND  -> GND
 *         DATA -> GPIO 4 (with 4.7k ohm pull-up resistor to 3.3V)
 *     - Status LED:
 *         Built-in LED -> GPIO 2
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <ArduinoJson.h>
#include "MAX30105.h"
#include "heartRate.h"

// ==========================================
// CONFIGURATION: Network & Server Settings
// ==========================================
const char* WIFI_SSID     = "vivo T3 5G";
const char* WIFI_PASSWORD = "sam12345";

// Server IP address and port where the FastAPI backend is running
const char* SERVER_HOST   = "10.129.97.210"; 
const int   SERVER_PORT   = 8000;

// Hardware Identity
const char* DEVICE_ID     = "ESP32-VITALS-01";
const char* CLIENT_ID     = "bed_01"; // Associates with the patient's camera/client ID
const char* FIRMWARE_VER  = "1.0.0";
const char* SENSOR_TAGS   = "MAX30102,DS18B20";

// Pins
#define ONE_WIRE_BUS 4    // DS18B20 Data pin
#define I2C_SDA_PIN  21   // MAX30102 SDA
#define I2C_SCL_PIN  22   // MAX30102 SCL
#define STATUS_LED_PIN 2  // Onboard Blue LED

// Telemetry intervals (in milliseconds)
const unsigned long TELEMETRY_INTERVAL_MS = 1500;  // Push vitals every 1.5s
const unsigned long ANNOUNCE_INTERVAL_MS  = 60000; // Heartbeat announcement every 60s
const unsigned long WIFI_RETRY_INTERVAL_MS = 5000; // Wi-Fi reconnect attempt interval

// ==========================================
// Global Sensor Instances & State Variables
// ==========================================
MAX30105 max30102;
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature tempSensors(&oneWire);

// Heart Rate calculation state
const byte RATE_SIZE = 4; // Moving average buffer size
byte rates[RATE_SIZE];
byte rateSpot = 0;
long lastBeat = 0; // Time in millis when last beat occurred
float beatsPerMinute = 0.0;
int beatAvg = 0;

// SpO2 calculation variables (AC/DC ratio estimation)
long irBuffer[50];
long redBuffer[50];
int sampleIndex = 0;
float calculatedSpO2 = 98.0;

// Temperature state
float lastTempC = 36.5;
float lastTempF = 97.7;
bool hasValidTemp = false;

// Timing counters
unsigned long lastTelemetryTime = 0;
unsigned long lastAnnounceTime = 0;
unsigned long lastWifiCheck = 0;

// Hardware health
bool max30102Detected = false;
bool ds18b20Detected = false;

// ==========================================
// Helper: Wi-Fi Setup and Reconnect
// ==========================================
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.println();
  Serial.print("[WiFi] Connecting to: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 25) {
    delay(400);
    digitalWrite(STATUS_LED_PIN, !digitalRead(STATUS_LED_PIN)); // Flash LED
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    digitalWrite(STATUS_LED_PIN, HIGH);
    Serial.println("\n[WiFi] Connected successfully!");
    Serial.print("[WiFi] IP Address:  ");
    Serial.println(WiFi.localIP());
    Serial.print("[WiFi] MAC Address: ");
    Serial.println(WiFi.macAddress());
  } else {
    digitalWrite(STATUS_LED_PIN, LOW);
    Serial.println("\n[WiFi] Connection timeout. Running in offline/sensing mode.");
  }
}

// ==========================================
// Sensor Announcement: POST /api/devices/announce
// ==========================================
bool announceDeviceToServer() {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  String url = "http://" + String(SERVER_HOST) + ":" + String(SERVER_PORT) + "/api/devices/announce";
  
  Serial.print("[Announce] Contacting server: ");
  Serial.println(url);

  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(4000);

  StaticJsonDocument<256> doc;
  doc["device_id"]        = DEVICE_ID;
  doc["client_id"]        = CLIENT_ID;
  doc["ip_address"]       = WiFi.localIP().toString();
  doc["mac_address"]      = WiFi.macAddress();
  doc["firmware_version"] = FIRMWARE_VER;
  doc["sensors"]          = SENSOR_TAGS;

  String requestBody;
  serializeJson(doc, requestBody);

  int httpCode = http.POST(requestBody);
  bool success = false;

  if (httpCode == HTTP_CODE_OK || httpCode == HTTP_CODE_CREATED) {
    String response = http.getString();
    Serial.print("[Announce] Success! Server response: ");
    Serial.println(response);
    success = true;
  } else {
    Serial.printf("[Announce] HTTP POST failed, error code: %d - %s\n", httpCode, http.errorToString(httpCode).c_str());
  }

  http.end();
  return success;
}

// ==========================================
// Telemetry Ingestion: POST /api/vitals
// ==========================================
bool sendTelemetryToServer(float bpm, float spo2, float tempC, float tempF, bool fingerDetected) {
  if (WiFi.status() != WL_CONNECTED) {
    return false;
  }

  HTTPClient http;
  String url = "http://" + String(SERVER_HOST) + ":" + String(SERVER_PORT) + "/api/vitals";

  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(2500);

  StaticJsonDocument<320> doc;
  doc["device_id"]       = DEVICE_ID;
  doc["client_id"]       = CLIENT_ID;

  if (fingerDetected && bpm > 30.0 && bpm < 220.0) {
    doc["heart_rate"] = round(bpm * 10.0) / 10.0;
  } else {
    doc["heart_rate"] = nullptr;
  }

  if (fingerDetected && spo2 > 50.0 && spo2 <= 100.0) {
    doc["spo2"] = round(spo2 * 10.0) / 10.0;
  } else {
    doc["spo2"] = nullptr;
  }

  if (hasValidTemp) {
    doc["temperature_c"] = round(tempC * 100.0) / 100.0;
    doc["temperature_f"] = round(tempF * 100.0) / 100.0;
  } else {
    doc["temperature_c"] = nullptr;
    doc["temperature_f"] = nullptr;
  }

  doc["finger_detected"] = fingerDetected;

  String jsonPayload;
  serializeJson(doc, jsonPayload);

  int httpResponseCode = http.POST(jsonPayload);
  bool sent = false;

  if (httpResponseCode == HTTP_CODE_OK || httpResponseCode == HTTP_CODE_CREATED) {
    sent = true;
  } else {
    Serial.printf("[Vitals] HTTP Send failed: code %d (%s)\n", httpResponseCode, http.errorToString(httpResponseCode).c_str());
  }

  http.end();
  return sent;
}

// ==========================================
// Sensor Reading: DS18B20 Body Temperature
// ==========================================
void readTemperature() {
  tempSensors.requestTemperatures();
  float c = tempSensors.getTempCByIndex(0);

  // Check if sensor reading is valid (-127C is error condition)
  if (c > -50.0 && c < 85.0 && c != DEVICE_DISCONNECTED_C) {
    lastTempC = c;
    lastTempF = tempSensors.toFahrenheit(c);
    hasValidTemp = true;
    ds18b20Detected = true;
  } else {
    hasValidTemp = false;
  }
}

// ==========================================
// Sensor Reading: MAX30102 PPG & Oximetry
// ==========================================
void setupMAX30102() {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

  if (!max30102.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("[MAX30102] ERROR: Sensor not found on I2C bus! Check SDA/SCL wiring & 3.3V power.");
    max30102Detected = false;
    return;
  }

  max30102Detected = true;
  Serial.println("[MAX30102] Sensor initialized successfully.");

  // Configure sensor settings
  byte ledBrightness = 60; // 0x3C: 12mA (60/255 * 50mA)
  byte sampleAverage = 4;  // Options: 1, 2, 4, 8, 16, 32
  byte ledMode = 2;        // 2 = Red + IR (SpO2 mode)
  int sampleRate = 100;    // Options: 50, 100, 200, 400, 800, 1000, 1600, 3200
  int pulseWidth = 411;    // Options: 69, 118, 215, 411
  int adcRange = 4096;     // Options: 2048, 4096, 8192, 16384

  max30102.setup(ledBrightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange);
  max30102.setPulseAmplitudeRed(0x1F); // Red LED intensity
  max30102.setPulseAmplitudeIR(0x1F);  // IR LED intensity
  max30102.setPulseAmplitudeGreen(0);  // Green LED off
}

// ==========================================
// Setup Routine
// ==========================================
void setup() {
  Serial.begin(115200);
  delay(1000);

  pinMode(STATUS_LED_PIN, OUTPUT);
  digitalWrite(STATUS_LED_PIN, LOW);

  Serial.println("\n==============================================");
  Serial.println("  ESP32 Clinical Patient Vitals Node Starting  ");
  Serial.printf("  Device ID:  %s\n", DEVICE_ID);
  Serial.printf("  Client ID:  %s\n", CLIENT_ID);
  Serial.printf("  Firmware:   v%s\n", FIRMWARE_VER);
  Serial.println("==============================================\n");

  // 1. Initialize DS18B20 1-Wire Temperature Sensor
  Serial.println("[DS18B20] Initializing 1-Wire temperature sensor on GPIO 4...");
  tempSensors.begin();
  tempSensors.setResolution(11); // 11-bit resolution (~0.125 deg C)
  tempSensors.setWaitForConversion(false); // Non-blocking reads
  readTemperature();

  // 2. Initialize MAX30102 Pulse Oximeter
  setupMAX30102();

  // 3. Connect to Local Wi-Fi
  connectWiFi();

  // 4. Initial Announce to Central Server
  if (WiFi.status() == WL_CONNECTED) {
    bool ok = announceDeviceToServer();
    if (ok) {
      Serial.println("[System] Registered & Announced to Central Dashboard.");
    }
  }

  lastTelemetryTime = millis();
  lastAnnounceTime  = millis();
  lastWifiCheck     = millis();
}

// ==========================================
// Main Loop
// ==========================================
void loop() {
  unsigned long now = millis();

  // Continuous Beat Detection from MAX30102
  long irValue = 0;
  long redValue = 0;
  bool fingerDetected = false;

  if (max30102Detected) {
    irValue = max30102.getIR();
    redValue = max30102.getRed();

    // Finger detection threshold
    if (irValue > 50000) {
      fingerDetected = true;

      // Heartbeat detection
      if (checkForBeat(irValue)) {
        long delta = now - lastBeat;
        lastBeat = now;

        beatsPerMinute = 60.0 / (delta / 1000.0);

        // Sanity range filter for real human pulse
        if (beatsPerMinute >= 40.0 && beatsPerMinute <= 200.0) {
          rates[rateSpot++] = (byte)beatsPerMinute;
          rateSpot %= RATE_SIZE;

          // Rolling average calculation
          int rateSum = 0;
          for (byte x = 0; x < RATE_SIZE; x++) {
            rateSum += rates[x];
          }
          beatAvg = rateSum / RATE_SIZE;

          // Quick pulse indicator LED flash
          digitalWrite(STATUS_LED_PIN, HIGH);
          delay(10);
          digitalWrite(STATUS_LED_PIN, LOW);
        }
      }

      // Collect AC/DC samples for SpO2 calculation
      irBuffer[sampleIndex] = irValue;
      redBuffer[sampleIndex] = redValue;
      sampleIndex = (sampleIndex + 1) % 50;

      // Approximate SpO2 from AC/DC components every 25 samples
      if (sampleIndex % 25 == 0) {
        long minIR = irBuffer[0], maxIR = irBuffer[0], sumIR = 0;
        long minRed = redBuffer[0], maxRed = redBuffer[0], sumRed = 0;

        for (int i = 0; i < 50; i++) {
          if (irBuffer[i] < minIR) minIR = irBuffer[i];
          if (irBuffer[i] > maxIR) maxIR = irBuffer[i];
          sumIR += irBuffer[i];

          if (redBuffer[i] < minRed) minRed = redBuffer[i];
          if (redBuffer[i] > maxRed) maxRed = redBuffer[i];
          sumRed += redBuffer[i];
        }

        float dcIR = (float)sumIR / 50.0;
        float acIR = (float)(maxIR - minIR);

        float dcRed = (float)sumRed / 50.0;
        float acRed = (float)(maxRed - minRed);

        if (dcIR > 0 && dcRed > 0 && acIR > 0) {
          float ratio = (acRed / dcRed) / (acIR / dcIR);
          // Standard empirical curve approximation: SpO2 = 110 - 25 * R
          float spo2Val = 110.0 - (25.0 * ratio);
          if (spo2Val >= 75.0 && spo2Val <= 100.0) {
            // Smooth filter
            calculatedSpO2 = (calculatedSpO2 * 0.7) + (spo2Val * 0.3);
          }
        }
      }

    } else {
      fingerDetected = false;
      beatsPerMinute = 0;
      beatAvg = 0;
    }
  }

  // Periodic Telemetry Transmission
  if (now - lastTelemetryTime >= TELEMETRY_INTERVAL_MS) {
    lastTelemetryTime = now;

    // Read DS18B20 temperature
    readTemperature();

    // Print to Serial Console
    Serial.printf("[Tele] Finger: %s | HR: %d BPM | SpO2: %.1f%% | Temp: %.2fC (%.2fF)\n",
                  fingerDetected ? "YES" : "NO ",
                  beatAvg,
                  fingerDetected ? calculatedSpO2 : 0.0,
                  lastTempC,
                  lastTempF);

    // Send HTTP POST to FastAPI backend
    sendTelemetryToServer(
      beatAvg > 0 ? (float)beatAvg : 0.0,
      fingerDetected ? calculatedSpO2 : 0.0,
      lastTempC,
      lastTempF,
      fingerDetected
    );
  }

  // Periodic Server Announcement & Keepalive
  if (now - lastAnnounceTime >= ANNOUNCE_INTERVAL_MS) {
    lastAnnounceTime = now;
    announceDeviceToServer();
  }

  // Periodic Wi-Fi Health Check & Auto-reconnect
  if (now - lastWifiCheck >= WIFI_RETRY_INTERVAL_MS) {
    lastWifiCheck = now;
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("[WiFi] Connection lost. Attempting reconnection...");
      connectWiFi();
    }
  }

  // Brief yield for ESP32 FreeRTOS watchdog
  delay(5);
}
