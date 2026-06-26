"""
Sulama Algoritması HTTP Servisi
--------------------------------

Node backend (server.js) bu servise HTTP ile sensör verisi gönderir,
karşılığında sulama kararını (ON/OFF + pompa süresi) alır.

Algoritma kodu (final_irrigation_algorithm_standalone.py) HİÇ değiştirilmez;
buradan import edilip çağrılır. Böylece endüstri arkadaşın gönderdiği dosya
olduğu gibi korunur.

Yeni sürüm (v8): algoritmanın ana kullanımı artık STATEFUL
`AdaptiveIrrigationController` sınıfıdır. Bu katman ardışık ölçümlerde:
  - karar eşiğini gözlem penceresi (varsayılan 600) sonunda hedef nem
    sapmasına göre günceller (adaptif eşik),
  - toprak nemi art arda 0/100 ölçülürse sensör kalite uyarısı üretir.

Servis stateless çalışır: controller durumu her ölçümde Postgres'ten
yüklenir, işlenir ve geri yazılır (bkz. state_store.py). Böylece gunicorn
worker sayısından ve Render restart'ından bağımsız kalıcılık sağlanır.
DB yoksa süreç belleğindeki tek controller'a düşülür (graceful degrade).

Çalıştırma:
    pip install -r requirements.txt
    python app.py
    # veya: gunicorn app:app

Endpoint:
    GET  /health        -> servis ayakta mı + state durumu
    GET  /debug/state   -> kalıcı adaptif state teşhisi
    POST /predict       -> { sensor_data } -> algoritma çıktısı
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

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
    AdaptiveIrrigationController,
    IrrigationConfig,
)

import state_store  # noqa: E402

app = Flask(__name__)

# Kalıcı state katmanını başlat (DB yoksa RAM moduna düşer).
state_store.init_state_store()

# ── RAM modu (DB kapalı) için tek kalıcı controller ───────────
# DB etkinken kullanılmaz; her istek DB'den yükleyip kaydeder.
_ram_controller: Optional[AdaptiveIrrigationController] = None
_ram_lock = threading.Lock()


def _build_config(body: Dict[str, Any]) -> IrrigationConfig:
    """İstek gövdesindeki opsiyonel `config` alanından IrrigationConfig üretir.
    server.js her istekte `config: { target_soil_moisture: <sidebar eşiği> }` yollar."""
    raw = body.get("config")
    if isinstance(raw, dict) and raw:
        return IrrigationConfig(**raw)
    return IrrigationConfig()


def _run_with_db(sensor_data: Any, config: IrrigationConfig) -> Dict[str, Any]:
    """Kalıcı yol: state'i kilitli işlem içinde yükle -> ölç -> geri yaz."""
    controller = AdaptiveIrrigationController(config=config)
    with state_store.state_session() as sess:
        if sess.state:
            controller.load_state_dict(sess.state)
        result = controller.process_measurement(sensor_data)
        sess.save(controller.get_state_dict())
    return result


def _run_in_ram(sensor_data: Any, config: IrrigationConfig) -> Dict[str, Any]:
    """RAM modu (DB kapalı): süreç belleğindeki tek controller'ı kullan.
    Hedef nem (target_soil_moisture) sidebar'dan değişebildiği için her
    ölçümde controller.config güncel istekten alınır."""
    global _ram_controller
    with _ram_lock:
        if _ram_controller is None:
            _ram_controller = AdaptiveIrrigationController(config=config)
        else:
            # Frozen dataclass: yeni target ile config'i değiştir, adaptif durumu koru.
            _ram_controller.config = replace(
                _ram_controller.config,
                target_soil_moisture=config.target_soil_moisture,
            )
        return _ram_controller.process_measurement(sensor_data)


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "irrigation-algo",
            "algo_dir": str(ALGO_DIR),
            "state_persistence": "db" if state_store.db_enabled() else "ram",
        }
    )


@app.get("/debug/state")
def debug_state():
    """Kalıcı adaptif state teşhisi: DB durumu + mevcut eşik/pencere bilgisi."""
    diag: Dict[str, Any] = {"store": state_store.diagnostics()}
    if state_store.db_enabled():
        try:
            with state_store.state_session() as sess:
                diag["current_state"] = sess.state  # save() çağrılmaz -> değişmez
        except Exception as exc:  # pragma: no cover
            diag["read_error"] = f"{type(exc).__name__}: {exc}"
    elif _ram_controller is not None:
        diag["current_state"] = _ram_controller.get_state_dict()
    return jsonify(diag)


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

    Çıktı: predict_irrigation sonucuna ek olarak `adaptive_threshold` ve
    `sensor_quality` alanlarını da içerir (Node ekstra alanları yok sayar).
    """
    body = request.get_json(silent=True) or {}
    sensor_data = body.get("sensor_data", body)  # düz body de kabul

    try:
        config = _build_config(body)
    except TypeError as exc:
        return jsonify({"error": f"Geçersiz config alanı: {exc}"}), 400

    try:
        if state_store.db_enabled():
            result = _run_with_db(sensor_data, config)
        else:
            result = _run_in_ram(sensor_data, config)
    except ValueError as exc:
        # Algoritma/config girdi doğrulaması hatası -> 422
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:  # beklenmeyen
        return jsonify({"error": f"Algoritma hatası: {exc}"}), 500

    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("ALGO_PORT", "5001"))
    app.run(host="0.0.0.0", port=port)
