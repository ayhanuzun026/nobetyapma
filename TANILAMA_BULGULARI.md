# Tanılama ve bekleme bulguları

- Hazırlık kapasitesi `INFEASIBLE` veya `UNKNOWN` döndüğünde istemci
  `hazirlikKapasiteKontrolEt` içinde kullanıcı kararını bekleyen bir Promise'e
  giriyordu. Panel 5. adıma taşınmadığı için 6. adım düğmesi uzun süre
  **Oluşturuluyor...** durumunda kalabiliyordu.
- Hedef hesaplama başarılı sonucu, görev kotası dağıtımından önceki hedefleri;
  plan kontratı ise dağıtım sonrası kotaları taşıyabiliyor. Bu iki görünüm aynı
  sözleşme olarak raporlanmalıdır.
- Backend debug kaydına istemci `denemeId/istekId/istemciSurum`, sunucu
  `serverSureMs/durum/asama/kararDurumu/surum` bilgileri eklenmiştir.
  Firestore `kayitId` yalnız gerçek kayıt başarılıysa yanıt tanılamasına yazılır;
  log başarısızlığı isteği engellemez.
