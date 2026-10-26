// Virtual Lift Simulation Frontend Logic with AI Passenger Detection & 4-Motor Driver
// 5-Floor Building Layout (Floor 1 Ground to Floor 5 Top, Floor 3 is Main Station)

const MIN_FLOOR = 1;
const MAX_FLOOR = 5;

let currentLiftState = {
  state: "NORMAL",
  current_floor: 1.0,
  display_floor: 1,
  target_floor: null,
  destination_floor: null,
  source: "MANUAL",
  request_id: null,
  door_open_pct: 0.0,
  is_idle: true,
  backend_arrived_url: "http://localhost:8000/lift/arrived"
};

let previousFloor = 1.0;
let ws = null;
let reconnectTimer = null;
let pollTimer = null;
let autoDoorTimer = null;
let totalLogCount = 0;

// Hardware & Passenger Detection State at Floor 3 Station
let selectedFloor3Dest = 1; // 1 = Ground Floor (1), 5 = Top Floor (5)
let isRequestLedActive = false;
let activeRequestDirection = null; // "DOWN" or "UP"
let isRealLifeMotorRunning = false;
let motorAnimInterval = null;

// DOM Elements - Lift Core
const floorsContainer = document.getElementById("floorsContainer");
const liftCar = document.getElementById("liftCar");
const carCable = document.getElementById("carCable");
const doorLeft = document.getElementById("doorLeft");
const doorRight = document.getElementById("doorRight");
const carDisplayFloor = document.getElementById("carDisplayFloor");
const carDirectionArrow = document.getElementById("carDirectionArrow");
const carStateMini = document.getElementById("carStateMini");
const shaftCarFloorVal = document.getElementById("shaftCarFloorVal");

const doorMeterBar = document.getElementById("doorMeterBar");
const doorApertureVal = document.getElementById("doorApertureVal");

const mainStateBadge = document.getElementById("mainStateBadge");
const telCurrentFloor = document.getElementById("telCurrentFloor");
const telTargetFloor = document.getElementById("telTargetFloor");
const telMode = document.getElementById("telMode");
const telRequestId = document.getElementById("telRequestId");
const btnCopyReqId = document.getElementById("btnCopyReqId");

const wsStatusChip = document.getElementById("wsStatusChip");
const wsStatusText = document.getElementById("wsStatusText");
const webhookUrlDisplay = document.getElementById("webhookUrlDisplay");

const btnRequestFloor5 = document.getElementById("btnRequestFloor5");
const btnDoorComplete = document.getElementById("btnDoorComplete");
const floorBtnGrid = document.getElementById("floorBtnGrid");
const autoDoorCompleteToggle = document.getElementById("autoDoorCompleteToggle");

const eventFeedTerminal = document.getElementById("eventFeedTerminal");
const logCountBadge = document.getElementById("logCountBadge");
const btnClearLogs = document.getElementById("btnClearLogs");

// DOM Elements - Floor 3 Call Buttons & Hardware Motor Module
const hwAiStatusBadge = document.getElementById("hwAiStatusBadge");
const passengerPresenceLabel = document.getElementById("passengerPresenceLabel");
const activeSwitchBadge = document.getElementById("activeSwitchBadge");
const btnPassengerVerifyYes = document.getElementById("btnPassengerVerifyYes");
const btnPassengerVerifyNo = document.getElementById("btnPassengerVerifyNo");

// DOM Elements - Live Vision AI Camera on Dashboard
const dashWebcam = document.getElementById("dashWebcam");
const dashOverlay = document.getElementById("dashOverlay");
const camPlaceholder = document.getElementById("camPlaceholder");
const dashCamHud = document.getElementById("dashCamHud");
const dashGlow = document.getElementById("dashGlow");
const btnToggleDashCam = document.getElementById("btnToggleDashCam");
const dashCamBtnText = document.getElementById("dashCamBtnText");
const dashGaugeFill = document.getElementById("dashGaugeFill");
const dashDetectionLabel = document.getElementById("dashDetectionLabel");
const dashGaugeTimer = document.getElementById("dashGaugeTimer");

const btnSwitchDown = document.getElementById("btnSwitchDown");
const btnSwitchUp = document.getElementById("btnSwitchUp");
const ledDownInd = document.getElementById("ledDownInd");
const ledUpInd = document.getElementById("ledUpInd");

const motorStateBadge = document.getElementById("motorStateBadge");
const gearSpinner = document.getElementById("gearSpinner");
const motorPwmVal = document.getElementById("motorPwmVal");
const motorDirVal = document.getElementById("motorDirVal");
const motorApertureVal = document.getElementById("motorApertureVal");

const hwLedAuth = document.getElementById("hwLedAuth");
const hwLedDownActive = document.getElementById("hwLedDownActive");
const hwLedUpActive = document.getElementById("hwLedUpActive");
const hwLedDoorActive = document.getElementById("hwLedDoorActive");

// DOM Elements - Hoist Machine & 4-Motor Architecture
const hoistDrumWheel = document.getElementById("hoistDrumWheel");
const m1Card = document.getElementById("m1Card");
const m1Status = document.getElementById("m1Status");
const m2Card = document.getElementById("m2Card");
const m2Status = document.getElementById("m2Status");
const m3Card = document.getElementById("m3Card");
const m3Status = document.getElementById("m3Status");
const m4Card = document.getElementById("m4Card");
const m4Status = document.getElementById("m4Status");

// High-Precision 60fps Physics & Exact Physical Real-Life Dimensions (68cm Total Height)
const PHYSICAL_ELEVATIONS_CM = {
  1: 0.0,
  2: 12.0,
  3: 25.0,
  4: 37.0,
  5: 51.0
};
const TOTAL_TRAVEL_CM = 51.0;
const TOTAL_TRAVEL_PX = 464.0; // Travel between Floor 1 and Floor 5 sills in stage
const GROUND_FLOOR_BOTTOM = 14.0;

function floorToElevationCm(floor) {
  const f = Math.max(1.0, Math.min(5.0, floor));
  const fLow = Math.floor(f);
  if (f === fLow || fLow >= 5) return PHYSICAL_ELEVATIONS_CM[fLow];
  const frac = f - fLow;
  return PHYSICAL_ELEVATIONS_CM[fLow] + frac * (PHYSICAL_ELEVATIONS_CM[fLow + 1] - PHYSICAL_ELEVATIONS_CM[fLow]);
}

function floorToBottomPx(floor) {
  const elevCm = floorToElevationCm(floor);
  return GROUND_FLOOR_BOTTOM + (elevCm / TOTAL_TRAVEL_CM) * TOTAL_TRAVEL_PX;
}

let renderedFloor = 1.0;
let lastRafTimestamp = performance.now();

function shaftAnimationLoop(currentTime) {
  const dt = Math.min(0.05, (currentTime - lastRafTimestamp) / 1000.0);
  lastRafTimestamp = currentTime;

  const target = currentLiftState.current_floor;
  const isMoving = (currentLiftState.state === "MOVING");

  if (isMoving && currentLiftState.target_floor !== null) {
    const moveDir = currentLiftState.target_floor > renderedFloor ? 1.0 : -1.0;
    // Hardware hoist travel speed: 1.0 floor per 2.5s (0.40 floors/sec)
    const nominalVelocity = 1.0 / 2.5;
    // Soft proportional spring to eliminate any network latency drift smoothly without jerk
    const lead = target - renderedFloor;
    const correctionVelocity = lead * 3.5;
    const step = (nominalVelocity * moveDir + correctionVelocity) * dt;
    renderedFloor += step;

    // Clamp cleanly at target
    if (moveDir > 0 && renderedFloor > currentLiftState.target_floor) {
      renderedFloor = currentLiftState.target_floor;
    } else if (moveDir < 0 && renderedFloor < currentLiftState.target_floor) {
      renderedFloor = currentLiftState.target_floor;
    }
  } else {
    // When idle or at floor, critically damped convergence
    const diff = target - renderedFloor;
    if (Math.abs(diff) > 0.001) {
      renderedFloor += diff * Math.min(1.0, 16.0 * dt);
    } else {
      renderedFloor = target;
    }
  }

  // Position liftCar directly in pixels for 100% precision
  if (liftCar) {
    const bottomPx = floorToBottomPx(renderedFloor);
    liftCar.style.bottom = `${bottomPx.toFixed(2)}px`;
  }

  syncMotorTelemetryUI();
  requestAnimationFrame(shaftAnimationLoop);
}
requestAnimationFrame(shaftAnimationLoop);

