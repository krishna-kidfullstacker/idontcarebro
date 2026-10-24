/*
 * Arduino UNO - 5-Floor Lift Controller & Multi-Door Automation
 * ==============================================================
 * Clean Redesign with Exact User Hardware Wiring:
 *
 * 1. BUTTONS (Active LOW, internal INPUT_PULLUP):
 *    - D12: Down Button (Station Floor 3 -> Ground Floor 1)
 *    - D13: Up Button   (Station Floor 3 -> Top Floor 5)
 *    Wiring: One leg of button to pin D12/D13, other leg to Arduino GND.
 *
 * 2. SINGLE INDICATOR LED:
 *    - D7:  LED Light (Anode through 220-330 ohm resistor -> D7, Cathode -> GND)
 *    Turns ON when a button is pressed / request is active / lift or doors moving.
 *    Turns OFF when lift cycle is complete or idle.
 *
 * 3. MOTOR DRIVER 1 (Pins 2, 3, 4, 5):
 *    - IN1 = Pin 2, IN2 = Pin 3: Floor 5 Door Motor (OUT1, OUT2)
 *    - IN3 = Pin 4, IN4 = Pin 5: Lift Hoist Motor   (OUT3, OUT4)
 *      * Hoist UP:   IN3 = HIGH, IN4 = LOW
 *      * Hoist DOWN: IN3 = LOW,  IN4 = HIGH
 *      * Hoist STOP: IN3 = LOW,  IN4 = LOW
 *
 * 4. MOTOR DRIVER 2 (Pins 8, 9, 10, 11):
 *    - IN1 = Pin 8,  IN2 = Pin 9:  Floor 1 Door Motor (OUT1, OUT2)
 *    - IN3 = Pin 10, IN4 = Pin 11: Floor 3 Door Motor (OUT3, OUT4)
 *
 * NOTE:
 * - ENA and ENB jumpers are attached on both L298N boards.
 * - Non-blocking timing using millis() ensures Serial and button responses are instant.
 * - Non-blocking serial buffer prevents loop latency.
 * - Baud rate: 115200 baud over standard USB cable.
 */

// ==================== PIN DEFINITIONS ====================
// Motor Driver 1: Floor 5 Door & Lift Hoist
const int PIN_DOOR5_IN1 = 2;
const int PIN_DOOR5_IN2 = 3;
const int PIN_HOIST_IN3 = 4;
const int PIN_HOIST_IN4 = 5;

// Motor Driver 2: Floor 1 Door & Floor 3 Door
const int PIN_DOOR1_IN1 = 8;
const int PIN_DOOR1_IN2 = 9;
const int PIN_DOOR3_IN3 = 10;
const int PIN_DOOR3_IN4 = 11;

// Single LED Indicator
const int PIN_LED = 7;

// Station Floor 3 Request Buttons
const int PIN_BTN_DOWN = 12; // Floor 3 -> Ground Floor 1
const int PIN_BTN_UP   = 13; // Floor 3 -> Floor 5

// Timing constants
const unsigned long BUTTON_DEBOUNCE_MS = 250;
const unsigned long DOOR_RUN_DURATION_MS = 1800; // Time door motor runs to open/close

// ==================== STATE VARIABLES ====================
unsigned long lastDebounceDown = 0;
unsigned long lastDebounceUp   = 0;
int lastBtnDownState = HIGH;
int lastBtnUpState   = HIGH;

// Non-blocking door motor auto-stop timer
unsigned long doorMotorStartTime = 0;
bool isDoorMotorRunning = false;
bool isDoorClosingAction = false;
int activeDoorFloor = 0;

// Current tracked positions
int currentFloor = 1;

// Non-blocking serial buffer
char serialBuffer[64];
byte serialBufferIndex = 0;

// ==================== HARDWARE CONTROL HELPERS ====================
void setLed(bool on) {
  digitalWrite(PIN_LED, on ? HIGH : LOW);
}

void hoistUp() {
  stopAllDoors();
  digitalWrite(PIN_HOIST_IN3, HIGH);
  digitalWrite(PIN_HOIST_IN4, LOW);
}

void hoistDown() {
  stopAllDoors();
  digitalWrite(PIN_HOIST_IN3, LOW);
  digitalWrite(PIN_HOIST_IN4, HIGH);
}

void hoistStop() {
  digitalWrite(PIN_HOIST_IN3, LOW);
  digitalWrite(PIN_HOIST_IN4, LOW);
}

