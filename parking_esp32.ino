/*
  Smart Parking - ESP32 DevKit V1 firmware (Smooth Gate Sweep)
  - 9 active-low IR sensors -> FastAPI/MongoDB backend
  - 3x I2C 16x2 LCD via TCA9548A (live slot status + 4-digit Gate OTP)
  - 3x Micro-servos (D13, D14, D18) on isolated 5V rail with shared GND

  Libraries (Library Manager):
    1. "LiquidCrystal I2C" (Frank de Brabander)
    2. "ArduinoJson" (v7)
    3. "ESP32Servo" (Kevin Harrington)
  Board: "ESP32 Dev Module".  Serial monitor: 115200.
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// ---------- EDIT THESE ----------
const char* WIFI_SSID  = "YOUR_WIFI_NAME";
const char* WIFI_PASS  = "YOUR_WIFI_PASSWORD";
const char* SERVER_URL = "https://your-app.vercel.app";   // update with your Vercel deployment URL (no trailing slash)
const char* API_KEY    = "same-value-as-ESP32_API_KEY";
// --------------------------------

const uint8_t TCA_ADDR = 0x70;   // TCA9548A default (A0-A2 to GND)
const uint8_t LCD_ADDR = 0x27;   // use 0x3F if your LCD backpack uses PCF8574A

// Bldg 1: D25, D26, D27 | Bldg 2: D32, D33, D34 | Bldg 3: D35, VP(36), VN(39)
const uint8_t PINS[3][3] = {
  {25, 26, 27},
  {32, 33, 34},
  {35, 36, 39}
};

// Gate servo signal pins: Bldg 1 -> D13, Bldg 2 -> D14, Bldg 3 -> D18
const uint8_t GATE_PINS[3]         = {13, 14, 18};
const int GATE_CLOSED_ANGLE        = 12;     // Stops clear of internal 0-deg gear wall to prevent stall spikes
const int GATE_OPEN_ANGLE          = 95;
const unsigned long GATE_OPEN_MS   = 5000;   // Keep gate open for 5 seconds
const unsigned long GATE_STEP_MS   = 15;     // Smooth 5-deg sweep step interval
const unsigned long GATE_SETTLE_MS = 250;    // Brief settle before detaching

const unsigned long SAMPLE_MS      = 100;    // IR poll interval (3 samples = 300ms debounce)
const unsigned long HEARTBEAT_MS   = 2000;   // Fast 2s sync for bookings/OTP + heartbeat
const unsigned long RETRY_MS       = 1500;   // Fast retry if POST failed or Wi-Fi was reconnecting
const unsigned long WIFI_RETRY_MS  = 5000;   // Non-blocking Wi-Fi reconnect cooldown

LiquidCrystal_I2C lcd(LCD_ADDR, 16, 2);
Servo gateServo;
WiFiClientSecure secureClient;
WiFiClient plainClient;
HTTPClient http;
bool httpConfigured = false;

bool ir[3][3];                 // debounced physical IR state (true = LOW = occupied)
uint8_t cnt[3][3];             // consecutive opposite-sample counter
char st[3][3];                 // latest backend state char: 'F', 'B', or 'O'
int freeCt[3] = {3, 3, 3};
char gateCode[3][8] = {"", "", ""};
bool haveState = false;

// Sequential single-motor gate queue with smooth ramped sweep
long lastGateSeq[3] = {-1, -1, -1};
bool gatePending[3] = {false, false, false};
int activeGate = -1;
uint8_t gatePhase = 0;         // 0=idle, 1=sweeping open, 2=holding open, 3=sweeping closed, 4=settling
unsigned long gateTimer = 0;
int currentGateAngle = GATE_CLOSED_ANGLE;
int targetGateAngle  = GATE_CLOSED_ANGLE;

// Track last rendered LCD snapshot so unchanged displays aren't redrawn every 5s
bool lcdHasState[3] = {false, false, false};
int lastShownFree[3] = {-1, -1, -1};
char lastShownSlots[3][3] = {{0, 0, 0}, {0, 0, 0}, {0, 0, 0}};
char lastShownCode[3][8] = {"", "", ""};
bool lastShownOpening[3] = {false, false, false};

unsigned long lastSample = 0;
unsigned long lastSend = 0;
unsigned long lastWifiAttempt = 0;
bool needImmediateSend = true;

bool selectCh(uint8_t ch) {
  if (ch > 7) return false;
  Wire.beginTransmission(TCA_ADDR);
  Wire.write(1 << ch);
  return (Wire.endTransmission() == 0);
}

void showLcd(int b, bool force = false) {
  if (!selectCh(b)) return;

  if (!haveState) {
    if (!force && lcdHasState[b]) return;
    char l1[17];
    snprintf(l1, sizeof(l1), "Bldg %d          ", b + 1);
    lcd.setCursor(0, 0);
    lcd.print(l1);
    lcd.setCursor(0, 1);
    lcd.print("Connecting...   ");
    return;
  }

  bool isOpening = (activeGate == b);
  bool same = (!force) &&
              lcdHasState[b] &&
              (lastShownFree[b] == freeCt[b]) &&
              (lastShownSlots[b][0] == st[b][0]) &&
              (lastShownSlots[b][1] == st[b][1]) &&
              (lastShownSlots[b][2] == st[b][2]) &&
              (strcmp(lastShownCode[b], gateCode[b]) == 0) &&
              (lastShownOpening[b] == isOpening);
  if (same) return;

  char l1[17], l2[17];
  snprintf(l1, sizeof(l1), "Bldg %d  Free %d/3", b + 1, freeCt[b]);

  if (isOpening) {
    snprintf(l2, sizeof(l2), "Gate OPENING... ");
  } else if (gateCode[b][0] != '\0') {
    snprintf(l2, sizeof(l2), "Gate OTP: %-6s", gateCode[b]);
  } else {
    snprintf(l2, sizeof(l2), "1:%c 2:%c 3:%c     ", st[b][0], st[b][1], st[b][2]);
  }

  lcd.setCursor(0, 0);
  lcd.print(l1);
  lcd.setCursor(0, 1);
  lcd.print(l2);

  lcdHasState[b] = true;
  lastShownFree[b] = freeCt[b];
  for (int s = 0; s < 3; s++) lastShownSlots[b][s] = st[b][s];
  strncpy(lastShownCode[b], gateCode[b], sizeof(lastShownCode[b]) - 1);
  lastShownCode[b][sizeof(lastShownCode[b]) - 1] = '\0';
  lastShownOpening[b] = isOpening;
}

void processGates() {
  unsigned long now = millis();

  if (activeGate == -1) {
    for (int b = 0; b < 3; b++) {
      if (gatePending[b]) {
        gatePending[b] = false;
        activeGate = b;
        gatePhase = 1;
        gateTimer = now;
        currentGateAngle = GATE_CLOSED_ANGLE;
        targetGateAngle = GATE_OPEN_ANGLE;
        Serial.printf("Opening Building %d gate (GPIO %d)\n", b + 1, GATE_PINS[b]);
        gateServo.setPeriodHertz(50);
        gateServo.attach(GATE_PINS[b], 600, 2400);
        gateServo.write(currentGateAngle);
        showLcd(b, true);
        break;
      }
    }
    return;
  }

  if (gatePhase == 1) {
    if (now - gateTimer >= GATE_STEP_MS) {
      gateTimer = now;
      if (currentGateAngle < targetGateAngle) {
        currentGateAngle += 5;
        if (currentGateAngle > targetGateAngle) currentGateAngle = targetGateAngle;
        gateServo.write(currentGateAngle);
      } else {
        gatePhase = 2;
        gateTimer = now;
      }
    }
  } else if (gatePhase == 2 && (now - gateTimer >= GATE_OPEN_MS)) {
    Serial.printf("Closing Building %d gate\n", activeGate + 1);
    targetGateAngle = GATE_CLOSED_ANGLE;
    gatePhase = 3;
    gateTimer = now;
  } else if (gatePhase == 3) {
    if (now - gateTimer >= GATE_STEP_MS) {
      gateTimer = now;
      if (currentGateAngle > targetGateAngle) {
        currentGateAngle -= 5;
        if (currentGateAngle < targetGateAngle) currentGateAngle = targetGateAngle;
        gateServo.write(currentGateAngle);
      } else {
        gatePhase = 4;
        gateTimer = now;
      }
    }
  } else if (gatePhase == 4 && (now - gateTimer >= GATE_SETTLE_MS)) {
    gateServo.detach();
    int finishedB = activeGate;
    activeGate = -1;
    gatePhase = 0;
    showLcd(finishedB, true);
  }
}

void configurePinMode(uint8_t pin) {
  // GPIOs 34..39 are input-only on ESP32 and do not have internal pull-ups
  if (pin >= 34) {
    pinMode(pin, INPUT);
  } else {
    pinMode(pin, INPUT_PULLUP);
  }
}

bool readSensors() {
  bool changed = false;
  for (int b = 0; b < 3; b++) {
    for (int s = 0; s < 3; s++) {
      bool raw = (digitalRead(PINS[b][s]) == LOW);
      if (raw == ir[b][s]) {
        cnt[b][s] = 0;
      } else if (++cnt[b][s] >= 3) {
        ir[b][s] = raw;
        cnt[b][s] = 0;
        changed = true;
      }
    }
  }
  return changed;
}

void ensureWiFiNonBlocking() {
  if (WiFi.status() == WL_CONNECTED) return;
  unsigned long now = millis();
  if (now - lastWifiAttempt >= WIFI_RETRY_MS) {
    lastWifiAttempt = now;
    Serial.println("WiFi disconnected, attempting reconnect...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
}

bool handleHttpResponse(int code, HTTPClient& http) {
  if (code != 200) {
    Serial.printf("Telemetry POST failed, code: %d\n", code);
    return false;
  }

  String payload = http.getString();
  JsonDocument res;
  DeserializationError err = deserializeJson(res, payload);
  if (err) {
    Serial.print("JSON parse error: ");
    Serial.println(err.c_str());
    return false;
  }

  for (JsonObject bld : res["buildings"].as<JsonArray>()) {
    int b = (int)bld["building_id"] - 1;
    if (b < 0 || b > 2) continue;
    freeCt[b] = bld["free"] | 0;

    JsonArray slotsArr = bld["slots"].as<JsonArray>();
    for (int s = 0; s < 3; s++) {
      const char* slotStr = (s < (int)slotsArr.size()) ? (const char*)slotsArr[s] : nullptr;
      st[b][s] = (slotStr && slotStr[0] != '\0') ? slotStr[0] : (ir[b][s] ? 'O' : 'F');
    }

    const char* codeStr = bld["gate_code"] | "";
    strncpy(gateCode[b], codeStr, sizeof(gateCode[b]) - 1);
    gateCode[b][sizeof(gateCode[b]) - 1] = '\0';

    long seq = bld["gate_seq"] | 0L;
    if (lastGateSeq[b] < 0) {
      lastGateSeq[b] = seq;
    } else if (seq > lastGateSeq[b]) {
      lastGateSeq[b] = seq;
      gatePending[b] = true;
    }
  }

  haveState = true;
  for (int b = 0; b < 3; b++) {
    showLcd(b, false);
  }
  return true;
}

bool sendTelemetry() {
  if (WiFi.status() != WL_CONNECTED) {
    ensureWiFiNonBlocking();
    return false;
  }

  JsonDocument doc;
  JsonArray arr = doc["slots"].to<JsonArray>();
  for (int b = 0; b < 3; b++) {
    for (int s = 0; s < 3; s++) {
      JsonObject o = arr.add<JsonObject>();
      o["building_id"] = b + 1;
      o["slot_number"] = s + 1;
      o["ir_occupied"] = ir[b][s];
    }
  }

  String body;
  serializeJson(doc, body);

  String url = String(SERVER_URL) + "/api/esp32/telemetry";
  bool isHttps = strncmp(SERVER_URL, "https", 5) == 0;

  if (!httpConfigured) {
    if (isHttps) {
      secureClient.setInsecure();
    }
    http.setReuse(true);
    http.setTimeout(15000);
    http.setConnectTimeout(8000);
    httpConfigured = true;
  }

  if (isHttps) {
    if (!http.connected()) {
      if (!http.begin(secureClient, url)) {
        Serial.println("HTTPS begin() failed");
        return false;
      }
    }
  } else {
    if (!http.connected()) {
      if (!http.begin(plainClient, url)) {
        Serial.println("HTTP begin() failed");
        return false;
      }
    }
  }

  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-ESP32-Key", API_KEY);
  int code = http.POST(body);
  bool ok = handleHttpResponse(code, http);

  if (!ok || code < 0) {
    http.end();
  }
  return ok;
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\nBoot reset reason: %d\n", (int)esp_reset_reason());

  // Move all 3 gates to closed position one by one at boot, then detach
  for (int b = 0; b < 3; b++) {
    gateServo.setPeriodHertz(50);
    gateServo.attach(GATE_PINS[b], 600, 2400);
    gateServo.write(GATE_CLOSED_ANGLE);
    delay(300);
    gateServo.detach();
  }

  for (int b = 0; b < 3; b++) {
    for (int s = 0; s < 3; s++) {
      configurePinMode(PINS[b][s]);
      ir[b][s] = (digitalRead(PINS[b][s]) == LOW);
      cnt[b][s] = 0;
      st[b][s] = '-';
    }
  }

  Wire.begin(21, 22);   // SDA = D21, SCL = D22
  Wire.setTimeOut(50);
  delay(100);
  for (int b = 0; b < 3; b++) {
    if (selectCh(b)) {
      lcd.init();
      lcd.backlight();
      showLcd(b, true);
    } else {
      Serial.printf("TCA9548A channel %d select failed\n", b);
    }
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  lastWifiAttempt = millis();

  Serial.print("Connecting to WiFi");
  unsigned long startAttempt = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 15000) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("WiFi connected, IP: " + WiFi.localIP().toString());
    if (sendTelemetry()) {
      lastSend = millis();
      needImmediateSend = false;
    } else {
      lastSend = millis() - HEARTBEAT_MS + RETRY_MS;
    }
  } else {
    Serial.println("Initial WiFi connect pending; will retry in loop.");
    lastSend = millis() - HEARTBEAT_MS + RETRY_MS;
  }
}

void loop() {
  processGates();
  ensureWiFiNonBlocking();

  unsigned long now = millis();

  if (now - lastSample >= SAMPLE_MS) {
    lastSample = now;
    if (readSensors()) {
      needImmediateSend = true;
    }
  }

  unsigned long interval = needImmediateSend ? 0 : HEARTBEAT_MS;
  if (now - lastSend >= interval) {
    if (sendTelemetry()) {
      lastSend = millis();
      needImmediateSend = false;
    } else {
      lastSend = millis() - HEARTBEAT_MS + RETRY_MS;
    }
  }
}