// Synchronize all 4 motors and physical telemetry with real simulation & Arduino state
function syncMotorTelemetryUI() {
  const isMoving = (currentLiftState.state === "MOVING");
  const dir = currentLiftState.direction || (currentLiftState.target_floor > currentLiftState.current_floor ? "UP" : (currentLiftState.target_floor < currentLiftState.current_floor ? "DOWN" : "IDLE"));
  const dispFloor = currentLiftState.display_floor || Math.round(currentLiftState.current_floor);
  const doorState = currentLiftState.state;
  const doorPct = currentLiftState.door_open_pct || 0.0;
  const isDoorActive = (doorState === "DOOR_OPENING" || doorState === "DOOR_OPEN" || doorState === "DOOR_CLOSING");

  // Single Operation Indicator LED (Pin 7)
  const isOperationActive = (currentLiftState.state !== "NORMAL" || currentLiftState.scan_active || isRequestLedActive);
  if (hwLedDoorActive) {
    if (isOperationActive) {
      hwLedDoorActive.classList.add("active");
    } else {
      hwLedDoorActive.classList.remove("active");
    }
  }

  // 1. M1: Hoist Motor (Driver 1: Pins 4 & 5 - OUT3/4) & Hoist Machine Drum Wheel
  if (hoistDrumWheel) {
    if (isMoving && dir === "UP") {
      hoistDrumWheel.className = "hoist-drum-wheel spinning-up";
    } else if (isMoving && dir === "DOWN") {
      hoistDrumWheel.className = "hoist-drum-wheel spinning-down";
    } else {
      hoistDrumWheel.className = "hoist-drum-wheel";
    }
  }

  if (m1Card && m1Status) {
    if (isMoving) {
      m1Card.style.background = "rgba(245, 158, 11, 0.18)";
      m1Card.style.borderColor = "rgba(245, 158, 11, 0.6)";
      m1Card.style.boxShadow = "0 0 14px rgba(245, 158, 11, 0.35)";
      const pinStr = (dir === "UP") ? "IN3=HIGH, IN4=LOW" : "IN3=LOW, IN4=HIGH";
      m1Status.textContent = `${dir} ➔ FL ${currentLiftState.target_floor} (${pinStr})`;
      m1Status.style.color = "#f59e0b";
    } else {
      m1Card.style.background = "rgba(255, 255, 255, 0.04)";
      m1Card.style.borderColor = "var(--border-subtle)";
      m1Card.style.boxShadow = "none";
      m1Status.textContent = "READY (Pins 4, 5)";
      m1Status.style.color = "#94a3b8";
    }
  }

  // 2. M2: Floor 3 Door Motor (Driver 2: Pins 10 & 11 - OUT3/4)
  if (m2Card && m2Status) {
    if (dispFloor === 3 && isDoorActive) {
      m2Card.style.background = "rgba(16, 185, 129, 0.18)";
      m2Card.style.borderColor = "rgba(16, 185, 129, 0.6)";
      m2Card.style.boxShadow = "0 0 14px rgba(16, 185, 129, 0.35)";
      const action = doorState === "DOOR_OPENING" ? "OPENING (IN3=H, IN4=L)" : (doorState === "DOOR_CLOSING" ? "CLOSING (IN3=L, IN4=H)" : "HOLD (100%)");
      m2Status.textContent = `${action} [${Math.round(doorPct)}%]`;
      m2Status.style.color = "#34d399";
    } else {
      m2Card.style.background = "rgba(255, 255, 255, 0.04)";
      m2Card.style.borderColor = "var(--border-subtle)";
      m2Card.style.boxShadow = "none";
      m2Status.textContent = "READY (Pins 10, 11)";
      m2Status.style.color = "#94a3b8";
    }
  }

  // 3. M3: Floor 5 Door Motor (Driver 1: Pins 2 & 3 - OUT1/2)
  if (m3Card && m3Status) {
    if (dispFloor === 5 && isDoorActive) {
      m3Card.style.background = "rgba(168, 85, 247, 0.18)";
      m3Card.style.borderColor = "rgba(168, 85, 247, 0.6)";
      m3Card.style.boxShadow = "0 0 14px rgba(168, 85, 247, 0.35)";
      const action = doorState === "DOOR_OPENING" ? "OPENING (IN1=H, IN2=L)" : (doorState === "DOOR_CLOSING" ? "CLOSING (IN1=L, IN2=H)" : "HOLD (100%)");
      m3Status.textContent = `${action} [${Math.round(doorPct)}%]`;
      m3Status.style.color = "#c084fc";
    } else {
      m3Card.style.background = "rgba(255, 255, 255, 0.04)";
      m3Card.style.borderColor = "var(--border-subtle)";
      m3Card.style.boxShadow = "none";
      m3Status.textContent = "READY (Pins 2, 3)";
      m3Status.style.color = "#94a3b8";
    }
  }

  // 4. M4: Floor 1 Door Motor (Driver 2: Pins 8 & 9 - OUT1/2)
  if (m4Card && m4Status) {
    if (dispFloor === 1 && isDoorActive) {
      m4Card.style.background = "rgba(56, 189, 248, 0.18)";
      m4Card.style.borderColor = "rgba(56, 189, 248, 0.6)";
      m4Card.style.boxShadow = "0 0 14px rgba(56, 189, 248, 0.35)";
      const action = doorState === "DOOR_OPENING" ? "OPENING (IN1=H, IN2=L)" : (doorState === "DOOR_CLOSING" ? "CLOSING (IN1=L, IN2=H)" : "HOLD (100%)");
      m4Status.textContent = `${action} [${Math.round(doorPct)}%]`;
      m4Status.style.color = "#38bdf8";
    } else {
      m4Card.style.background = "rgba(255, 255, 255, 0.04)";
      m4Card.style.borderColor = "var(--border-subtle)";
      m4Card.style.boxShadow = "none";
      m4Status.textContent = "READY (Pins 8, 9)";
      m4Status.style.color = "#94a3b8";
    }
  }

  // Central Gear Spinner & Telemetry Text
  if (isMoving) {
    if (motorStateBadge) {
      motorStateBadge.textContent = `HOIST ${dir} (FL ${dispFloor} ➔ ${currentLiftState.target_floor})`;
      motorStateBadge.className = "motor-state-badge active";
    }
    if (gearSpinner) {
      gearSpinner.className = (dir === "UP") ? "gear-spinner spinning-forward" : "gear-spinner spinning-reverse";
    }
    if (motorPwmVal) motorPwmVal.textContent = `M1: HOIST (${dir}) • Pins 4/5`;
    if (motorDirVal) motorDirVal.textContent = (dir === "UP") ? "TRAVELING UP (IN3=H, IN4=L)" : "TRAVELING DOWN (IN3=L, IN4=H)";
    if (motorApertureVal) motorApertureVal.textContent = "CLOSED (0%)";
  } else if (isDoorActive) {
    const motorName = dispFloor === 3 ? "M2: FL 3 DOOR (Pins 10, 11)" : (dispFloor === 5 ? "M3: FL 5 DOOR (Pins 2, 3)" : "M4: FL 1 DOOR (Pins 8, 9)");
    if (motorStateBadge) {
      motorStateBadge.textContent = `${doorState.replace("DOOR_", "")} (FL ${dispFloor})`;
      motorStateBadge.className = "motor-state-badge active";
    }
    if (gearSpinner) {
      gearSpinner.className = doorState === "DOOR_OPENING" ? "gear-spinner spinning-forward" : (doorState === "DOOR_CLOSING" ? "gear-spinner spinning-reverse" : "gear-spinner");
    }
    if (motorPwmVal) motorPwmVal.textContent = motorName;
    if (motorDirVal) {
      motorDirVal.textContent = doorState === "DOOR_OPENING" ? "OPENING (FORWARD)" : (doorState === "DOOR_CLOSING" ? "CLOSING (REVERSE)" : "HOLDING OPEN (DWELL)");
    }
    if (motorApertureVal) motorApertureVal.textContent = `${Math.round(doorPct)}% (${doorState.replace("DOOR_", "")})`;
  } else {
    if (motorStateBadge) {
      motorStateBadge.textContent = "STANDBY (READY)";
      motorStateBadge.className = "motor-state-badge";
    }
    if (gearSpinner) gearSpinner.className = "gear-spinner";
    if (motorPwmVal) motorPwmVal.textContent = "ALL MOTORS STOPPED (STANDBY)";
    if (motorDirVal) motorDirVal.textContent = "IDLE (PARKED AT FLOOR 1)";
    if (motorApertureVal) motorApertureVal.textContent = "CLOSED (0%)";
  }
}