// Floor 1 Door Motor (Pins 8, 9)
void door1Open() {
  digitalWrite(PIN_DOOR1_IN1, HIGH);
  digitalWrite(PIN_DOOR1_IN2, LOW);
}

void door1Close() {
  digitalWrite(PIN_DOOR1_IN1, LOW);
  digitalWrite(PIN_DOOR1_IN2, HIGH);
}

void door1Stop() {
  digitalWrite(PIN_DOOR1_IN1, LOW);
  digitalWrite(PIN_DOOR1_IN2, LOW);
}

// Floor 3 Door Motor (Pins 10, 11)
void door3Open() {
  digitalWrite(PIN_DOOR3_IN3, HIGH);
  digitalWrite(PIN_DOOR3_IN4, LOW);
}

void door3Close() {
  digitalWrite(PIN_DOOR3_IN3, LOW);
  digitalWrite(PIN_DOOR3_IN4, HIGH);
}

void door3Stop() {
  digitalWrite(PIN_DOOR3_IN3, LOW);
  digitalWrite(PIN_DOOR3_IN4, LOW);
}

// Floor 5 Door Motor (Pins 2, 3)
void door5Open() {
  digitalWrite(PIN_DOOR5_IN1, HIGH);
  digitalWrite(PIN_DOOR5_IN2, LOW);
}

void door5Close() {
  digitalWrite(PIN_DOOR5_IN1, LOW);
  digitalWrite(PIN_DOOR5_IN2, HIGH);
}

void door5Stop() {
  digitalWrite(PIN_DOOR5_IN1, LOW);
  digitalWrite(PIN_DOOR5_IN2, LOW);
}

void stopAllDoors() {
  door1Stop();
  door3Stop();
  door5Stop();
  isDoorMotorRunning = false;
}

void stopAllMotors() {
  hoistStop();
  stopAllDoors();
}

// Start timed door action (Non-blocking)
void triggerDoorAction(int floorNum, bool isOpening) {
  hoistStop();
  stopAllDoors();
  activeDoorFloor = floorNum;
  isDoorClosingAction = !isOpening;
  isDoorMotorRunning = true;
  doorMotorStartTime = millis();
  setLed(true);

  if (floorNum == 1) {
    if (isOpening) door1Open(); else door1Close();
  } else if (floorNum == 5) {
    if (isOpening) door5Open(); else door5Close();
  } else {
    // Default to Floor 3
    if (isOpening) door3Open(); else door3Close();
  }
}

// ==================== COMMAND PARSER ====================
void processSerialCommand(const String& cmd) {
  if (cmd.length() == 0) return;

  // Handshake / Ping
  if (cmd == "PING") {
    Serial.println(F("PONG"));
    return;
  }

  // Hoist Movement: UP
  if (cmd.startsWith("CMD:LIFT:UP")) {
    hoistUp();
    Serial.println(F("ACK:LIFT:UP"));

  // Hoist Movement: DOWN
  } else if (cmd.startsWith("CMD:LIFT:DOWN")) {
    hoistDown();
    Serial.println(F("ACK:LIFT:DOWN"));

  // Hoist Movement: STOP
  } else if (cmd == "CMD:LIFT:STOP") {
    hoistStop();
    Serial.println(F("ACK:LIFT:STOP"));

  // Lift Arrived at Floor
  } else if (cmd.startsWith("CMD:LIFT:ARRIVED:")) {
    hoistStop();
    int arrFloor = cmd.substring(17).toInt();
    if (arrFloor >= 1 && arrFloor <= 5) {
      currentFloor = arrFloor;
    }
    Serial.print(F("ACK:LIFT:ARRIVED:"));
    Serial.println(currentFloor);

  // Open Door Commands (Handles CMD:DOOR:OPEN, CMD:DOOR:OPEN:1, CMD:DOOR:OPEN:3, CMD:DOOR:OPEN:5)
  } else if (cmd.startsWith("CMD:DOOR:OPEN")) {
    int targetF = 3;
    if (cmd.indexOf(":1") != -1) targetF = 1;
    else if (cmd.indexOf(":5") != -1) targetF = 5;
    else if (cmd.indexOf(":3") != -1) targetF = 3;
    triggerDoorAction(targetF, true);
    Serial.print(F("ACK:DOOR:OPEN:"));
    Serial.println(targetF);

  // Close Door Commands (Handles CMD:DOOR:CLOSE, CMD:DOOR:CLOSE:1, CMD:DOOR:CLOSE:3, CMD:DOOR:CLOSE:5)
  } else if (cmd.startsWith("CMD:DOOR:CLOSE")) {
    int targetF = 3;
    if (cmd.indexOf(":1") != -1) targetF = 1;
    else if (cmd.indexOf(":5") != -1) targetF = 5;
    else if (cmd.indexOf(":3") != -1) targetF = 3;
    triggerDoorAction(targetF, false);
    Serial.print(F("ACK:DOOR:CLOSE:"));
    Serial.println(targetF);

  // Stop Doors
  } else if (cmd == "CMD:DOOR:STOP") {
    stopAllDoors();
    Serial.println(F("ACK:DOOR:STOP"));

  // LED Commands
  } else if (cmd == "CMD:LED:ON" || cmd == "CMD:LED:DOWN:ON" || cmd == "CMD:LED:UP:ON" || cmd == "RESULT:SUCCESS") {
    setLed(true);

  } else if (cmd == "CMD:LED:OFF") {
    setLed(false);

  } else if (cmd == "RESULT:CANCELED") {
    setLed(false);
    hoistStop();
    stopAllDoors();

  // Operation Complete / System Ready
  } else if (cmd == "CMD:OPERATION:COMPLETE" || cmd == "STATUS:READY") {
    stopAllMotors();
    setLed(false);
    Serial.println(F("ACK:SYSTEM:READY"));
  }
}

