# Sulama Karar Kriterleri Listesi

## 1. Model Tabanlı ON Olasılığı

- Algoritma sensör verilerinden ON olasılığı hesaplar.
- Başlangıç karar eşiği `0.40` olarak belirlenmiştir.
- ON olasılığı 0.40 ve üzerindeyse model sulama yönünde karar üretir.

## 2. Toprak Nemi

- Toprak nemi algoritmanın ana karar girdisidir.
- `soil_moisture` değeri raw sensör değeri olarak değil, 0-100 aralığına normalize edilmiş toprak nemi değeri olarak gönderilmelidir.
- Toprak nemi hedef nem seviyesine ulaşmışsa pompa çalıştırılmaz.
- Varsayılan hedef nem seviyesi `target_soil_moisture = 55.0` olarak tanımlanmıştır.

## 3. Nem Açığı

Nem açığı şu şekilde hesaplanır:

```text
moisture_deficit = target_soil_moisture - soil_moisture
```

- Nem açığı 0 veya daha küçükse sulama yapılmaz.
- Nem açığı arttıkça önerilen pompa çalışma süresi artar.

## 4. Son Sulamadan Geçen Süre

- Sistem son sulamadan geçen süreyi kontrol eder.
- Varsayılan minimum bekleme süresi `30 dakika` olarak tanımlanmıştır.
- Son sulamadan yeterli süre geçmediyse pompa tekrar çalıştırılmaz.

## 5. Pompa Çalışma Süresi

Pompa çalışma süresi nem açığına göre hesaplanır:

```text
pump_duration_seconds = moisture_deficit * seconds_per_moisture_point
```

- Varsayılan `seconds_per_moisture_point = 0.20` olarak tanımlanmıştır.
- Pompa süresi minimum 1 saniye, maksimum 8 saniye ile sınırlandırılmıştır.
- Bu parametreler prototip testleriyle kalibre edilmelidir.

## 6. Işık Verisi

- BH1750 ışık sensöründen gelen `light_lux` değeri bu versiyonda model kararına doğrudan dahil edilmemiştir.
- Açık veri setinde ışık değişkeni bulunmadığı için model eğitimi bu değişkenle yapılmamıştır.
- Bu değer sistem loglarında tutulabilir ve sonraki geliştirme aşamalarında değerlendirilebilir.

## 7. Basınç ve Hava Koşulları

- `air_humidity_pct` 0-100 aralığında hava nemi olarak gönderilmelidir.
- `pressure_kpa` kPa cinsinden gönderilmelidir.
- hPa veya Pa birimiyle gönderilen basınç değerleri algoritmada hata oluşturur.

## 8. Kalibrasyon Notu

- Toprak nem sensörü raw analog değer üretiyorsa kuru ve ıslak referans ölçümleriyle 0-100 aralığına kalibre edilmelidir.
- Gerçek prototipten veri toplandıkça hedef nem, minimum bekleme süresi ve pompa süresi katsayısı yeniden değerlendirilebilir.