// Initialize Building Floors (Scaled strictly to real cm measurements: 12, 13, 12, 14, 17cm)
const FLOOR_HEIGHTS_CM = {
  1: 12.0,
  2: 13.0,
  3: 12.0,
  4: 14.0,
  5: 17.0
};
const TOTAL_BUILDING_HEIGHT_CM = 68.0;

function initShaftFloors() {
  if (!floorsContainer) return;
  floorsContainer.innerHTML = "";
  const floorLabels = {
    5: "Floor 5: Top Floor (17cm)",
    4: "Floor 4: Upper Level (14cm)",
    3: "★ Floor 3: MAIN STATION (12cm)",
    2: "Floor 2: Lower Level (13cm)",
    1: "Floor 1: Ground Floor / Lobby (12cm)"
  };

  for (let f = MAX_FLOOR; f >= MIN_FLOOR; f--) {
    const row = document.createElement("div");
    row.className = "floor-row";
    row.id = `floorRow_${f}`;
    const pct = ((FLOOR_HEIGHTS_CM[f] / TOTAL_BUILDING_HEIGHT_CM) * 100).toFixed(2);
    row.style.height = `${pct}%`;
    row.style.flex = "none";
    if (f === 3) {
      row.classList.add("floor-esp32-station");
    }
    row.innerHTML = `
      <div class="floor-indicator-tag">
        <div class="floor-num-badge">${f}</div>
        <div class="floor-beacon"></div>
      </div>
      <div class="floor-label-sub">${floorLabels[f] || `Level ${f}`}</div>
    `;
    floorsContainer.appendChild(row);
  }
}

// Initialize Quick Floor Matrix Buttons (1 through 5)
function initFloorButtons() {
  if (!floorBtnGrid) return;
  floorBtnGrid.innerHTML = "";
  for (let f = MIN_FLOOR; f <= MAX_FLOOR; f++) {
    const btn = document.createElement("button");
    btn.className = "floor-select-btn";
    btn.id = `btnSelectFloor_${f}`;
    btn.textContent = `L${f}`;
    btn.title = `Request Floor ${f}`;
    btn.addEventListener("click", () => sendLiftRequest(f));
    floorBtnGrid.appendChild(btn);
  }
}

