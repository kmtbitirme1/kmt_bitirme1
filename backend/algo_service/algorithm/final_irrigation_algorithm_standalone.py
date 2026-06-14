"""
Final Sulama Karar ve Optimizasyon Algoritması - Standalone Sürüm
-------------------------------------------------------------------

Bu dosya tek başına çalışır. Ayrı bir Logistic Regression model dosyası gerektirmez.
Model katsayıları, ölçekleme değerleri ve karar eşiği doğrudan bu dosyanın içine gömülüdür.

Algoritma iki katmandan oluşur:
1. Karar katmanı: Sensör verilerinden ON olasılığı hesaplanır ve 0.40 karar eşiğiyle sulama kararı verilir.
2. Optimizasyon katmanı: Sulama gerekiyorsa nem açığına göre pompa çalışma süresi hesaplanır.

Bu dosya arkadaşlara verilecek ana algoritma dosyasıdır.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping
import json
import math


# ============================================================
# 1. MODEL SABİTLERİ
# ============================================================

MODEL_FEATURES = [
    "Soil_Moisture",
    "Temperature",
    "Air_humidity_pct",
    "Pressure_KPa",
]

# StandardScaler ortalamaları
SCALER_MEAN = [
    45.433090227130656,
    22.492227547405708,
    58.52105188580954,
    101.13141821212753,
]

# StandardScaler standart sapma değerleri
SCALER_SCALE = [
    26.007173042265276,
    13.28271661038062,
    30.072821403765282,
    0.2184434734277875,
]

# Logistic Regression katsayıları
MODEL_COEFFICIENTS = [
    -0.7745651265412601,
    0.7426499310497418,
    -0.015164651317430384,
    -0.020784588977389953,
]

# Logistic Regression sabiti
MODEL_INTERCEPT = 0.20465532647535664


# ============================================================
# 2. GİRDİ ANAHTARLARI
# ============================================================

INPUT_KEY_ALIASES = {
    "Soil_Moisture": ["Soil_Moisture", "soil_moisture"],
    "Temperature": ["Temperature", "temperature"],
    "Air_humidity_pct": ["Air_humidity_pct", "air_humidity_pct", "air_humidity"],
    "Pressure_KPa": ["Pressure_KPa", "pressure_kpa", "pressure"],
}

OPTIONAL_INPUTS = {
    "light_lux": ["light_lux", "light_level", "Light_Lux"],
    "last_irrigation_minutes_ago": ["last_irrigation_minutes_ago", "minutes_since_last_irrigation"],
}

# Yüzde işaretiyle gelebilecek alanlar.
# Sensör/API entegrasyonunda sayısal değer gönderilmesi önerilir.
# Ancak kullanıcı arayüzü veya ara katmanda "55%" gibi string gelirse yalnızca bu alanlarda kabul edilir.
PERCENT_COMPATIBLE_FIELDS = {
    "Soil_Moisture",
    "soil_moisture",
    "Air_humidity_pct",
    "air_humidity_pct",
    "air_humidity",
}


# ============================================================
# 3. ALGORİTMA PARAMETRELERİ
# ============================================================

@dataclass(frozen=True)
class IrrigationConfig:
    # Logistic Regression karar eşiği
    decision_threshold: float = 0.40

    # Hedef toprak nem seviyesi
    target_soil_moisture: float = 55.0

    # Aynı noktaya çok sık sulama yapılmasını engelleyen minimum bekleme süresi
    minimum_irrigation_interval_minutes: float = 30.0

    # Nem açığı başına pompa çalışma süresi katsayısı
    seconds_per_moisture_point: float = 0.20

    # Pompa çalışma süresi sınırları
    min_pump_duration_seconds: float = 1.0
    max_pump_duration_seconds: float = 8.0

    # Satın alınan pompanın nominal akış aralığı
    # Bu bilgi raporlama ve kalibrasyon içindir.
    pump_flow_lph_min: float = 80.0
    pump_flow_lph_max: float = 120.0

    def validate(self) -> None:
        if not 0 < self.decision_threshold < 1:
            raise ValueError("decision_threshold 0 ile 1 arasında olmalıdır.")
        if not 0 < self.target_soil_moisture <= 100:
            raise ValueError("target_soil_moisture 0 ile 100 arasında olmalıdır.")
        if self.minimum_irrigation_interval_minutes < 0:
            raise ValueError("minimum_irrigation_interval_minutes negatif olamaz.")
        if self.seconds_per_moisture_point <= 0:
            raise ValueError("seconds_per_moisture_point pozitif olmalıdır.")
        if self.min_pump_duration_seconds < 0:
            raise ValueError("min_pump_duration_seconds negatif olamaz.")
        if self.max_pump_duration_seconds <= 0:
            raise ValueError("max_pump_duration_seconds pozitif olmalıdır.")
        if self.min_pump_duration_seconds > self.max_pump_duration_seconds:
            raise ValueError("min_pump_duration_seconds max_pump_duration_seconds değerinden büyük olamaz.")


# ============================================================
# 4. YARDIMCI FONKSİYONLAR
# ============================================================

def _to_float(value: Any, field_name: str) -> float:
    original_value = value

    if isinstance(value, bool):
        raise ValueError(f"{field_name} bool olamaz. Gelen değer: {value!r}")

    if isinstance(value, str):
        value = value.strip().replace(",", ".")

        if value.endswith("%"):
            if field_name not in PERCENT_COMPATIBLE_FIELDS:
                raise ValueError(
                    f"{field_name} yüzde işaretiyle gönderilemez. Gelen değer: {original_value!r}"
                )
            value = value[:-1].strip()

    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} sayısal olmalıdır. Gelen değer: {original_value!r}")

    if math.isnan(numeric_value) or math.isinf(numeric_value):
        raise ValueError(f"{field_name} geçerli bir sayı olmalıdır. Gelen değer: {original_value!r}")

    return numeric_value


def _get_required_value(sensor_data: Mapping[str, Any], feature_name: str) -> float:
    aliases = INPUT_KEY_ALIASES[feature_name]

    for key in aliases:
        if key in sensor_data:
            return _to_float(sensor_data[key], key)

    raise ValueError(f"Girdi eksik: {feature_name}. Kabul edilen anahtarlar: {aliases}")


def _get_optional_value(sensor_data: Mapping[str, Any], optional_name: str) -> float | None:
    aliases = OPTIONAL_INPUTS[optional_name]

    for key in aliases:
        if key in sensor_data and sensor_data[key] is not None:
            return _to_float(sensor_data[key], key)

    return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def build_model_input(sensor_data: Mapping[str, Any]) -> Dict[str, float]:
    model_input = {
        feature: _get_required_value(sensor_data, feature)
        for feature in MODEL_FEATURES
    }
    _validate_model_input_ranges(model_input)
    return model_input


def _validate_model_input_ranges(model_input: Mapping[str, float]) -> None:
    soil_moisture = model_input["Soil_Moisture"]
    temperature = model_input["Temperature"]
    air_humidity = model_input["Air_humidity_pct"]
    pressure = model_input["Pressure_KPa"]

    if not 0 <= soil_moisture <= 100:
        raise ValueError(
            "Soil_Moisture 0-100 aralığında normalize edilmiş değer olmalıdır. "
            f"Gelen değer: {soil_moisture}"
        )

    if not -20 <= temperature <= 80:
        raise ValueError(
            "Temperature °C cinsinden beklenir ve -20 ile 80 arasında olmalıdır. "
            f"Gelen değer: {temperature}"
        )

    if not 0 <= air_humidity <= 100:
        raise ValueError(
            "Air_humidity_pct 0-100 aralığında yüzde nem değeri olmalıdır. "
            f"Gelen değer: {air_humidity}"
        )

    if not 80 <= pressure <= 120:
        raise ValueError(
            "Pressure_KPa kPa cinsinden beklenir ve 80-120 aralığında olmalıdır. "
            f"Gelen değer: {pressure}"
        )


# ============================================================
# 5. KARAR MODELİ
# ============================================================

def calculate_on_probability(model_input: Mapping[str, float]) -> float:
    """
    Logistic Regression modelinin ON olasılığını manuel olarak hesaplar.
    Ayrı model dosyası gerekmez.
    """
    standardized_values = []

    for index, feature in enumerate(MODEL_FEATURES):
        value = model_input[feature]
        standardized_value = (value - SCALER_MEAN[index]) / SCALER_SCALE[index]
        standardized_values.append(standardized_value)

    logit = MODEL_INTERCEPT

    for coefficient, standardized_value in zip(MODEL_COEFFICIENTS, standardized_values):
        logit += coefficient * standardized_value

    on_probability = 1 / (1 + math.exp(-logit))
    return float(on_probability)


# ============================================================
# 6. POMPA SÜRESİ OPTİMİZASYONU
# ============================================================

def calculate_pump_duration_seconds(
    soil_moisture: float,
    config: IrrigationConfig,
) -> tuple[float, float]:
    """
    Nem açığına göre pompa çalışma süresini hesaplar.

    moisture_deficit = target_soil_moisture - soil_moisture
    raw_duration = moisture_deficit * seconds_per_moisture_point
    pump_duration = min/max sınırları içinde tutulur.
    """
    moisture_deficit = max(0.0, config.target_soil_moisture - soil_moisture)

    if moisture_deficit <= 0:
        return 0.0, 0.0

    raw_duration = moisture_deficit * config.seconds_per_moisture_point
    pump_duration = _clamp(
        raw_duration,
        config.min_pump_duration_seconds,
        config.max_pump_duration_seconds,
    )

    return round(pump_duration, 2), round(moisture_deficit, 2)


# ============================================================
# 7. FİNAL ALGORİTMA
# ============================================================

def predict_irrigation(
    sensor_data: Mapping[str, Any],
    config: IrrigationConfig | None = None,
) -> Dict[str, Any]:
    """
    Sensör verilerine göre sulama kararı ve pompa çalışma süresini üretir.
    """
    if config is None:
        config = IrrigationConfig()

    config.validate()

    model_input = build_model_input(sensor_data)
    soil_moisture = model_input["Soil_Moisture"]

    light_lux = _get_optional_value(sensor_data, "light_lux")
    last_irrigation_minutes_ago = _get_optional_value(sensor_data, "last_irrigation_minutes_ago")

    if light_lux is not None and light_lux < 0:
        raise ValueError(f"light_lux negatif olamaz. Gelen değer: {light_lux}")

    if last_irrigation_minutes_ago is not None and last_irrigation_minutes_ago < 0:
        raise ValueError(
            "last_irrigation_minutes_ago negatif olamaz. "
            f"Gelen değer: {last_irrigation_minutes_ago}"
        )

    on_probability = calculate_on_probability(model_input)
    model_says_on = on_probability >= config.decision_threshold

    moisture_deficit = round(max(0.0, config.target_soil_moisture - soil_moisture), 2)

    # 1. Model OFF diyorsa pompa çalışmaz.
    if not model_says_on:
        return {
            "irrigation_required": False,
            "decision_label": "OFF",
            "on_probability": round(on_probability, 6),
            "decision_threshold": config.decision_threshold,
            "pump_duration_seconds": 0.0,
            "moisture_deficit": moisture_deficit,
            "decision_reason": (
                f"ON olasılığı {on_probability:.3f}, "
                f"{config.decision_threshold:.2f} karar eşiğinin altındadır."
            ),
            "model_input": model_input,
            "operational_inputs": {
                "light_lux": light_lux,
                "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
            },
            "config": asdict(config),
        }

    # 2. Toprak nemi hedefe ulaşmışsa pompa çalışmaz.
    if soil_moisture >= config.target_soil_moisture:
        return {
            "irrigation_required": False,
            "decision_label": "OFF",
            "on_probability": round(on_probability, 6),
            "decision_threshold": config.decision_threshold,
            "pump_duration_seconds": 0.0,
            "moisture_deficit": 0.0,
            "decision_reason": (
                f"Model ON eğilimi göstermiştir ancak toprak nemi "
                f"hedef değer olan {config.target_soil_moisture:.1f} seviyesine ulaşmıştır."
            ),
            "model_input": model_input,
            "operational_inputs": {
                "light_lux": light_lux,
                "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
            },
            "config": asdict(config),
        }

    # 3. Son sulamadan yeterince süre geçmediyse pompa çalışmaz.
    if (
        last_irrigation_minutes_ago is not None
        and last_irrigation_minutes_ago < config.minimum_irrigation_interval_minutes
    ):
        return {
            "irrigation_required": False,
            "decision_label": "OFF",
            "on_probability": round(on_probability, 6),
            "decision_threshold": config.decision_threshold,
            "pump_duration_seconds": 0.0,
            "moisture_deficit": moisture_deficit,
            "decision_reason": (
                f"Model ON eğilimi göstermiştir ancak son sulamadan yalnızca "
                f"{last_irrigation_minutes_ago:.1f} dakika geçmiştir."
            ),
            "model_input": model_input,
            "operational_inputs": {
                "light_lux": light_lux,
                "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
            },
            "config": asdict(config),
        }

    # 4. Sulama gerekiyorsa pompa süresi hesaplanır.
    pump_duration_seconds, moisture_deficit = calculate_pump_duration_seconds(
        soil_moisture=soil_moisture,
        config=config,
    )

    return {
        "irrigation_required": True,
        "decision_label": "ON",
        "on_probability": round(on_probability, 6),
        "decision_threshold": config.decision_threshold,
        "pump_duration_seconds": pump_duration_seconds,
        "moisture_deficit": moisture_deficit,
        "decision_reason": (
            f"ON olasılığı {on_probability:.3f}, "
            f"{config.decision_threshold:.2f} karar eşiğinin üzerindedir. "
            f"Nem açığı {moisture_deficit:.2f} olarak hesaplanmış ve "
            f"pompa süresi {pump_duration_seconds:.2f} saniye belirlenmiştir."
        ),
        "model_input": model_input,
        "operational_inputs": {
            "light_lux": light_lux,
            "last_irrigation_minutes_ago": last_irrigation_minutes_ago,
        },
        "config": asdict(config),
    }


# ============================================================
# 8. ÖRNEK ÇALIŞTIRMA
# ============================================================

if __name__ == "__main__":
    sample_sensor_data = {
        "soil_moisture": 35.0,
        "temperature": 28.0,
        "air_humidity_pct": 60.0,
        "pressure_kpa": 101.2,
        "light_lux": 450.0,
        "last_irrigation_minutes_ago": 120.0,
    }

    result = predict_irrigation(sample_sensor_data)
    print(json.dumps(result, ensure_ascii=False, indent=2))
