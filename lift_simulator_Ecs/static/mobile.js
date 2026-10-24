/**
 * Mobile Phone AI Passenger Detection Engine
 * ===========================================
 * Runs client-side Computer Vision using TensorFlow.js COCO-SSD.
 * 
 * STRICT 4-PHASE LIFECYCLE WITH PASSENGER ABANDONMENT MONITORING:
 * 1. STATE_IDLE: Waiting for user to press a button (on Arduino UNO or Mobile screen).
 *    -> Camera displays live preview, but DOES NOT accumulate hold timer and DOES NOT dispatch.
 * 
 * 2. STATE_SCANNING: Triggered ONLY after Down/Up button is pressed.
 *    -> Actively checks for human in front of camera.
 *    -> Accumulates continuous 2.0s hold threshold.
 *    -> If human verified -> triggers dispatch.
 * 
 * 3. STATE_WAITING_FOR_LIFT: Lift dispatched and traveling to Floor 6.
 *    -> Camera CONTINUES monitoring passenger presence at Floor 6!
 *    -> If passenger leaves station (no human detected for > 3.5s):
 *       AUTOMATICALLY cancels the lift request, turns off LEDs, and resets to idle.
 *    -> If passenger stays until lift arrives:
 *       Doors open and passenger boards.
 * 
 * 4. STATE_DISPATCHED: Lift arrived at Floor 6.
 *    -> Doors open/cycle. Resets to IDLE once trip completes.
 */