// Update Lift Visualization & Telemetry
function updateUI(data) {
  if (!data) return;
  const prevState = currentLiftState.state;
  currentLiftState = { ...currentLiftState, ...data };

  // 1. Shaft Car Position (1.0 to 5.0) - Handled continuously by 60fps RAF shaftAnimationLoop
  // Synchronization of motors and mechanical components
  syncMotorTelemetryUI();

  // 2. Car Displays & Direction Arrow
  const dispFloor = currentLiftState.display_floor || Math.round(currentLiftState.current_floor);
  if (carDisplayFloor) carDisplayFloor.textContent = dispFloor;
  if (shaftCarFloorVal) shaftCarFloorVal.textContent = dispFloor;

  if (carDirectionArrow) {
    if (currentLiftState.target_floor !== null) {
      if (currentLiftState.current_floor < currentLiftState.target_floor - 0.05) {
        carDirectionArrow.textContent = "▲";
        carDirectionArrow.className = "car-direction-arrow moving-up";
      } else if (currentLiftState.current_floor > currentLiftState.target_floor + 0.05) {
        carDirectionArrow.textContent = "▼";
        carDirectionArrow.className = "car-direction-arrow moving-down";
      } else {
        carDirectionArrow.textContent = "■";
        carDirectionArrow.className = "car-direction-arrow";
      }
    } else {
      carDirectionArrow.textContent = "■";
      carDirectionArrow.className = "car-direction-arrow";
    }
  }

  // 3. Sliding Bi-parting Doors & Aperture Meter
  const pct = currentLiftState.door_open_pct || 0.0;
  if (doorLeft && doorRight) {
    const slideOffset = (pct / 100.0) * 100;
    doorLeft.style.transform = `translateX(-${slideOffset}%)`;
    doorRight.style.transform = `translateX(${slideOffset}%)`;
  }

  if (doorMeterBar) doorMeterBar.style.width = `${pct}%`;
  if (doorApertureVal) {
    if (pct <= 0) doorApertureVal.textContent = "0% (Closed)";
    else if (pct >= 100) doorApertureVal.textContent = "100% (Fully Open)";
    else doorApertureVal.textContent = `${Math.round(pct)}% (Moving)`;
  }

  // 4. Telemetry Deck Values
  if (mainStateBadge) {
    mainStateBadge.textContent = currentLiftState.state;
    mainStateBadge.setAttribute("data-state", currentLiftState.state);
  }
  if (carStateMini) carStateMini.textContent = currentLiftState.state;

  if (telCurrentFloor) telCurrentFloor.textContent = currentLiftState.current_floor.toFixed(2);
  if (telTargetFloor) {
    telTargetFloor.textContent = currentLiftState.target_floor !== null ? currentLiftState.target_floor : "--";
  }

  if (telMode) {
    if (currentLiftState.state === "NORMAL") {
      telMode.textContent = "Idle Station (Ready)";
      telMode.style.color = "#a5b4fc";
    } else {
      const destStr = currentLiftState.destination_floor ? ` ➔ FL ${currentLiftState.destination_floor}` : "";
      telMode.textContent = `Servicing FL ${currentLiftState.target_floor}${destStr}`;
      telMode.style.color = "var(--primary)";
    }
  }

  if (telRequestId) {
    if (currentLiftState.request_id) {
      telRequestId.textContent = currentLiftState.request_id;
      if (btnCopyReqId) btnCopyReqId.style.display = "flex";
    } else {
      telRequestId.textContent = "None (Simulation Idle)";
      if (btnCopyReqId) btnCopyReqId.style.display = "none";
    }
  }

  if (webhookUrlDisplay && currentLiftState.backend_arrived_url) {
    webhookUrlDisplay.textContent = currentLiftState.backend_arrived_url;
  }

  // 5. Highlight Active Floor Row
  document.querySelectorAll(".floor-row").forEach((el) => {
    el.classList.remove("active-floor", "car-arrived");
  });
  const activeRow = document.getElementById(`floorRow_${dispFloor}`);
  if (activeRow) {
    activeRow.classList.add("active-floor");
    if (currentLiftState.state === "ARRIVED" || currentLiftState.state.startsWith("DOOR_")) {
      activeRow.classList.add("car-arrived");
    }
  }

  // 6. Highlight Matrix Button
  document.querySelectorAll(".floor-select-btn").forEach((btn) => btn.classList.remove("active"));
  const matrixBtn = document.getElementById(`btnSelectFloor_${dispFloor}`);
  if (matrixBtn) matrixBtn.classList.add("active");

  // 7. Door Complete Button Enablement
  if (btnDoorComplete) {
    const canComplete = currentLiftState.state === "DOOR_OPEN" && currentLiftState.request_id;
    btnDoorComplete.disabled = !canComplete;
    btnDoorComplete.classList.toggle("pulse-ready", canComplete);
  }

  // 8. Auto-send DOOR_COMPLETE if enabled and not already running
  if (currentLiftState.state === "DOOR_OPEN" && prevState !== "DOOR_OPEN") {
    if (autoDoorCompleteToggle && autoDoorCompleteToggle.checked) {
      if (autoDoorTimer) clearTimeout(autoDoorTimer);
      autoDoorTimer = setTimeout(() => {
        sendDoorComplete(currentLiftState.request_id);
      }, 3500);
    }
  }

  // 9. Motors & mechanical hoist status are synchronized in real-time by syncMotorTelemetryUI()

  // 10. Turn OFF Request LEDs when Operation Completes (returns to NORMAL)
  if (currentLiftState.state === "NORMAL" && prevState !== "NORMAL") {
    resetSwitchIndicators();
  }

  // 11. Sync Passenger Verification & Scan UI from Backend/WebSocket
  if (currentLiftState.human_verified) {
    if (hwAiStatusBadge) {
      hwAiStatusBadge.textContent = "HUMAN VERIFIED";
      hwAiStatusBadge.classList.add("pulse-ready");
    }
    if (passengerPresenceLabel) {
      passengerPresenceLabel.textContent = "Passenger Verified! En route / arrived at FL 3";
      passengerPresenceLabel.classList.add("active");
    }
    if (hwLedAuth) hwLedAuth.classList.add("active");
  } else if (currentLiftState.scan_active) {
    const dest = currentLiftState.destination_floor || currentLiftState.scan_target || selectedFloor3Dest || 1;
    updateSwitchIndicators(dest, true);
    if (hwAiStatusBadge) {
      hwAiStatusBadge.textContent = "LIFT MOVING ➔ FL 3";
      hwAiStatusBadge.classList.add("pulse-ready");
    }
    if (passengerPresenceLabel) {
      passengerPresenceLabel.textContent = "Lift Moving to FL 3 • AI Watching for Human...";
      passengerPresenceLabel.classList.add("active");
    }
    // Automatically start live camera when button is pressed (Arduino UNO, Web, or API)
    if (!isDashCamActive && typeof startDashCamera === 'function') {
      startDashCamera();
    }
  }

  // 12. Sync Arduino UNO USB Hardware Status
  const portBadge = document.getElementById("arduinoPortBadge");
  if (portBadge) {
    if (currentLiftState.uno_connected) {
      portBadge.textContent = `Arduino Uno (${currentLiftState.uno_port || 'Connected'}) • Active Sync`;
      portBadge.style.color = "#34d399";
    } else {
      portBadge.textContent = `Arduino Uno • Scanning USB COM Ports...`;
      portBadge.style.color = "#94a3b8";
    }
  }
}

// Append Event to Diagnostic Terminal
function addLogEntry(log) {
  if (!log || !eventFeedTerminal) return;
  totalLogCount++;
  if (logCountBadge) logCountBadge.textContent = `${totalLogCount} events`;

  const item = document.createElement("div");
  item.className = "terminal-log-item";

  const lvl = log.level || "INFO";
  const stateVal = log.state || currentLiftState.state;
  const time = log.timestamp || new Date().toLocaleTimeString();
  const eventType = log.event_type || "EVENT";

  item.innerHTML = `
    <div class="log-meta">
      <span class="log-timestamp">${time}</span>
      <span class="log-level ${lvl.toLowerCase()}">${lvl}</span>
      <span class="log-type-tag">[${eventType}]</span>
      <span class="log-state-tag">${stateVal}</span>
    </div>
    <div class="log-msg">${escapeHtml(log.message || "")}</div>
    ${log.extra && Object.keys(log.extra).length > 0 ? `<pre class="log-extra">${escapeHtml(JSON.stringify(log.extra, null, 2))}</pre>` : ""}
  `;

  eventFeedTerminal.appendChild(item);
  eventFeedTerminal.scrollTop = eventFeedTerminal.scrollHeight;

  while (eventFeedTerminal.children.length > 150) {
    eventFeedTerminal.removeChild(eventFeedTerminal.firstChild);
  }
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.innerText = text;
  return div.innerHTML;
}

// WebSocket Connection Management
function connectWebSocket() {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  if (wsStatusChip) {
    wsStatusChip.className = "status-chip connecting";
    wsStatusText.textContent = "Connecting WS...";
  }

  try {
    ws = new WebSocket(wsUrl);
  } catch (err) {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    if (wsStatusChip) {
      wsStatusChip.className = "status-chip connected";
      wsStatusText.textContent = "Live WS Connected";
    }
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === "STATUS_UPDATE" || msg.type === "INITIAL_STATE") {
        updateUI(msg.data);
      }
      if (msg.event && (msg.event.event === "SCAN_STARTED" || msg.event.event === "SWITCH_PRESSED")) {
        if (!isDashCamActive && typeof startDashCamera === 'function') {
          startDashCamera();
        }
      }
      if (msg.logs && Array.isArray(msg.logs)) {
        msg.logs.forEach(addLogEntry);
      }
    } catch (e) {
      console.error("WS message parse error:", e);
    }
  };

  ws.onclose = () => {
    if (wsStatusChip) {
      wsStatusChip.className = "status-chip connecting";
      wsStatusText.textContent = "WS Disconnected (Reconnecting...)";
    }
    scheduleReconnect();
  };

  ws.onerror = () => {
    if (ws) ws.close();
  };
}

function scheduleReconnect() {
  if (!reconnectTimer) {
    reconnectTimer = setTimeout(() => {
      connectWebSocket();
    }, 2000);
  }
}

// HTTP API Invocations

