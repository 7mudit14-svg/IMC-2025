"""
weapon_detect_pipeline.py

End-to-end edge inference pipeline for weapon detection on Raspberry Pi 5.

Pipeline stages implemented in this script:
  1. Load ONNX model (exported from a YOLOv8n weapon-detection checkpoint)
  2. OpenCV USB camera capture
  3. Real-time detection per frame
  4. Temporal validation — a detection only becomes a "confirmed" alert once
     it has appeared in at least K of the last N frames, which suppresses
     single-frame false positives (flicker, motion blur, lighting glitches)
  5. Alert/safety action — on a confirmed detection, saves an annotated
     snapshot + timestamp + confidence to disk and appends a row to alerts.csv
  6. Live overlay + CSV logging of FPS, inference latency, CPU%, RAM%,
     CPU temperature, and board power draw (Pi 5 PMIC)

Run with: python3 weapon_detect_pipeline.py
Press 'q' in the video window to quit.
"""

import cv2
import time
import csv
import subprocess
import psutil
from pathlib import Path
from collections import deque, defaultdict
from datetime import datetime
from ultralytics import YOLO

# ---------------- CONFIG ----------------
MODEL_PATH = "/home/pi/weapon-detect/best.onnx"
CAM_INDEX = 0
CONF_THRESH = 0.4
IMG_SIZE = 640

LOG_CSV = "results/benchmark_log.csv"
ALERTS_CSV = "results/alerts.csv"
ALERTS_DIR = "results/alert_snapshots"
LOG_EVERY_N_FRAMES = 10

# Temporal validation: class must appear in at least CONSENSUS_K of the last
# CONSENSUS_N frames before it's treated as a confirmed alert (not just a
# single noisy detection).
CONSENSUS_N = 8
CONSENSUS_K = 5
ALERT_COOLDOWN_SEC = 5.0   # don't re-fire an alert for the same class within this window
# -----------------------------------------


def get_cpu_temp():
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"]).decode()
        return float(out.split("=")[1].split("'")[0])
    except Exception:
        return None


def get_pmic_power():
    """Pi 5 only: sums V*I across PMIC rails for an approximate board power draw (W)."""
    try:
        out = subprocess.check_output(["vcgencmd", "pmic_read_adc"]).decode()
        volts, currents = {}, {}
        for line in out.strip().split("\n"):
            left, value = line.split("=")
            token = left.split()[0]
            value = float(value)
            if token.endswith("_A"):
                currents[token[:-2]] = value
            elif token.endswith("_V"):
                volts[token[:-2]] = value
        return round(sum(currents[r] * volts[r] for r in currents if r in volts), 3)
    except Exception:
        return None


class TemporalValidator:
    """
    Tracks recent per-class detection history across frames and confirms an
    alert only once a class has appeared in K of the last N frames. This is
    the "decision logic" layer sitting between raw per-frame detections and
    an actual alert action, so a single misclassified frame doesn't trigger
    a false alarm.
    """

    def __init__(self, n=CONSENSUS_N, k=CONSENSUS_K, cooldown=ALERT_COOLDOWN_SEC):
        self.n = n
        self.k = k
        self.cooldown = cooldown
        self.history = defaultdict(lambda: deque(maxlen=n))
        self.last_alert_time = defaultdict(lambda: 0.0)

    def update(self, detected_classes_this_frame):
        """
        detected_classes_this_frame: set of class names seen in the current frame.
        Returns a list of class names that just became confirmed (i.e. should
        trigger an alert action right now).
        """
        all_classes = set(self.history.keys()) | detected_classes_this_frame
        confirmed_now = []
        now = time.time()

        for cls in all_classes:
            self.history[cls].append(cls in detected_classes_this_frame)
            hits = sum(self.history[cls])
            if hits >= self.k and (now - self.last_alert_time[cls]) > self.cooldown:
                confirmed_now.append(cls)
                self.last_alert_time[cls] = now

        return confirmed_now