(function () {
  'use strict';

  // System States
  const STATE_IDLE = "IDLE";
  const STATE_SCANNING = "SCANNING";
  const STATE_WAITING_FOR_LIFT = "WAITING_FOR_LIFT";
  const STATE_DISPATCHED = "DISPATCHED";

  let currentState = STATE_IDLE;

  // DOM Elements
  const webcam = document.getElementById('webcam');
  const overlay = document.getElementById('overlay');
  const ctx = overlay.getContext('2d');
  const verificationGlow = document.getElementById('verificationGlow');
  const hudConfidence = document.getElementById('hudConfidence');
  const hudFps = document.getElementById('hudFps');
  const modelStatusPill = document.getElementById('modelStatusPill');
  const modelStatusText = document.getElementById('modelStatusText');
  const gaugeFill = document.getElementById('gaugeFill');
  const gaugeTimerText = document.getElementById('gaugeTimerText');
  const gaugePct = document.getElementById('gaugePct');
  const detectionStatusLabel = document.getElementById('detectionStatusLabel');
  const targetSelect = document.getElementById('targetSelect');
  const confidenceSelect = document.getElementById('confidenceSelect');
  const btnSelectDown = document.getElementById('btnSelectDown');
  const btnSelectUp = document.getElementById('btnSelectUp');
  const activeDestTitle = document.getElementById('activeDestTitle');
  const activeDestSubtitle = document.getElementById('activeDestSubtitle');
  const btnManualTrigger = document.getElementById('btnManualTrigger');
  const btnCancelScan = document.getElementById('btnCancelScan');
  const btnFlipCam = document.getElementById('btnFlipCam');
  const btnSnapshot = document.getElementById('btnSnapshot');
  const snapshotInput = document.getElementById('snapshotInput');
  const insecureNoticeBanner = document.getElementById('insecureNoticeBanner');
  const mobileToast = document.getElementById('mobileToast');
  const toastIcon = document.getElementById('toastIcon');
  const toastMessage = document.getElementById('toastMessage');

  // Camera & Detection State
  const isMobileDevice = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
  let cocoModel = null;
  let isCameraActive = false;
  let isDetectingFrame = false;
  let useFacingMode = isMobileDevice ? 'environment' : 'user';
  let selectedDestinationFloor = 1; // 1 = Ground, 5 = Floor 5
  const currentStationFloor = 3;

  // Hold Timer (removed — now instant detection)
  let holdProgressMs = 0; // kept for state reset compatibility

  // Passenger Abandonment Buffer (3.5 seconds of consecutive absence)
  const ABANDON_THRESHOLD_MS = 3500;
  let passengerAbsentMs = 0;

  let lastFrameTime = performance.now();
  let frameCount = 0;
  let fpsTimer = performance.now();

  // Web Audio chime
  let audioCtx = null;
  function playBeep(freq = 880, duration = 0.15) {
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.2, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + duration);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start();
      osc.stop(audioCtx.currentTime + duration);
    } catch (e) {}
  }

  function playSuccessChime() {
    playBeep(587.33, 0.12);
    setTimeout(() => playBeep(880, 0.25), 130);
  }

  function playCancelSound() {
    playBeep(440, 0.1);
    setTimeout(() => playBeep(330, 0.25), 120);
  }

  function triggerVibrate(pattern = [100, 50, 150]) {
    if (navigator.vibrate) {
      try { navigator.vibrate(pattern); } catch (e) {}
    }
  }

  function showToast(msg, icon = '✅', isError = false) {
    if (!toastMessage || !toastIcon || !mobileToast) return;
    toastMessage.textContent = msg;
    toastIcon.textContent = icon;
    if (isError) {
      mobileToast.classList.add('error');
    } else {
      mobileToast.classList.remove('error');
    }
    mobileToast.classList.add('show');
    setTimeout(() => {
      mobileToast.classList.remove('show');
    }, 4000);
  }

  // 1. Initialize Camera (Laptop or Mobile Phone with full multi-browser fallbacks)
  async function initCamera() {
    if (webcam.srcObject) {
      try {
        webcam.srcObject.getTracks().forEach(t => t.stop());
      } catch (e) {}
    }

    const hasGetUserMedia = !!(
      (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) ||
      navigator.getUserMedia ||
      navigator.webkitGetUserMedia ||
      navigator.mozGetUserMedia
    );

    if (!hasGetUserMedia) {
      // Insecure context on mobile LAN HTTP
      console.warn("getUserMedia is not available on this origin (HTTP LAN).");
      isCameraActive = false;
      if (insecureNoticeBanner) insecureNoticeBanner.style.display = 'block';
      detectionStatusLabel.textContent = "Live video restricted by browser on HTTP. Tap 'Take Passenger Photo' below!";
      return;
    }

    function getUserMediaStream(constraints) {
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        return navigator.mediaDevices.getUserMedia(constraints);
      }
      const legacyGUM = navigator.getUserMedia || navigator.webkitGetUserMedia || navigator.mozGetUserMedia;
      return new Promise((resolve, reject) => {
        legacyGUM.call(navigator, constraints, resolve, reject);
      });
    }

    // Try constraint sets in order of specificity
    const constraintSets = [
      { audio: false, video: { facingMode: useFacingMode, width: { ideal: 640 }, height: { ideal: 480 } } },
      { audio: false, video: { facingMode: { ideal: useFacingMode } } },
      { audio: false, video: { facingMode: useFacingMode } },
      { audio: false, video: true }
    ];

    let stream = null;
    for (const constraints of constraintSets) {
      try {
        stream = await getUserMediaStream(constraints);
        if (stream) break;
      } catch (e) {
        // try next fallback
      }
    }

    if (stream) {
      webcam.srcObject = stream;
      webcam.onloadedmetadata = () => {
        webcam.play().catch(() => {});
        adjustCanvasSize();
      };
      webcam.oncanplay = () => adjustCanvasSize();
      isCameraActive = true;
      if (insecureNoticeBanner) insecureNoticeBanner.style.display = 'none';
      detectionStatusLabel.textContent = "Camera Ready • Press Down or Up switch to start";
    } else {
      isCameraActive = false;
      if (insecureNoticeBanner) insecureNoticeBanner.style.display = 'block';
      detectionStatusLabel.textContent = "Camera stream unavailable. Tap 'Take Passenger Photo' below!";
    }
  }

  function adjustCanvasSize() {
    if (!webcam || !overlay) return;
    const w = webcam.videoWidth || webcam.clientWidth || 640;
    const h = webcam.videoHeight || webcam.clientHeight || 480;
    if (w > 0 && h > 0 && (overlay.width !== w || overlay.height !== h)) {
      overlay.width = w;
      overlay.height = h;
    }
  }
  window.addEventListener('resize', adjustCanvasSize);

  if (btnFlipCam) {
    btnFlipCam.addEventListener('click', async () => {
      useFacingMode = (useFacingMode === 'environment') ? 'user' : 'environment';
      webcam.style.transform = (useFacingMode === 'user') ? 'scaleX(-1)' : 'scaleX(1)';
      await initCamera();
    });
  }

  // 2. Load Vision AI Model (with automatic retry if CDN is pending)
  async function loadModel() {
    if (cocoModel) return;

    if (typeof cocoSsd === 'undefined') {
      modelStatusText.textContent = "Loading AI Library...";
      setTimeout(loadModel, 1000);
      return;
    }

    try {
      modelStatusText.textContent = "Loading Vision AI...";
      cocoModel = await cocoSsd.load();
      modelStatusText.textContent = "AI Vision Live";
      modelStatusPill.style.background = "rgba(16, 185, 129, 0.15)";
      modelStatusPill.style.color = "var(--accent-green)";
    } catch (err) {
      console.warn("Error loading COCO-SSD:", err);
      modelStatusText.textContent = "AI Model Pending";
      setTimeout(loadModel, 3000);
    }
  }

  // 3. Native Camera Snapshot Fallback (Works on EVERY mobile phone, even on plain HTTP!)
  if (btnSnapshot && snapshotInput) {
    btnSnapshot.addEventListener('click', () => {
      if (currentState === STATE_DISPATCHED) {
        showToast("Lift is already in motion. Please wait for trip to complete.", "⏳");
        return;
      }
      snapshotInput.click();
    });

    snapshotInput.addEventListener('change', async (e) => {
      const file = e.target.files && e.target.files[0];
      if (!file) return;

      detectionStatusLabel.textContent = "Analyzing photo for human passenger...";
      showToast("🔍 Analyzing passenger photo...", "👤");

      const img = new Image();
      img.onload = async () => {
        // Adjust overlay canvas to image size
        overlay.width = img.naturalWidth || 640;
        overlay.height = img.naturalHeight || 480;
        ctx.clearRect(0, 0, overlay.width, overlay.height);

        // Draw image directly onto canvas
        ctx.drawImage(img, 0, 0, overlay.width, overlay.height);

        if (!cocoModel) {
          showToast("AI Model still loading. Please wait 2 seconds...", "⏳");
          return;
        }

        try {
          const predictions = await cocoModel.detect(img);
          const targetLabel = targetSelect.value.toLowerCase();
          const minConfidence = parseFloat(confidenceSelect.value);

          let bestPerson = null;
          for (const pred of predictions) {
            if (pred.class.toLowerCase() === targetLabel && pred.score >= minConfidence) {
              if (!bestPerson || pred.score > bestPerson.score) {
                bestPerson = pred;
              }
            }
          }

          if (bestPerson) {
            // Draw green bounding box on canvas
            const [x, y, w, h] = bestPerson.bbox;
            ctx.strokeStyle = '#10b981';
            ctx.lineWidth = 4;
            ctx.strokeRect(x, y, w, h);

            ctx.fillStyle = '#10b981';
            ctx.fillRect(x, Math.max(0, y - 24), ctx.measureText(bestPerson.class).width + 60, 24);
            ctx.fillStyle = '#ffffff';
            ctx.font = 'bold 13px JetBrains Mono, monospace';
            ctx.fillText(`${bestPerson.class} ${(bestPerson.score * 100).toFixed(0)}%`, x + 6, Math.max(16, y - 6));

            // Fill gauge to 100%
            gaugeFill.style.width = '100%';
            gaugePct.textContent = '100%';
            gaugeTimerText.textContent = '👤 Detected';
            hudConfidence.textContent = `Verified ${bestPerson.class}: ${(bestPerson.score * 100).toFixed(0)}%`;
            hudConfidence.style.color = '#34d399';

            showToast(`✅ Human Verified in photo (${(bestPerson.score * 100).toFixed(0)}%)! Dispatching lift...`, "🎉");
            await dispatchLiftRequest();
          } else {
            showToast("No human detected in photo. Please retake photo with passenger visible.", "⚠️", true);
            detectionStatusLabel.textContent = "No passenger detected. Please retake photo or press switch.";
          }
        } catch (err) {
          console.error("Photo detection error:", err);
          showToast("Error processing photo", "⚠️", true);
        }
      };
      img.src = URL.createObjectURL(file);
    });
  }

  // 4. Main Computer Vision Loop
  async function visionLoop() {
    // ALWAYS schedule next frame first so loop never dies
    requestAnimationFrame(visionLoop);

    const now = performance.now();
    const dt = now - lastFrameTime;
    lastFrameTime = now;

    frameCount++;
    if (now - fpsTimer >= 1000) {
      hudFps.textContent = `${frameCount} FPS`;
      frameCount = 0;
      fpsTimer = now;
    }

    // ---------- IDLE STATE ----------
    if (currentState === STATE_IDLE) {
      adjustCanvasSize();
      ctx.clearRect(0, 0, overlay.width, overlay.height);
      verificationGlow.classList.remove('active', 'success');

      holdProgressMs = 0;
      passengerAbsentMs = 0;
      gaugeFill.style.width = '0%';
      gaugeFill.style.background = 'linear-gradient(90deg, #3b82f6, #10b981)';
      gaugePct.textContent = '0%';
      gaugeTimerText.textContent = 'Standby';

      hudConfidence.textContent = "Standby (Camera Idle)";
      hudConfidence.style.color = '#ffffff';
      detectionStatusLabel.textContent = "Press Down or Up switch to start passenger verification.";
      return;
    }

    // ---------- DISPATCHED / COMPLETED STATE ----------
    if (currentState === STATE_DISPATCHED) {
      adjustCanvasSize();
      ctx.clearRect(0, 0, overlay.width, overlay.height);
      verificationGlow.classList.remove('active');
      hudConfidence.textContent = "Lift Arrived • Doors Opening";
      hudConfidence.style.color = '#10b981';
      detectionStatusLabel.textContent = "Lift reached Floor 3! Doors opening for passenger entry.";
      return;
    }

    // Camera must be active with ready video
    if (!isCameraActive || webcam.readyState < 2) {
      return;
    }

    if (isDetectingFrame) return;
    isDetectingFrame = true;

    adjustCanvasSize();
    ctx.clearRect(0, 0, overlay.width, overlay.height);

    let targetFound = false;
    let bestConfidence = 0;
    let detectedLabel = "";
    const targetLabel = targetSelect.value.toLowerCase();
    const minConfidence = parseFloat(confidenceSelect.value);

    if (cocoModel) {
      try {
        const predictions = await cocoModel.detect(webcam);
        for (const pred of predictions) {
          const label = pred.class.toLowerCase();
          const score = pred.score;

          if (score > 0.30) {
            const [x, y, w, h] = pred.bbox;
            const isTarget = (label === targetLabel && score >= minConfidence);

            ctx.strokeStyle = isTarget ? '#10b981' : 'rgba(255, 255, 255, 0.25)';
            ctx.lineWidth = isTarget ? 3 : 1;
            ctx.strokeRect(x, y, w, h);

            ctx.fillStyle = isTarget ? '#10b981' : 'rgba(0,0,0,0.5)';
            ctx.fillRect(x, Math.max(0, y - 20), ctx.measureText(label).width + 50, 20);

            ctx.fillStyle = '#ffffff';
            ctx.font = 'bold 11px JetBrains Mono, monospace';
            ctx.fillText(`${pred.class} ${(score * 100).toFixed(0)}%`, x + 4, Math.max(14, y - 5));

            if (isTarget && score > bestConfidence) {
              targetFound = true;
              bestConfidence = score;
              detectedLabel = pred.class;
            }
          }
        }
      } catch (e) {}
    }
    isDetectingFrame = false;

    // ---------- STATE 2: SCANNING (Before Dispatch) ----------
    if (currentState === STATE_SCANNING) {
      if (targetFound) {
        hudConfidence.textContent = `👤 ${detectedLabel}: ${(bestConfidence * 100).toFixed(0)}%`;
        hudConfidence.style.color = '#34d399';
        verificationGlow.classList.add('active');

        detectionStatusLabel.textContent = "👤 Person Present — Dispatching lift!";
        gaugeFill.style.width = '100%';
        gaugeFill.style.background = 'linear-gradient(90deg, #10b981, #059669)';
        gaugePct.textContent = '100%';
        gaugeTimerText.textContent = '👤 Detected';

        // Instant verification — no hold timer needed
        await dispatchLiftRequest();
      } else {
        hudConfidence.textContent = "Watching for Person (Live)...";
        hudConfidence.style.color = '#f59e0b';
        verificationGlow.classList.remove('active');

        detectionStatusLabel.textContent = "Button active! Watching live for person...";
        gaugeFill.style.width = '0%';
        gaugeFill.style.background = 'linear-gradient(90deg, #3b82f6, #10b981)';
        gaugePct.textContent = '0%';
        gaugeTimerText.textContent = 'Live Watching...';
      }
      return;
    }

    // ---------- STATE 3: WAITING FOR LIFT (Continuous Passenger Presence Monitoring) ----------
    if (currentState === STATE_WAITING_FOR_LIFT) {
      if (targetFound) {
        // Passenger is present and waiting! Reset absence timer
        passengerAbsentMs = 0;
        sendMobilePresenceHeartbeat();
        hudConfidence.textContent = `Passenger Present (${(bestConfidence * 100).toFixed(0)}%) • Lift En Route`;
        hudConfidence.style.color = '#34d399';
        verificationGlow.classList.add('active', 'success');
        detectionStatusLabel.textContent = "Lift is traveling to Floor 3! Please wait...";

        gaugeFill.style.width = '100%';
        gaugeFill.style.background = 'linear-gradient(90deg, #10b981, #059669)';
        gaugePct.textContent = 'WAITING';
        gaugeTimerText.textContent = 'Passenger Present';
      } else {
        // PASSENGER NOT IN FRAME! Accumulate absence timer
        passengerAbsentMs += dt;
        const remainSec = Math.max(0, (ABANDON_THRESHOLD_MS - passengerAbsentMs) / 1000).toFixed(1);
        const abandonPct = Math.min(100, Math.round((passengerAbsentMs / ABANDON_THRESHOLD_MS) * 100));

        hudConfidence.textContent = `⚠️ No Passenger Seen (${remainSec}s)`;
        hudConfidence.style.color = '#ef4444';
        verificationGlow.classList.remove('success');
        verificationGlow.classList.add('active');
        detectionStatusLabel.textContent = `Passenger walked away! Auto-canceling in ${remainSec}s...`;

        gaugeFill.style.width = `${100 - abandonPct}%`;
        gaugeFill.style.background = 'linear-gradient(90deg, #ef4444, #f59e0b)';
        gaugePct.textContent = `${remainSec}s`;
        gaugeTimerText.textContent = 'Abandon Warning';

        // 3.5s absence reached -> PASSENGER ABANDONED CALL! AUTO-CANCEL!
        if (passengerAbsentMs >= ABANDON_THRESHOLD_MS) {
          await autoCancelAbandonedCall();
        }
      }
    }
  }

  // 5. Dispatch Lift Request to Backend
  async function dispatchLiftRequest() {
    verificationGlow.classList.remove('active');
    verificationGlow.classList.add('success');
    playSuccessChime();
    triggerVibrate([120, 60, 180]);

    const destName = selectedDestinationFloor === 1 ? "Ground Floor (FL 1)" : "Floor 5 (Top)";
    showToast(`✅ Human Verified! Lift dispatched to Floor 3 ➔ ${destName}`, "🛗");

    const reqId = (crypto && crypto.randomUUID) ? crypto.randomUUID() : 'mob-' + Math.random().toString(36).substring(2, 10);

    try {
      const response = await fetch('/ai/verify-human', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          verified: true,
          floor: currentStationFloor, // Floor 3
          destination_floor: selectedDestinationFloor,
          source: 'MOBILE_PHONE_AI',
          request_id: reqId
        })
      });

      const resData = await response.json();
      if (response.ok) {
        showToast(`🛗 Lift en route to Floor 3! Please stay in front of camera.`, "🎉");
        // Transition to WAITING_FOR_LIFT: Keep monitoring until lift arrives!
        currentState = STATE_WAITING_FOR_LIFT;
        passengerAbsentMs = 0;
      } else {
        currentState = STATE_IDLE;
      }
    } catch (err) {
      console.error("Failed to send lift request:", err);
      showToast("Network error contacting server", "⚠️", true);
      currentState = STATE_IDLE;
    }
  }

  let lastHeartbeatSent = 0;
  function sendMobilePresenceHeartbeat() {
    const now = performance.now();
    if (now - lastHeartbeatSent >= 700) {
      lastHeartbeatSent = now;
      fetch('/ai/presence', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ present: true })
      }).catch(() => {});
    }
  }

  // 6. Automatic Cancellation When Passenger Walks Away
  async function autoCancelAbandonedCall() {
    currentState = STATE_IDLE;
    passengerAbsentMs = 0;
    holdProgressMs = 0;

    adjustCanvasSize();
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    verificationGlow.classList.remove('active', 'success');

    gaugeFill.style.width = '0%';
    gaugePct.textContent = '0%';
    gaugeTimerText.textContent = 'Standby';
    hudConfidence.textContent = "Call Canceled • Passenger Left";
    hudConfidence.style.color = '#ef4444';
    detectionStatusLabel.textContent = "Passenger left Floor 3. Lift request canceled automatically.";

    playCancelSound();
    triggerVibrate([200, 100, 200]);
    showToast("🚶 Passenger Left! Lift request canceled automatically.", "❌", true);

    try {
      await fetch('/lift/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: "PASSENGER_WALKED_AWAY" })
      });
    } catch (e) {
      console.warn("Error sending cancel:", e);
    }
  }

  // 7. User Presses Switch (Down or Up)
  async function triggerSwitch(destFloor) {
    if (currentState === STATE_WAITING_FOR_LIFT) {
      showToast("Lift is already traveling to Floor 3! Please wait.", "🛗");
      return;
    }
    if (currentState === STATE_DISPATCHED) {
      showToast("Lift is currently at Floor 3. Please wait for doors.", "⏳");
      return;
    }

    selectedDestinationFloor = destFloor;
    updateDestUI(destFloor);
    currentState = STATE_SCANNING;
    holdProgressMs = 0;
    passengerAbsentMs = 0;

    const destStr = destFloor === 1 ? "Ground Floor (Floor 1)" : "5th Floor (Floor 5)";
    showToast(`🔔 Switch Pressed! Floor 3 ➔ ${destStr}. Stand in front of camera!`, "📷");
    playBeep(440, 0.1);

    // Notify backend and Arduino UNO on COM3 (lights up blue or amber switch LED)
    try {
      await fetch('/ai/trigger-switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ destination_floor: destFloor })
      });
    } catch (e) {}
  }

  function updateDestUI(destFloor) {
    selectedDestinationFloor = destFloor;
    if (destFloor === 1) {
      btnSelectDown.classList.add('active-down');
      btnSelectUp.classList.remove('active-up');
      activeDestTitle.textContent = "⬇️ Floor 3 ➔ Ground (Floor 1)";
      activeDestSubtitle.textContent = "Switch: Pin 2 (Blue LED on Uno stays ON)";
    } else {
      btnSelectUp.classList.add('active-up');
      btnSelectDown.classList.remove('active-down');
      activeDestTitle.textContent = "⬆️ Floor 3 ➔ Floor 5 (Top)";
      activeDestSubtitle.textContent = "Switch: Pin 3 (Amber LED on Uno stays ON)";
    }
  }

  // Switch Click Listeners on Phone Screen
  btnSelectDown.addEventListener('click', () => triggerSwitch(1));
  btnSelectUp.addEventListener('click', () => triggerSwitch(5));

  // Instant Manual Trigger
  btnManualTrigger.addEventListener('click', () => {
    if (currentState === STATE_DISPATCHED || currentState === STATE_WAITING_FOR_LIFT) return;
    currentState = STATE_SCANNING;
    holdProgressMs = REQUIRED_HOLD_MS;
    dispatchLiftRequest();
  });

  // Cancel Button
  btnCancelScan.addEventListener('click', async () => {
    currentState = STATE_IDLE;
    holdProgressMs = 0;
    passengerAbsentMs = 0;
    gaugeFill.style.width = '0%';
    gaugePct.textContent = '0%';
    verificationGlow.classList.remove('active', 'success');
    showToast("Verification canceled. System reset to IDLE.", "❌");

    try {
      await fetch('/lift/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: "USER_MANUAL_CANCEL" })
      });
    } catch (e) {}
  });

  // 8. Synchronize Live with Arduino UNO over WebSocket & Polling
  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/lift`;
    let ws = null;

    try {
      ws = new WebSocket(wsUrl);

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          
          // If request was canceled by timeout, button, or abandonment
          if ((msg.event && (msg.event.event === 'SCAN_CANCELED' || msg.event.event === 'PASSENGER_ABANDONED')) || msg.type === 'SCAN_CANCELED') {
            currentState = STATE_IDLE;
            holdProgressMs = 0;
            passengerAbsentMs = 0;
            ctx.clearRect(0, 0, overlay.width, overlay.height);
            verificationGlow.classList.remove('active', 'success');
            showToast("Trip canceled. Camera in standby.", "⏹️");
            return;
          }

          if (msg.type === 'STATUS_UPDATE' || msg.type === 'INITIAL_STATE') {
            const data = msg.data || {};

            // Lift reached Floor 3! Stop waiting monitoring and transition to arrival
            if (data.current_floor === 3 && (data.state === 'ARRIVED' || data.state === 'DOOR_OPENING' || data.state === 'DOOR_OPEN')) {
              if (currentState === STATE_WAITING_FOR_LIFT) {
                currentState = STATE_DISPATCHED;
                passengerAbsentMs = 0;
                playSuccessChime();
                showToast("🛗 Lift Arrived at Floor 3! Doors opening!", "🎉");
                detectionStatusLabel.textContent = "Lift arrived at Floor 3! Doors opening.";
                hudConfidence.textContent = "Lift Arrived • Doors Opening";
                hudConfidence.style.color = '#10b981';
              }
            }

            // If lift returns to NORMAL idle and no request is active, reset mobile state to IDLE ready for next button press
            if (data.state === 'NORMAL' && !data.request_id && (currentState === STATE_DISPATCHED || currentState === STATE_WAITING_FOR_LIFT)) {
              currentState = STATE_IDLE;
              holdProgressMs = 0;
              passengerAbsentMs = 0;
              ctx.clearRect(0, 0, overlay.width, overlay.height);
              verificationGlow.classList.remove('active', 'success');
            }

            // If a physical button on Arduino UNO was pressed -> start scanning
            if (data.scan_active && currentState !== STATE_DISPATCHED && currentState !== STATE_WAITING_FOR_LIFT) {
              if (currentState !== STATE_SCANNING) {
                currentState = STATE_SCANNING;
                holdProgressMs = 0;
                passengerAbsentMs = 0;
                if (data.scan_target) {
                  updateDestUI(data.scan_target);
                }
                showToast("🔔 Switch Pressed! Stand in front of camera...", "👤");
              }
            } else if (!data.scan_active && currentState === STATE_SCANNING) {
              // Switch/scan canceled or timed out -> stop scanning immediately!
              currentState = STATE_IDLE;
              holdProgressMs = 0;
              passengerAbsentMs = 0;
              ctx.clearRect(0, 0, overlay.width, overlay.height);
              verificationGlow.classList.remove('active', 'success');
              showToast("Scan ended. Standing by...", "⏹️");
            }
          }
        } catch (e) {}
      };

      ws.onclose = () => setTimeout(connectWebSocket, 3000);
    } catch (err) {
      setTimeout(connectWebSocket, 5000);
    }
  }

  // Periodic poll fallback for Arduino UNO switch and cancel events
  setInterval(async () => {
    if (currentState === STATE_DISPATCHED) return;
    try {
      const res = await fetch('/ai/scan-status');
      if (res.ok) {
        const data = await res.json();
        if (data.scan_active && currentState === STATE_IDLE) {
          currentState = STATE_SCANNING;
          holdProgressMs = 0;
          passengerAbsentMs = 0;
          if (data.destination_floor) {
            updateDestUI(data.destination_floor);
          }
        } else if (!data.scan_active && currentState === STATE_SCANNING) {
          // Canceled or timed out on Arduino UNO or Backend -> Stop looking for persons immediately!
          currentState = STATE_IDLE;
          holdProgressMs = 0;
          passengerAbsentMs = 0;
          ctx.clearRect(0, 0, overlay.width, overlay.height);
          verificationGlow.classList.remove('active', 'success');
        }
      }
    } catch (e) {}
  }, 1200);

  // Initialize
  async function init() {
    updateDestUI(1);
    await initCamera();
    await loadModel();
    connectWebSocket();
    requestAnimationFrame(visionLoop);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