async function sendLiftRequest(floor = 3, destinationFloor = null, source = "MANUAL") {
  // Automatically start live camera on button press
  if (!isDashCamActive && typeof startDashCamera === 'function') {
    startDashCamera();
  }
  const requestId = crypto.randomUUID();
  const payload = {
    request: "LIFT_REQUEST",
    floor: floor,
    destination_floor: destinationFloor,
    source: source,
    request_id: requestId
  };

  const destStr = destinationFloor ? ` ➔ Dest: Floor ${destinationFloor}` : "";
  addLogEntry({
    timestamp: new Date().toLocaleTimeString(),
    event_type: "HTTP_POST_SEND",
    level: "INFO",
    message: `Sending POST /lift/request for Floor ${floor}${destStr} from [${source}] (UUID: ${requestId})`,
    extra: payload
  });

  try {
    const res = await fetch("/lift/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (res.ok) {
      addLogEntry({
        timestamp: new Date().toLocaleTimeString(),
        event_type: "HTTP_POST_SUCCESS",
        level: "INFO",
        message: `HTTP 200: Lift request accepted for Floor ${floor}!`,
        extra: data
      });
      return true;
    } else {
      addLogEntry({
        timestamp: new Date().toLocaleTimeString(),
        event_type: "HTTP_POST_REJECTED",
        level: "WARNING",
        message: `HTTP ${res.status}: ${data.message || 'Request rejected'}`,
        extra: data
      });
      return false;
    }
  } catch (err) {
    addLogEntry({
      timestamp: new Date().toLocaleTimeString(),
      event_type: "HTTP_POST_ERROR",
      level: "ERROR",
      message: `Failed to send /lift/request: ${err.message}`
    });
    return false;
  }
}

async function sendDoorComplete(customReqId = null) {
  const reqId = customReqId || currentLiftState.request_id;
  if (!reqId) return;

  const payload = {
    event: "DOOR_COMPLETE",
    request_id: reqId
  };

  addLogEntry({
    timestamp: new Date().toLocaleTimeString(),
    event_type: "HTTP_POST_SEND",
    level: "INFO",
    message: `Sending POST /door/complete (UUID: ${reqId})`,
    extra: payload
  });

  try {
    const res = await fetch("/door/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });

    const data = await res.json();
    if (res.ok) {
      addLogEntry({
        timestamp: new Date().toLocaleTimeString(),
        event_type: "HTTP_POST_SUCCESS",
        level: "INFO",
        message: `HTTP 200: Door complete accepted! Virtual door closing.`,
        extra: data
      });
    } else {
      addLogEntry({
        timestamp: new Date().toLocaleTimeString(),
        event_type: "HTTP_POST_REJECTED",
        level: "WARNING",
        message: `HTTP ${res.status}: ${data.message || 'Door complete rejected'}`,
        extra: data
      });
    }
  } catch (err) {
    addLogEntry({
      timestamp: new Date().toLocaleTimeString(),
      event_type: "HTTP_POST_ERROR",
      level: "ERROR",
      message: `Failed to send /door/complete: ${err.message}`
    });
  }
}

/* ========================================================================== */
/* Direct Hardware Switch & Passenger Verification Logic                      */
/* LED turns ON when button is pressed, STAYS ON until operation completes    */
/* ========================================================================== */

function updateSwitchIndicators(dest, isActive) {
  selectedFloor3Dest = dest;
  isRequestLedActive = isActive;
  activeRequestDirection = dest === 1 ? "DOWN" : "UP";

  if (dest === 1) {
    // DOWN Request (Floor 3 -> Ground Floor 1)
    if (btnSwitchDown) btnSwitchDown.classList.toggle("active", isActive);
    if (btnSwitchUp) btnSwitchUp.classList.remove("active");
    if (ledDownInd) ledDownInd.classList.toggle("active", isActive);
    if (ledUpInd) ledUpInd.classList.remove("active");
    if (hwLedDownActive) hwLedDownActive.classList.toggle("active", isActive);
    if (hwLedUpActive) hwLedUpActive.classList.remove("active");
    if (activeSwitchBadge) {
      activeSwitchBadge.textContent = isActive ? "PIN 2: FL 3 ➔ GND (BLUE LED ON)" : "IDLE";
      activeSwitchBadge.classList.toggle("active", isActive);
    }
  } else if (dest === 5) {
    // UP Request (Floor 3 -> Floor 5)
    if (btnSwitchDown) btnSwitchDown.classList.remove("active");
    if (btnSwitchUp) btnSwitchUp.classList.toggle("active", isActive);
    if (ledDownInd) ledDownInd.classList.remove("active");
    if (ledUpInd) ledUpInd.classList.toggle("active", isActive);
    if (hwLedDownActive) hwLedDownActive.classList.remove("active");
    if (hwLedUpActive) hwLedUpActive.classList.toggle("active", isActive);
    if (activeSwitchBadge) {
      activeSwitchBadge.textContent = isActive ? "PIN 3: FL 3 ➔ FL 5 (AMBER LED ON)" : "IDLE";
      activeSwitchBadge.classList.toggle("active", isActive);
    }
  }
}

function resetSwitchIndicators() {
  isRequestLedActive = false;
  activeRequestDirection = null;
  dashVerifySent = false;
  if (btnSwitchDown) btnSwitchDown.classList.remove("active");
  if (btnSwitchUp) btnSwitchUp.classList.remove("active");
  if (ledDownInd) ledDownInd.classList.remove("active");
  if (ledUpInd) ledUpInd.classList.remove("active");
  if (hwLedDownActive) hwLedDownActive.classList.remove("active");
  if (hwLedUpActive) hwLedUpActive.classList.remove("active");
  if (activeSwitchBadge) {
    activeSwitchBadge.textContent = "IDLE";
    activeSwitchBadge.classList.remove("active");
  }
  if (hwAiStatusBadge) {
    hwAiStatusBadge.textContent = "READY";
    hwAiStatusBadge.classList.remove("pulse-ready");
  }
  if (passengerPresenceLabel) {
    passengerPresenceLabel.textContent = "Waiting for Button Press";
    passengerPresenceLabel.classList.remove("active");
  }
  if (hwLedAuth) hwLedAuth.classList.remove("active");
  if (dashGlow) {
    dashGlow.style.borderColor = "transparent";
    dashGlow.style.boxShadow = "none";
  }
}

async function triggerHardwareSwitch(dest) {
  // Light up the corresponding indicator LED (stays ON until operation complete)
  updateSwitchIndicators(dest, true);

  if (hwAiStatusBadge) {
    hwAiStatusBadge.textContent = "LIFT MOVING ➔ FL 3";
    hwAiStatusBadge.classList.add("pulse-ready");
  }
  if (passengerPresenceLabel) {
    passengerPresenceLabel.textContent = `Lift Moving to FL 3 • AI Scanning for Human...`;
    passengerPresenceLabel.classList.add("active");
  }

  addLogEntry({
    timestamp: new Date().toLocaleTimeString(),
    event_type: "SWITCH_PRESSED",
    level: "INFO",
    message: `Floor 3 Button Pressed! Destination: ${dest === 1 ? 'Ground Floor 1 (Blue LED ON)' : 'Floor 5 (Amber LED ON)'}. Lift begins moving slowly to Floor 3, Vision AI live scanning activated!`
  });

  // Automatically start live camera on dashboard if available and not yet running
  if (!isDashCamActive && typeof startDashCamera === 'function') {
    startDashCamera();
  }

  try {
    const res = await fetch("/ai/trigger-switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ destination_floor: dest })
    });
    const data = await res.json();
    if (res.ok) {
      addLogEntry({
        timestamp: new Date().toLocaleTimeString(),
        event_type: "LIFT_DISPATCHED",
        level: "INFO",
        message: `Lift dispatched immediately to Floor 3! Status: ${data.status}`
      });
    }
  } catch (err) {
    console.error("Failed to trigger switch:", err);
  }
}

let presenceHeartbeatTimer = null;

