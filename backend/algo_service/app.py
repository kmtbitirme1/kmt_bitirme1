"""
Sulama Algoritması HTTP Servisi
--------------------------------

Node backend (server.js) bu servise HTTP ile sensör verisi gönderir,
karşılığında sulama kararını (ON/OFF + pompa süresi) alır.

Algoritma kodu (final_irrigation_algorithm_standalone.py) HİÇ değiştirilmez;
buradan import edilip çağrılır. Böylece endüstri arkadaşın gönderdiği dosya
olduğu gibi korunur.

Çalıştırma:
    pip install -r requirements.txt
    python app.py
    # veya: flask --app app run --port 5001

Endpoint:
    GET  /health        -> servis ayakta mı
    POST /predict       -> { sensor_data } -> algoritma çıktısı
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request

# ── Algoritma dosyasını import yoluna ekle ────────────────────
# Algoritma servisin yaninda bundle edilmistir: ./algorithm/
# (Endustri ekibinden gelen dosya repo'ya kopyalandi; Render bu sayede bulur.)
# Gerekirse ALGO_DIR ortam degiskeniyle baska bir klasor gosterilebilir.
DEFAULT_ALGO_DIR = Path(__file__).resolve().parent / "algorithm"
ALGO_DIR = Path(os.environ.get("ALGO_DIR", DEFAULT_ALGO_DIR))

if not (ALGO_DIR / "final_irrigation_algorithm_standalone.py").exists():
    raise FileNotFoundError(
        f"Algoritma dosyası bulunamadı: {ALGO_DIR}. "
        "ALGO_DIR ortam değişkeniyle doğru klasörü göster."
    )

sys.path.insert(0, str(ALGO_DIR))

from final_irrigation_algorithm_standalone import (  # noqa: E402
    IrrigationConfig,
    predict_irrigation,
)

app = Flask(__name__)


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "irrigation-algo", "algo_dir": str(ALGO_DIR)})


@app.post("/predict")
def predict():
    """
    Beklenen body örneği:
        {
          "soil_moisture": 35.0,
          "temperature": 28.0,
          "air_humidity_pct": 60.0,
          "pressure_kpa": 101.2,
          "light_lux": 450.0,
          "last_irrigation_minutes_ago": 120.0
        }

    İsteğe bağlı: "config": { "target_soil_moisture": 55, ... }
    """
    body = request.get_json(silent=True) or {}
    sensor_data = body.get("sensor_data", body)  # düz body de kabul

    config = None
    if isinstance(body.get("config"), dict):
        try:
            config = IrrigationConfig(**body["config"])
        except TypeError as exc:
            return jsonify({"error": f"Geçersiz config alanı: {exc}"}), 400

    try:
        result = predict_irrigation(sensor_data, config)
    except ValueError as exc:
        # Algoritma girdi doğrulaması hatası -> 422
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:  # beklenmeyen
        return jsonify({"error": f"Algoritma hatası: {exc}"}), 500

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("ALGO_PORT", "5001"))
    app.run(host="0.0.0.0", port=port)