class AlertLogger:
    """Handles the actual 'safety action': saving evidence + logging confirmed alerts."""

    def __init__(self, alerts_dir=ALERTS_DIR, alerts_csv=ALERTS_CSV):
        Path(alerts_dir).mkdir(parents=True, exist_ok=True)
        Path(alerts_csv).parent.mkdir(parents=True, exist_ok=True)
        self.alerts_dir = alerts_dir

        is_new = not Path(alerts_csv).exists()
        self.csv_file = open(alerts_csv, "a", newline="")
        self.writer = csv.writer(self.csv_file)
        if is_new:
            self.writer.writerow(["timestamp", "class", "snapshot_path"])

    def fire(self, cls_name, annotated_frame):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = f"{self.alerts_dir}/{cls_name}_{ts}.jpg"
        cv2.imwrite(snapshot_path, annotated_frame)
        self.writer.writerow([ts, cls_name, snapshot_path])
        self.csv_file.flush()
        print(f"[ALERT] Confirmed '{cls_name}' detection — snapshot saved to {snapshot_path}")

    def close(self):
        self.csv_file.close()


def main():
    if not Path(MODEL_PATH).exists():
        raise FileNotFoundError(f"Model not found at {MODEL_PATH} — update MODEL_PATH.")

    Path(LOG_CSV).parent.mkdir(parents=True, exist_ok=True)

    print("Loading model...")
    model = YOLO(MODEL_PATH, task="detect")

    cap = cv2.VideoCapture(CAM_INDEX)
    if not cap.isOpened():
        raise RuntimeError("Could not open USB camera. Try CAM_INDEX=1 if you have multiple cameras.")
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("Warming up model...")
    for _ in range(3):
        ret, frame = cap.read()
        if ret:
            model.predict(frame, imgsz=IMG_SIZE, conf=CONF_THRESH, verbose=False)

    validator = TemporalValidator()
    alerts = AlertLogger()

    csv_file = open(LOG_CSV, "w", newline="")
    writer = csv.writer(csv_file)
    writer.writerow(
        ["frame", "fps", "inference_ms", "cpu_percent", "ram_percent", "cpu_temp_c", "power_w"]
    )

    frame_count = 0
    fps_smooth = 0.0
    prev_time = time.time()

    print("Starting detection loop. Press 'q' to quit.")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Failed to grab frame.")
                break

            t0 = time.time()
            results = model.predict(frame, imgsz=IMG_SIZE, conf=CONF_THRESH, verbose=False)
            t1 = time.time()
            inference_ms = (t1 - t0) * 1000

            now = time.time()
            instant_fps = 1.0 / (now - prev_time) if now != prev_time else 0.0
            fps_smooth = 0.9 * fps_smooth + 0.1 * instant_fps if fps_smooth else instant_fps
            prev_time = now

            annotated = results[0].plot()

            # ---- temporal validation + alert action ----
            names = model.names
            classes_this_frame = set()
            if results[0].boxes is not None:
                for cls_id in results[0].boxes.cls.tolist():
                    classes_this_frame.add(names[int(cls_id)])

            confirmed = validator.update(classes_this_frame)
            for cls_name in confirmed:
                alerts.fire(cls_name, annotated)
            # ---------------------------------------------

            cpu_pct = psutil.cpu_percent()
            ram_pct = psutil.virtual_memory().percent
            cpu_temp = get_cpu_temp()
            power_w = get_pmic_power()

            overlay_lines = [
                f"FPS: {fps_smooth:.1f}",
                f"Inference: {inference_ms:.1f} ms",
                f"CPU: {cpu_pct:.0f}%  RAM: {ram_pct:.0f}%",
                f"Temp: {cpu_temp:.1f}C" if cpu_temp is not None else "Temp: N/A",
                f"Power: {power_w:.2f} W" if power_w is not None else "Power: N/A",
            ]
            if confirmed:
                overlay_lines.append(f"ALERT: {', '.join(confirmed)}")

            for i, line in enumerate(overlay_lines):
                color = (0, 0, 255) if line.startswith("ALERT") else (0, 255, 0)
                cv2.putText(annotated, line, (10, 25 + i * 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            cv2.imshow("Weapon Detection - Raspberry Pi 5", annotated)

            frame_count += 1
            if frame_count % LOG_EVERY_N_FRAMES == 0:
                writer.writerow([frame_count, round(fps_smooth, 2), round(inference_ms, 1),
                                  cpu_pct, ram_pct, cpu_temp, power_w])
                csv_file.flush()

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    finally:
        cap.release()
        cv2.destroyAllWindows()
        csv_file.close()
        alerts.close()
        print(f"\nBenchmark log saved to {LOG_CSV}")
        print(f"Alerts log saved to {ALERTS_CSV}")


if __name__ == "__main__":
    main()