function startPresenceHeartbeat() {
  if (presenceHeartbeatTimer) return;
  presenceHeartbeatTimer = setInterval(async () => {
    const isEnRouteOrArrived = (currentLiftState.state === 'REQUESTED' || currentLiftState.state === 'MOVING' || currentLiftState.state === 'ARRIVED');
    // FIX 2: Only send presence during the pickup leg — NOT during passenger trip or empty return.
    const isPickupLeg = isEnRouteOrArrived
      && !currentLiftState.returning_to_ground
      && !currentLiftState.is_passenger_trip
      && !['DOOR_OPENING', 'DOOR_OPEN', 'DOOR_CLOSING', 'NORMAL'].includes(currentLiftState.state);
    if (isPickupLeg) {
      try {
        await fetch('/ai/presence', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ present: true })
        });
      } catch (e) {}
    } else {
      stopPresenceHeartbeat();
    }
  }, 700);
}

function stopPresenceHeartbeat() {
  if (presenceHeartbeatTimer) {
    clearInterval(presenceHeartbeatTimer);
    presenceHeartbeatTimer = null;
  }
}

async function verifyPassengerYes(customDest = null) {
  // GUARD: Never verify during passenger trip or empty return — only during pickup leg
  if (currentLiftState.returning_to_ground || currentLiftState.is_passenger_trip) return;

  let targetDest = null;
  if (typeof customDest === "number" && (customDest === 1 || customDest === 5)) {
    targetDest = customDest;
  } else {
    targetDest = (currentLiftState.destination_floor === 1 || currentLiftState.destination_floor === 5)
      ? currentLiftState.destination_floor
      : ((currentLiftState.scan_target === 1 || currentLiftState.scan_target === 5)
        ? currentLiftState.scan_target
        : (selectedFloor3Dest || 1));
  }
  selectedFloor3Dest = targetDest;
  startPresenceHeartbeat();

  if (hwAiStatusBadge) {
    hwAiStatusBadge.textContent = "HUMAN VERIFIED (YES)";
    hwAiStatusBadge.classList.add("pulse-ready");
  }
  if (passengerPresenceLabel) {
    passengerPresenceLabel.textContent = "Human Detected! Lift proceeding with pickup at FL 3";
    passengerPresenceLabel.classList.add("active");
  }
  if (hwLedAuth) hwLedAuth.classList.add("active");
  if (dashGlow) {
    dashGlow.style.borderColor = "#10b981";
    dashGlow.style.boxShadow = "inset 0 0 20px rgba(16, 185, 129, 0.4)";
  }

  const destName = targetDest === 1 ? "Ground Floor (Floor 1)" : "Floor 5 (Top)";
  addLogEntry({
    timestamp: new Date().toLocaleTimeString(),
    event_type: "HUMAN_VERIFIED_YES",
    level: "INFO",
    message: `Passenger Verified (YES)! Lift en route / at Floor 3 (Destination: ${destName}). Indicator LED remains ON!`
  });

  try {
    await fetch("/ai/verify-human", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        verified: true,
        destination_floor: targetDest,
        floor: 3,
        source: "DASHBOARD_VISION_AI"
      })
    });
  } catch (err) {
    console.error("Failed to verify human passenger:", err);
  }
}

async function verifyPassengerNo() {
  stopPresenceHeartbeat();
  if (hwAiStatusBadge) {
    hwAiStatusBadge.textContent = "REQUEST CANCELED";
    hwAiStatusBadge.classList.remove("pulse-ready");
  }
  if (passengerPresenceLabel) {
    passengerPresenceLabel.textContent = "No Human - Light OFF";
    passengerPresenceLabel.classList.remove("active");
  }
  if (hwLedAuth) hwLedAuth.classList.remove("active");
  if (dashGlow) {
    dashGlow.style.borderColor = "transparent";
    dashGlow.style.boxShadow = "none";
  }

  // Extinguish indicator LEDs when request is canceled
  resetSwitchIndicators();

  addLogEntry({
    timestamp: new Date().toLocaleTimeString(),
    event_type: "REQUEST_CANCELED_NO",
    level: "WARNING",
    message: "Passenger NOT detected within timeout. Request canceled, indicator LED turned OFF. System IDLE."
  });

  try {
    await fetch("/ai/verify-human", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verified: false })
    });
  } catch (err) {
    console.error("Failed to cancel scan:", err);
  }

  setTimeout(() => {
    if (hwAiStatusBadge) hwAiStatusBadge.textContent = "READY";
    if (passengerPresenceLabel) passengerPresenceLabel.textContent = "Waiting for Button Press";
  }, 2000);
}

/* ========================================================================== */
/* Live Vision AI Camera Engine (TensorFlow.js COCO-SSD on Dashboard)         */
/* ========================================================================== */
let dashCocoModel = null;
let isDashCamActive = false;
let dashVerifySent = false;
let dashLastTime = performance.now();
let isDashDetecting = false;

async function initDashAiModel() {
  if (dashCocoModel) return;
  let attempts = 0;
  while (!window.cocoSsd && attempts < 25) {
    await new Promise(r => setTimeout(r, 200));
    attempts++;
  }
  if (!window.cocoSsd) {
    console.warn("TensorFlow COCO-SSD not available on window. Running in smart presence detection mode.");
    if (dashDetectionLabel) dashDetectionLabel.textContent = "Live Webcam Active (Direct Presence Detection)";
    return;
  }
  try {
    if (dashDetectionLabel) dashDetectionLabel.textContent = "Loading TensorFlow.js model...";
    dashCocoModel = await cocoSsd.load();
    if (dashDetectionLabel) dashDetectionLabel.textContent = "AI Model Ready • Continuous Live Camera";
    console.log("TensorFlow.js COCO-SSD loaded on dashboard!");
  } catch (e) {
    console.warn("Could not load COCO-SSD model on dashboard:", e);
    if (dashDetectionLabel) dashDetectionLabel.textContent = "Live Webcam Active (Direct Presence Detection)";
  }
}

async function startDashCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (camPlaceholder) {
      camPlaceholder.style.display = "flex";
      camPlaceholder.innerHTML = `<span style="color:#f59e0b;">⚠️ Camera not supported in this browser</span><div style="font-size:11px; margin-top:4px;">Please open on http://localhost:8000 for webcam permissions.</div>`;
    }
    return;
  }
  try {
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: "user" },
        audio: false
      });
    } catch (e1) {
      console.warn("Retrying getUserMedia with basic constraints:", e1);
      stream = await navigator.mediaDevices.getUserMedia({
        video: true,
        audio: false
      });
    }

    if (dashWebcam) {
      dashWebcam.srcObject = stream;
      dashWebcam.muted = true;
      dashWebcam.setAttribute("muted", "");
      dashWebcam.setAttribute("playsinline", "");
      dashWebcam.setAttribute("autoplay", "");
      dashWebcam.style.display = "block";
      try {
        await dashWebcam.play();
      } catch (playErr) {
        console.warn("Webcam play deferred, will play on click:", playErr);
      }
    }
    isDashCamActive = true;
    if (camPlaceholder) camPlaceholder.style.display = "none";
    if (dashCamHud) {
      dashCamHud.style.display = "block";
      dashCamHud.textContent = "Laptop Webcam Live • Continuous";
      dashCamHud.style.color = "#34d399";
    }
    if (dashCamBtnText) dashCamBtnText.textContent = "Disable Web Camera";
    if (btnToggleDashCam) btnToggleDashCam.classList.add("active");

    initDashAiModel();
    dashLastTime = performance.now();
    requestAnimationFrame(dashVisionLoop);
  } catch (err) {
    console.warn("Could not start dashboard camera:", err);
    if (camPlaceholder) {
      camPlaceholder.style.display = "flex";
      camPlaceholder.innerHTML = `<span style="color:#ef4444;">⚠️ Camera: ${err.message || 'Access Denied'}</span><div style="font-size:11px; margin-top:4px; color:#cbd5e1;">Click <b>"Enable Web Camera"</b> below or grant browser permissions!</div>`;
    }
  }
}

