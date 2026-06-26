# Sulama Karar Kriterleri Listesi

## 1. Model Tabanlı ON Olasılığı

- Algoritma sensör verilerinden ON olasılığı hesaplar.
- Başlangıç karar eşiği `0.40` olarak belirlenmiştir.
- ON olasılığı güncel karar eşiği ve üzerindeyse model sulama yönünde karar üretir.
- Standart tekil kullanımda karar eşiği `IrrigationConfig` içinden gelir.
- Gerçek sistemde ardışık ölçüm kullanılacaksa karar eşiği `AdaptiveIrrigationController` ile belirli gözlem aralıkları sonunda güncellenebilir.

## 2. Dinamik Karar Eşiği Güncelleme

- Başlangıç karar eşiği `0.40` olarak kullanılır.
- Eşik her gözlemde değil, belirli bir gözlem aralığı tamamlandıktan sonra güncellenir.
- Varsayılan gözlem aralığı `600` ölçümdür. Mevcut sensör kayıtlarında 15 dakikada yaklaşık 300 gözlem bulunduğu için bu değer yaklaşık 30 dakikalık değerlendirme penceresine karşılık gelir.
- Bu aralıkta gerçekleşen ortalama toprak nemi hedef nem ile karşılaştırılır.
- Ortalama nem hedef nemin üstünde kalırsa sistem fazla sulama eğiliminde kabul edilir ve karar eşiği artırılır.
- Ortalama nem hedef nemin altında kalırsa sistemin sulama kararını yeterince kolay veremediği kabul edilir ve karar eşiği azaltılır.
- Sapma oranı `%10` değerinden küçükse eşik ilgili yönde `0.01` değişir.
- Sapma oranı `%10` değerinden büyükse eşik ilgili yönde `0.02` değişir.
- Eşik değeri güvenlik amacıyla `0.20` ile `0.70` arasında tutulur.

## 3. Toprak Nemi

- Toprak nemi algoritmanın ana karar girdisidir.
- `soil_moisture` değeri raw sensör değeri olarak değil, 0-100 aralığına normalize edilmiş toprak nemi değeri olarak gönderilmelidir.
- Toprak nemi hedef nem seviyesine ulaşmışsa pompa çalıştırılmaz.
- Varsayılan hedef nem seviyesi `target_soil_moisture = 55.0` olarak tanımlanmıştır.

## 4. Nem Açığı

Nem açığı şu şekilde hesaplanır:

```text
moisture_deficit = target_soil_moisture - soil_moisture
```

- Nem açığı 0 veya daha küçükse sulama yapılmaz.
- Nem açığı arttıkça önerilen pompa çalışma süresi artar.

## 5. Son Sulamadan Geçen Süre

- Sistem son sulamadan geçen süreyi kontrol eder.
- Varsayılan minimum bekleme süresi `30 dakika` olarak tanımlanmıştır.
- Son sulamadan yeterli süre geçmediyse pompa tekrar çalıştırılmaz.

## 6. Pompa Çalışma Süresi

Pompa çalışma süresi nem açığına göre hesaplanır:

```text
pump_duration_seconds = moisture_deficit * seconds_per_moisture_point
```

- Varsayılan `seconds_per_moisture_point = 0.20` olarak tanımlanmıştır.
- Pompa süresi minimum 1 saniye, maksimum 8 saniye ile sınırlandırılmıştır.
- Bu parametreler prototip testleriyle kalibre edilmelidir.

## 7. Sensör Kalite Uyarısı

- Toprak nemi 0 veya 100 olarak ölçülebilir; bu değerler tek başına sulama kararını durdurmaz.
- Ancak toprak nemi art arda 10 gözlem boyunca 0 veya art arda 10 gözlem boyunca 100 ölçülürse sistem bilgilendirme uyarısı üretir.
- Bu uyarı yalnızca `sensor_quality` çıktısında gösterilir ve sulama kararını değiştirmez.
- Web arayüzünde kullanıcıya sensör kontrolü önerisi göstermek için kullanılabilir.

## 8. Işık Verisi

- BH1750 ışık sensöründen gelen `light_lux` değeri bu versiyonda model kararına doğrudan dahil edilmemiştir.
- Açık veri setinde ışık değişkeni bulunmadığı için model eğitimi bu değişkenle yapılmamıştır.
- Bu değer sistem loglarında tutulabilir ve sonraki geliştirme aşamalarında değerlendirilebilir.

## 9. Basınç ve Hava Koşulları

- `air_humidity_pct` 0-100 aralığında hava nemi olarak gönderilmelidir.
- `pressure_kpa` kPa cinsinden gönderilmelidir.
- hPa veya Pa birimiyle gönderilen basınç değerleri algoritmada hata oluşturur.

## 10. Kalibrasyon Notu

- Toprak nem sensörü raw analog değer üretiyorsa kuru ve ıslak referans ölçümleriyle 0-100 aralığına kalibre edilmelidir.
- Gerçek prototipten veri toplandıkça hedef nem, minimum bekleme süresi, karar eşiği ve pompa süresi katsayısı yeniden değerlendirilebilir.

## 11. Gerçek Sistem Performans Kayıtları

- Performans değerlendirmesi için ayrı bir fiziksel entegrasyon gerekmez; sistemin her ölçümde ürettiği sensör ve karar kayıtları kullanılır.
- Her ölçümde `build_log_record(result)` ile düz performans kaydı oluşturulabilir.
- Bu kayıtlarda toprak nemi, hedef nem, hedef nemden sapma, ON olasılığı, kullanılan eşik, bir sonraki eşik, sulama kararı, pompa süresi, eşik güncelleme bilgisi ve sensör uyarısı tutulur.
- İstenen zaman aralığı veya gözlem parçası seçilerek `evaluate_algorithm_performance.py` ile gerçek sistem performansı hesaplanabilir.
- Temel performans göstergeleri hedef nemden ortalama sapma, ortalama mutlak sapma, hedefin altında/üstünde kalma oranı, sulama kararı sayısı, eşik güncelleme sayısı ve sensör uyarısı sayısıdır.
