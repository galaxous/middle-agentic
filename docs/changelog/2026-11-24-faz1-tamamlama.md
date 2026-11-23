# 2026-11-24 · Faz 1 tamamlama: katman sınırları, yetenek bayrakları, profiller, katman testi · main'de (bf69056), v0.7.0

## Durum

`main'de` (bf69056). v0.7.0 olarak yayınlandı (sürüm commit'i ebdcd15). Kod ve testler hazır (238 geçiyor). Plandaki Faz 1 maddelerinden 1, 2, 3, 4, 5 ve 6 bu değişiklikle tamamlandı; yalnızca tahmini rezervasyon (madde 7) kaldı.

## Sorun

- Ücretsiz katmanlar günlük sınırına ulaşınca ya da hız sınırına takılınca her istekte yeniden deneniyordu.
- Araç çağrısı ya da çok uzun istem, bu özelliği desteklemeyen katmanlara gidebiliyordu.
- Zinciri maliyete göre sıralayan hazır bir yol yoktu.
- Yeni bir modelin fiyatını ve kimliğini doğrulamak için komut yoktu.

## Değişenler

- **Anahtar başına bütçe** (isteğe bağlı, varsayılan kapalı): `magent key create --budget/--on-exceed`, `magent key set-budget`, `magent budget --key|set --key|reset --key`, `PUT /admin/keys/{ad}/budget`, `GET /admin/budget?key=`. Verilmeyen anahtarlar genel bütçeyle çalışmaya devam eder.

- **Günlük istek sınırı** (`daily_request_cap`, `reset_tz`): kullanım kaydındaki denemeler sayılır; sınıra ulaşınca katman atlanır.
- **Soğuma** (`cooldown_seconds`, varsayılan 900 sn): hız sınırı (429) alan katman bu süre denenmez.
- **Yetenekler** (`supports_tools`, `context_window`): araç çağrısı içeren istekler ve sınırı aşan istemler ilgili katmanlara gönderilmez.
- **Profiller**: `model: profile:cheap-first` ve `profile:quality-first`.
- **Katman testi**: `POST /admin/models/{ad}/test` ve `magent models test <ad>`. Zinciri ve bütçeyi atlar; sonuç kullanım kaydına `models-test` işi olarak yazılır.
- Atlanan her katman, yanıttaki `attempts` içinde nedeniyle birlikte görünür.
- `config.example.yaml`: yeni alanların açıklaması; Gemini ve Luna örnekleri yorum satırında, fiyat ve kimlik alanları "doğrulanacak" olarak işaretli.

## Doğrulama

- `./venv/bin/python -m pytest -q`: 238 geçiyor (önceki 220; 18 yeni).
- `tests/test_chain_controls.py` (10): istek sınırı, soğuma, araç ve bağlam kontrolü, `cheap-first`/`quality-first` sıralaması, bilinmeyen profil, `models test` (başarı, hata, devre dışı katman, yetki).
- `tests/test_client_admin.py` (+1): `magent models test` isteği.

## Açık kalanlar

- Gemini Hydrogen/Neon ve Luna kimlikleri ile fiyatları resmi kaynaktan doğrulanmadı; örnekler yalnızca yorumda. Doğrulama için `magent models test` kullanılmalı.
- Bütçe tahmini rezervasyonu (Faz 1 madde 7) bu sürümde yok; aşım payı hâlâ var.
- Çalışma zamanında yeni katman ekleme/silme yok; yalnızca mevcut katmanlar değiştirilebiliyor.
- Yeni özellikler gateway makinesinde v0.7.0 (veya seçilen sürüm) yayınlanıp kurulduktan sonra canlı test edilecek.