function stopDashCamera() {
  isDashCamActive = false;
  if (dashWebcam && dashWebcam.srcObject) {
    try {
      dashWebcam.srcObject.getTracks().forEach(t => t.stop());
    } catch (e) {}
    dashWebcam.srcObject = null;
  }
  if (dashWebcam) dashWebcam.style.display = "none";
  if (camPlaceholder) camPlaceholder.style.display = "block";
  if (dashCamHud) dashCamHud.style.display = "none";
  if (dashCamBtnText) dashCamBtnText.textContent = "Enable Web Camera";
  if (btnToggleDashCam) btnToggleDashCam.classList.remove("active");
  if (dashOverlay) {
    const ctx = dashOverlay.getContext("2d");
    ctx.clearRect(0, 0, dashOverlay.width, dashOverlay.height);
  }
}

let consecutiveAbsentFrames = 0;

async function dashVisionLoop() {
  if (!isDashCamActive || !dashWebcam || dashWebcam.readyState < 2) {
    if (isDashCamActive) requestAnimationFrame(dashVisionLoop);
    return;
  }

  // Prevent re-entrant detection if AI model inference is in-flight
  if (isDashDetecting) {
    if (isDashCamActive) requestAnimationFrame(dashVisionLoop);
    return;
  }

  const now = performance.now();
  const dt = now - dashLastTime;
  dashLastTime = now;

  // Scanning progress accumulation when station call is active - DECLARED AT TOP
  const isScanning = (currentLiftState.scan_active || (currentLiftState.target_floor === 3 && !currentLiftState.human_verified));

  // Match overlay dimensions
  if (dashOverlay.width !== dashWebcam.videoWidth || dashOverlay.height !== dashWebcam.videoHeight) {
    dashOverlay.width = dashWebcam.videoWidth || 640;
    dashOverlay.height = dashWebcam.videoHeight || 480;
  }
  const ctx = dashOverlay.getContext("2d");
  ctx.clearRect(0, 0, dashOverlay.width, dashOverlay.height);

  let personFound = false;
  let bestScore = 0;

  if (dashCocoModel) {
    isDashDetecting = true;
    try {
      const predictions = await dashCocoModel.detect(dashWebcam);
      for (const pred of predictions) {
        if (pred.class.toLowerCase() === "person" && pred.score >= 0.35) {
          personFound = true;
          if (pred.score > bestScore) bestScore = pred.score;

          const [x, y, w, h] = pred.bbox;
          ctx.strokeStyle = "#10b981";
          ctx.lineWidth = 3;
          ctx.strokeRect(x, y, w, h);

          ctx.fillStyle = "#10b981";
          ctx.fillRect(x, Math.max(0, y - 22), 120, 22);
          ctx.fillStyle = "#ffffff";
          ctx.font = "bold 12px JetBrains Mono, monospace";
          ctx.fillText(`Person ${(pred.score * 100).toFixed(0)}%`, x + 4, Math.max(15, y - 6));
        }
      }
    } catch (e) {
      console.warn("AI detection frame error:", e);
    } finally {
      isDashDetecting = false;
    }
  } else if (!dashCocoModel && dashWebcam.videoWidth > 0) {
    // FIX 1: AI model not loaded — do NOT fake person detection. Show standby overlay only.
    personFound = false;
    bestScore = 0;
    ctx.strokeStyle = "#38bdf8";
    ctx.lineWidth = 2;
    ctx.strokeRect(30, 20, Math.max(100, dashOverlay.width - 60), Math.max(80, dashOverlay.height - 40));
    ctx.fillStyle = "#38bdf8";
    ctx.fillRect(30, 0, 200, 20);
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 11px JetBrains Mono, monospace";
    ctx.fillText("Webcam Ready • AI Model Loading...", 35, 14);
  }

  if (personFound) {
    consecutiveAbsentFrames = 0;
    if (isScanning && !currentLiftState.human_verified) {
      if (dashCamHud) {
        dashCamHud.textContent = `Person Detected: ${(bestScore * 100).toFixed(0)}% • Verifying...`;
        dashCamHud.style.color = "#34d399";
      }
      if (dashDetectionLabel) dashDetectionLabel.textContent = "👤 Person Present — Verifying Lift Call...";
      if (dashGlow) {
        dashGlow.style.borderColor = "#10b981";
        dashGlow.style.boxShadow = "inset 0 0 15px rgba(16, 185, 129, 0.3)";
      }
      if (dashGaugeFill) dashGaugeFill.style.width = "100%";
      if (dashGaugeTimer) dashGaugeTimer.textContent = "👤 Verified";
      if (!dashVerifySent) {
        dashVerifySent = true;
        const targetDest = currentLiftState.destination_floor || currentLiftState.scan_target || selectedFloor3Dest || 1;
        verifyPassengerYes(targetDest);
      }
    } else if (currentLiftState.human_verified) {
      // GUARD: Only keep presence alive during pickup leg, not during delivery or return
      if (!currentLiftState.returning_to_ground && !currentLiftState.is_passenger_trip) {
        startPresenceHeartbeat();
      } else {
        stopPresenceHeartbeat();
      }
      if (dashCamHud) {
        dashCamHud.textContent = `Passenger Verified • Tracking Live (${(bestScore * 100).toFixed(0)}%)`;
        dashCamHud.style.color = "#34d399";
      }
      if (dashDetectionLabel) dashDetectionLabel.textContent = "Passenger Verified! Lift servicing trip";
      if (dashGaugeFill) dashGaugeFill.style.width = "100%";
      if (dashGaugeTimer) dashGaugeTimer.textContent = "✅ Verified";
    } else {
      if (dashCamHud) {
        dashCamHud.textContent = `Camera Live (Continuous) • Person in Frame (${(bestScore * 100).toFixed(0)}%) [Waiting for Button]`;
        dashCamHud.style.color = "#60a5fa";
      }
      if (dashDetectionLabel) dashDetectionLabel.textContent = "Camera Live: Person visible • Press D12 (Down) or D13 (Up) button to call lift";
      if (dashGaugeFill) dashGaugeFill.style.width = "0%";
      if (dashGaugeTimer) dashGaugeTimer.textContent = "Standby (Press Button)";
      if (dashGlow) {
        dashGlow.style.borderColor = "transparent";
        dashGlow.style.boxShadow = "none";
      }
    }
  } else {
    consecutiveAbsentFrames++;
    // Only stop heartbeat after sustained absence (>= 15 consecutive absent frames)
    if (consecutiveAbsentFrames > 15) {
      if (dashCocoModel) {
        stopPresenceHeartbeat();
      }
    }
    if (isScanning && !currentLiftState.human_verified) {
      if (dashCamHud) {
        dashCamHud.textContent = "Button Pressed! Watching for Person (Live)...";
        dashCamHud.style.color = "#f59e0b";
      }
      // FIX 3: Do NOT reset dashVerifySent on absent frames mid-trip.
      // It only resets in resetSwitchIndicators() when lift returns to NORMAL state.
      if (dashDetectionLabel) dashDetectionLabel.textContent = "Button Pressed! Watching live for person... Please stand in front of camera";
      if (dashGaugeFill) dashGaugeFill.style.width = "0%";
      if (dashGaugeTimer) dashGaugeTimer.textContent = "Watching...";
      if (dashGlow) {
        dashGlow.style.borderColor = "transparent";
        dashGlow.style.boxShadow = "none";
      }
    } else {
      if (dashCamHud) {
        dashCamHud.textContent = "Camera Live (Continuous) • Standby";
        dashCamHud.style.color = "#94a3b8";
      }
      dashVerifySent = false;
      if (dashDetectionLabel) dashDetectionLabel.textContent = "Camera Live (Continuous) • Standby (Press Button to Call Lift)";
      if (dashGaugeFill) dashGaugeFill.style.width = "0%";
      if (dashGaugeTimer) dashGaugeTimer.textContent = "Standby";
      if (dashGlow) {
        dashGlow.style.borderColor = "transparent";
        dashGlow.style.boxShadow = "none";
      }
    }
  }

  if (isDashCamActive) {
    requestAnimationFrame(dashVisionLoop);
  }
}