// ==================== SETUP ====================
void setup() {
  Serial.begin(115200);

  // Buttons with internal pullup
  pinMode(PIN_BTN_DOWN, INPUT_PULLUP);
  pinMode(PIN_BTN_UP,   INPUT_PULLUP);

  // Single LED
  pinMode(PIN_LED, OUTPUT);
  setLed(false);

  // Motor Driver 1 Pins (Floor 5 Door & Hoist)
  pinMode(PIN_DOOR5_IN1, OUTPUT);
  pinMode(PIN_DOOR5_IN2, OUTPUT);
  pinMode(PIN_HOIST_IN3, OUTPUT);
  pinMode(PIN_HOIST_IN4, OUTPUT);

  // Motor Driver 2 Pins (Floor 1 Door & Floor 3 Door)
  pinMode(PIN_DOOR1_IN1, OUTPUT);
  pinMode(PIN_DOOR1_IN2, OUTPUT);
  pinMode(PIN_DOOR3_IN3, OUTPUT);
  pinMode(PIN_DOOR3_IN4, OUTPUT);

  // Safe initial stop
  stopAllMotors();

  Serial.println(F("UNO_LIFT_CONTROLLER_READY"));
}

// ==================== MAIN LOOP ====================
void loop() {
  unsigned long now = millis();

  // ── 1. Check Button Down (D12: Floor 3 -> Ground Floor 1) with Edge Detection ──
  int currentDown = digitalRead(PIN_BTN_DOWN);
  if (currentDown == LOW && lastBtnDownState == HIGH && (now - lastDebounceDown > BUTTON_DEBOUNCE_MS)) {
    lastDebounceDown = now;
    setLed(true);
    Serial.println(F("START_SCAN:DOWN"));
  }
  lastBtnDownState = currentDown;

  // ── 2. Check Button Up (D13: Floor 3 -> Floor 5) with Edge Detection ──
  int currentUp = digitalRead(PIN_BTN_UP);
  if (currentUp == LOW && lastBtnUpState == HIGH && (now - lastDebounceUp > BUTTON_DEBOUNCE_MS)) {
    lastDebounceUp = now;
    setLed(true);
    Serial.println(F("START_SCAN:UP"));
  }
  lastBtnUpState = currentUp;

  // ── 3. Non-blocking Door Motor Auto-Stop ──
  if (isDoorMotorRunning) {
    if (now - doorMotorStartTime >= DOOR_RUN_DURATION_MS) {
      stopAllDoors();
      // If closing cycle finished, notify backend that doors are fully closed
      if (isDoorClosingAction) {
        Serial.println(F("DOOR_DONE"));
      }
    }
  }

  // ── 4. Non-blocking Serial Command Reading ──
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (serialBufferIndex > 0) {
        serialBuffer[serialBufferIndex] = '\0';
        String cmdStr = String(serialBuffer);
        cmdStr.trim();
        processSerialCommand(cmdStr);
        serialBufferIndex = 0;
      }
    } else {
      if (serialBufferIndex < sizeof(serialBuffer) - 1) {
        serialBuffer[serialBufferIndex++] = c;
      }
    }
  }
}
