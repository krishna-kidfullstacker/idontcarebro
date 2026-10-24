# 🛗 5-Floor Smart Lift Automation with Arduino UNO, 4 Motors & AI Vision

An end-to-end IoT and Edge AI elevator automation system designed for a **5-Floor Real-Life Building Prototype** with **Floor 3 as the Main Station**, **4 Motors (Dual L298N drivers)**, **2 Floor Call Buttons & Indicator Lights**, and an **Arduino UNO Door Controller & Simulator**.

---

## 🏢 Building Layout

```
  Floor 5  (Top Floor)
  Floor 4
  Floor 3  ← ★ MAIN STATION (Buttons, LEDs, OLED, Camera)
  Floor 2
  Floor 1  (Ground Floor / Lobby)
```

- **Floor 3 is the Main Station**:
  - **DOWN Request**: Floor 3 ➔ Ground Floor (Floor 1)
  - **UP Request**: Floor 3 ➔ Floor 5 (Top Floor)
- **Buttons & Indicator Lights Behavior**:
  - When the DOWN or UP button is pressed, the corresponding indicator LED turns **ON**.
  - The LED **stays ON throughout the entire operation cycle**:
    1. Passenger request accepted & lift arrives at Floor 3.
    2. Physical door motor smoothly opens with PWM acceleration.
    3. Door holds open for passenger entry/exit dwell time.
    4. Physical door motor smoothly closes with PWM deceleration.
    5. Once the door operation cycle completes (or passenger arrives at destination), the LED turns **OFF**.
  - If **no passenger is detected** (or timeout / cancellation occurs), the LED immediately turns **OFF** and the system returns to IDLE.

---

## ⚙️ 4 Motors Architecture (Dual L298N Drivers)

The building hardware supports **4 motors** using two L298N dual H-bridge motor drivers:

| Motor | Role | Driver & Channel | Arduino UNO Pins | Status in Project |
|---|---|---|---|---|
| **Motor 1** | **Door Movement Motor** | L298N #1 Channel A | **IN1 = D7, IN2 = D8, ENA = D9 (PWM)** | **ACTIVE** (Physical smooth open/close cycle) |
| **Motor 2** | **Lift Hoist / Movement Motor** | L298N #1 Channel B | **IN3 = D11, IN4 = D12, ENB = D10 (PWM)** | **READY / PLACEHOLDER** (Pre-wired & assigned in code) |
| **Motor 3** | **Auxiliary Motor 3** | L298N #2 Channel A | **IN1 = A0, IN2 = A1, ENA = 5V jumper** | **READY / PLACEHOLDER** (Pre-wired & assigned in code) |
| **Motor 4** | **Auxiliary Motor 4** | L298N #2 Channel B | **IN3 = A2, IN4 = A3, ENB = 5V jumper** | **READY / PLACEHOLDER** (Pre-wired & assigned in code) |

---

## 🔌 Hardware Connections & Wiring Guide

### 1. Arduino UNO Pinout

| Peripheral / Component | Arduino UNO Pin | Wiring & Connection Details |
|---|---|---|
| **0.96" I2C OLED (SDA)** | **A4** | Connects to OLED SDA pin |
| **0.96" I2C OLED (SCL)** | **A5** | Connects to OLED SCL pin |
| **0.96" I2C OLED (VCC)** | **5V** | Arduino 5V rail |
| **0.96" I2C OLED (GND)** | **GND** | Arduino Common Ground |
| **DOWN Button** (Floor 3 ➔ Ground) | **D2** | One leg to D2, other leg to GND (Internal `INPUT_PULLUP`) |
| **UP Button** (Floor 3 ➔ Floor 5) | **D3** | One leg to D3, other leg to GND (Internal `INPUT_PULLUP`) |
| **DOWN Indicator LED** (Blue) | **D5** | Arduino D5 ➔ 220Ω resistor ➔ Anode (+), Cathode (-) ➔ GND |
| **UP Indicator LED** (Amber) | **D6** | Arduino D6 ➔ 220Ω resistor ➔ Anode (+), Cathode (-) ➔ GND |
| **Operation Running LED** (Red) | **D13** | Arduino D13 ➔ 220Ω resistor ➔ Anode (+), Cathode (-) ➔ GND |
| **L298N #1 - IN1** (Door Dir 1) | **D7** | Door motor forward / open |
| **L298N #1 - IN2** (Door Dir 2) | **D8** | Door motor reverse / close |
| **L298N #1 - ENA** (Door PWM) | **D9** | Hardware PWM (0–255) for smooth acceleration / deceleration |
| **L298N #1 - IN3** (Lift Dir 1) | **D11** | Lift hoist motor upward direction (placeholder) |
| **L298N #1 - IN4** (Lift Dir 2) | **D12** | Lift hoist motor downward direction (placeholder) |
| **L298N #1 - ENB** (Lift PWM) | **D10** | Lift hoist motor PWM speed (placeholder) |
| **L298N #2 - IN1** (Motor 3 Dir 1)| **A0** | Analog pin A0 used as digital output |
| **L298N #2 - IN2** (Motor 3 Dir 2)| **A1** | Analog pin A1 used as digital output |
| **L298N #2 - IN3** (Motor 4 Dir 1)| **A2** | Analog pin A2 used as digital output |
| **L298N #2 - IN4** (Motor 4 Dir 2)| **A3** | Analog pin A3 used as digital output |
| **Common Ground** | **GND** | Connect Arduino GND, L298N #1 GND, and L298N #2 GND together! |

> **IMPORTANT POWER NOTES:**
> 1. **Arduino UNO**: Powered via USB cable connected to your laptop.
> 2. **L298N Motor Drivers**: Connect an external 12V DC power supply to the L298N 12V/GND terminals to power the motors.
> 3. **Shared Ground**: Always connect the external power supply GND and Arduino GND together to establish a common ground reference.

---

## 🚀 Running the Project

### 1. Arduino UNO Code
- File: `lift_simulator_Ecs/newww.ino`
- Libraries needed in Arduino IDE:
  - `Adafruit SSD1306`
  - `Adafruit GFX Library`
- Select Board: **Arduino Uno**
- Select Port: **COM3** (or your detected COM port)
- Click **Upload**.

### 2. Python Backend & Simulation Server
1. Open PowerShell in the project directory:
   ```powershell
   cd "c:\Users\KRISHNA KANTH\ecsfinalv2\lift_simulator_Ecs"
   ```
2. Install dependencies (if not already installed):
   ```powershell
   pip install fastapi uvicorn websockets pyserial pydantic httpx
   ```
3. Run the application:
   ```powershell
   python main.py
   ```
   Or using uvicorn:
   ```powershell
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

### 3. Accessing the User Interfaces
- **Desktop Simulator**: Open `http://localhost:8000` in your computer browser.
- **Mobile Phone AI Camera**: Open `http://<YOUR_LOCAL_IP>:8000/mobile` from your smartphone connected to the same Wi-Fi / Hotspot.

---

## 🧪 Verifying the Implementation

Run the automated test suite anytime to verify physics, endpoints, and cancellation flows:
```powershell
python test_simulation.py
```
Expected output: **8 passed in 0.5s (100% OK)**.