/* ========================================================================== */
/* Real-Life Gear Motor & Motor Driver (L298N) Simulation                     */
/* ========================================================================== */

function runSmoothGearMotorCycle() {
  if (isRealLifeMotorRunning) return;
  isRealLifeMotorRunning = true;

  if (hwLedDoorActive) hwLedDoorActive.classList.add("active");
  if (motorStateBadge) {
    motorStateBadge.textContent = "OPENING (PWM RAMP UP)";
    motorStateBadge.classList.add("active");
  }
  if (gearSpinner) gearSpinner.className = "gear-spinner spinning-forward";
  if (motorDirVal) motorDirVal.textContent = "FORWARD (OPEN - D7=H, D8=L)";

  addLogEntry({
    timestamp: new Date().toLocaleTimeString(),
    event_type: "MOTOR_DRIVER_START",
    level: "INFO",
    message: "Arduino Uno: Triggering L298N Door Motor - Smooth PWM Ramp Up & Open Cycle"
  });

  let step = 0;
  if (motorAnimInterval) clearInterval(motorAnimInterval);

  motorAnimInterval = setInterval(() => {
    step++;

    if (step <= 12) {
      // Accelerating Open
      const pwm = Math.round((step / 12) * 220);
      if (motorPwmVal) motorPwmVal.textContent = `${pwm} / 255 (PWM D9)`;
      if (motorApertureVal) motorApertureVal.textContent = `OPENING (${Math.round((step / 12) * 100)}%)`;
      if (motorStateBadge) motorStateBadge.textContent = "OPENING (PWM RAMP)";
    } else if (step <= 30) {
      // Fully Open Dwell Hold
      if (motorPwmVal) motorPwmVal.textContent = `0 / 255 (HOLD DWELL)`;
      if (motorDirVal) motorDirVal.textContent = "HOLD (OPEN)";
      if (motorApertureVal) motorApertureVal.textContent = "100% (OPEN)";
      if (motorStateBadge) motorStateBadge.textContent = "HOLDING OPEN (PASSENGER ENTRY)";
      if (gearSpinner) gearSpinner.className = "gear-spinner";
    } else if (step <= 42) {
      // Reverse Closing
      const revStep = step - 30;
      const pwm = Math.round((revStep / 12) * 220);
      if (motorPwmVal) motorPwmVal.textContent = `${pwm} / 255 (PWM D9)`;
      if (motorDirVal) motorDirVal.textContent = "REVERSE (CLOSE - D7=L, D8=H)";
      if (motorApertureVal) motorApertureVal.textContent = `CLOSING (${Math.round(100 - (revStep / 12) * 100)}%)`;
      if (motorStateBadge) motorStateBadge.textContent = "CLOSING (PWM RAMP)";
      if (gearSpinner) gearSpinner.className = "gear-spinner spinning-reverse";
    } else {
      // Completed!
      clearInterval(motorAnimInterval);
      motorAnimInterval = null;
      isRealLifeMotorRunning = false;

      if (motorPwmVal) motorPwmVal.textContent = "0 / 255 (PWM D9)";
      if (motorDirVal) motorDirVal.textContent = "STOPPED";
      if (motorApertureVal) motorApertureVal.textContent = "CLOSED";
      if (motorStateBadge) {
        motorStateBadge.textContent = "STANDBY";
        motorStateBadge.classList.remove("active");
      }
      if (gearSpinner) gearSpinner.className = "gear-spinner";
      if (hwLedDoorActive) hwLedDoorActive.classList.remove("active");

      addLogEntry({
        timestamp: new Date().toLocaleTimeString(),
        event_type: "MOTOR_DRIVER_FINISHED",
        level: "INFO",
        message: "Arduino Uno: Door motor cycle completed. Emitting DOOR_DONE -> POST /door/complete"
      });

      // Send DOOR_COMPLETE to simulation
      if (currentLiftState.request_id) {
        sendDoorComplete(currentLiftState.request_id);
      }
    }
  }, 100);
}

/* ========================================================================== */
/* Event Listeners Setup                                                      */
/* ========================================================================== */

if (btnPassengerVerifyYes) btnPassengerVerifyYes.addEventListener("click", verifyPassengerYes);
if (btnPassengerVerifyNo) btnPassengerVerifyNo.addEventListener("click", verifyPassengerNo);

if (btnToggleDashCam) {
  btnToggleDashCam.addEventListener("click", () => {
    if (isDashCamActive) {
      stopDashCamera();
    } else {
      startDashCamera();
    }
  });
}

if (btnSwitchDown) btnSwitchDown.addEventListener("click", () => triggerHardwareSwitch(1));
if (btnSwitchUp) btnSwitchUp.addEventListener("click", () => triggerHardwareSwitch(5));

if (btnRequestFloor5) btnRequestFloor5.addEventListener("click", () => sendLiftRequest(5));
if (btnDoorComplete) btnDoorComplete.addEventListener("click", () => sendDoorComplete());

if (btnClearLogs) {
  btnClearLogs.addEventListener("click", () => {
    if (eventFeedTerminal) eventFeedTerminal.innerHTML = "";
    totalLogCount = 0;
    if (logCountBadge) logCountBadge.textContent = "0 events";
  });
}

if (btnCopyReqId) {
  btnCopyReqId.addEventListener("click", () => {
    if (currentLiftState.request_id) {
      navigator.clipboard.writeText(currentLiftState.request_id);
      const original = btnCopyReqId.innerHTML;
      btnCopyReqId.innerHTML = "✓";
      setTimeout(() => { btnCopyReqId.innerHTML = original; }, 1200);
    }
  });
}

// HTTP Polling Fallback (ensures lift position updates even if WebSocket drops)
function startStatusPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch("/lift/status");
      if (res.ok) {
        const data = await res.json();
        updateUI(data);
      }
    } catch (e) {}
  }, 200);
}

// Re-scan Arduino Uno USB COM Ports
const btnScanArduino = document.getElementById("btnScanArduino");
if (btnScanArduino) {
  btnScanArduino.addEventListener("click", async () => {
    const portBadge = document.getElementById("arduinoPortBadge");
    if (portBadge) portBadge.textContent = "Scanning USB ports...";
    try {
      const res = await fetch("/api/uno/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({})
      });
      const data = await res.json();
      addLogEntry({
        timestamp: new Date().toLocaleTimeString(),
        event_type: "HARDWARE_SCAN",
        level: "INFO",
        message: `Arduino USB scan triggered: ${data.status} (${data.port})`
      });
    } catch (e) {
      console.warn("Scan failed:", e);
    }
  });
}

// Startup
window.addEventListener("DOMContentLoaded", () => {
  initShaftFloors();
  initFloorButtons();
  updateSwitchIndicators(1, false);
  connectWebSocket();
  startStatusPolling();
  initDashAiModel();

  // Run camera continuously from the moment the dashboard opens
  startDashCamera();

  // Autoplay fallback in case browser requires user gesture to stream
  document.addEventListener("click", () => {
    if (!isDashCamActive) {
      startDashCamera();
    }
  }, { once: true });

  window.addEventListener("resize", () => updateUI(currentLiftState));
});
