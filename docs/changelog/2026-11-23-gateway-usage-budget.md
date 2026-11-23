# 2026-11-23 · Gateway: kullanım kaydı, günlük bütçe, admin uçları (v0.5.0) · commit bekliyor

## Durum

`commit bekliyor`. Gateway tarafı yazıldı ve testlerle doğrulandı (203 geçiyor). Client tarafı için ayrı changelog var: [2026-11-23-client-usage-budget.md](2026-11-23-client-usage-budget.md). Yayın (v0.5.0) ve gateway makinesinde kurulum henüz yapılmadı.

## Sorun

- Her model çağrısının maliyeti ve tokenı kaydedilmiyordu; işçi agent'ların ne harcadığı görülemiyordu.
- Günlük harcama sınırı yoktu. Otonom bir işçi, fiyatı bilinmeyen bir modelle sınırsız harcayabilirdi.
- Harcamayı, modelleri ve fiyatları görmek için yetkili bir okuma yolu yoktu.

## Değişenler

- **Kullanım kaydı** (`server/usage.py`): aynı SQLite dosyasında `usage` tablosu. Her model denemesi için bir satır: anahtar adı, iş etiketi, tier, model, token sayıları, maliyet, maliyetin kaynağı, gecikme, sonuç (`ok`, `failed`) ve akış bayrağı. Başarısız deneme 0 maliyetle kaydedilir.
- **Maliyet önceliği:** tier'in `price_in` / `price_out` değeri (USD / 1M token) > LiteLLM fiyat tablosu (akış olmayan yanıtlar) > yerel ve ücretsiz tier'ler için 0 > bilinmeyen için 0 ve `cost_src: unknown`.
- **Akış kullanımı:** akış isteklerinde `stream_options.include_usage` açılır. Sağlayıcı kullanım göndermezse token sayısı tahmin edilir (`tier-estimated`). İstemci bağlantıyı kopartsa da satır yazılır (`aborted`).
- **Günlük bütçe** (`server/budget.py`, `config.yaml` → `budget:`):
  - `daily_usd` varsayılan 1.0; 0 sınırı kapatır. Gün, gateway makinesinin yerel günüdür.
  - `on_exceed: downgrade` (varsayılan): sınır dolunca yalnız `free` ve `local` tier'ler çalışır. Etiketi `custom` olan ya da fiyatı tanımlı tier'ler ücretli sayılır.
  - `on_exceed: block`: sınır dolunca 429 (`budget_exceeded`); sağlayıcıya hiç istek gitmez.
  - İstek başlığı `X-Budget-USD` yalnız düşürür; daha yüksek bir değer sınırı yükseltmez. Geçersiz değer 400.
  - Yanıt başlığı `X-Gateway-Budget`: `ok`, `downgraded` ya da `blocked`.
- **İş etiketi:** `X-Job` başlığı, usage raporlarında gruplamak için kaydedilir (en fazla 64 karakter; yoksa `-`).
- **Admin uçları** (`server/routes_admin.py`), yalnız `admin` kapsamıyla:
  - `GET /admin/usage?by=tier|key|job|day&days=N`
  - `GET /admin/budget`: bugünkü harcama, sınır ve politika
  - `GET /admin/models`: tier'ler, fiyatlar ve durum. `overridden` her zaman `false`; çalışma zamanı değişikliği henüz yok.
- **`admin` kapsamı** (`server/api_keys.py`): yeni kapsam; varsayılan olarak hiçbir anahtara verilmez.
- **TierConfig:** `price_in`, `price_out` alanları (isteğe bağlı, ≥ 0).

## Doğrulama

- `./venv/bin/python -m pytest -q`: 203 geçiyor.
- Yeni testler (`tests/test_usage_budget.py`, 13 test): kayıt ve iş etiketi, maliyet hesabı, downgrade ve block davranışı, başlığın yalnız düşürmesi, geçersiz başlık, akış kaydı, başarısız denemenin 0 maliyetle kaydı, admin kapsam denetimi, fiyatların raporu, `by` parametresinin doğrulanması.
- Bu sırada akış yolunda gerçek bir hata yakalandı ve düzeltildi: `stream()` içindeki `acompletion` çağrısı `stream=True` almıyordu.
- `tests/test_scopes.py`: bilinmeyen kapsam örneği `admin`'den `root`'a çevrildi, çünkü `admin` artık geçerli bir kapsam.
- Canlı gateway ile test edilmedi; v0.5.0 yayınlanıp gateway makinesine kurulduktan sonra yapılacak.

## Açık kalanlar

- Bütçe kontrolü ile maliyet yazımı arasında küçük bir aşım payı olabilir: bir istek, kalan bütçeden pahalıysa yine tamamlanır. Bunu önlemek için tahmini rezervasyon gerekir; sonraki sürüme bırakıldı.
- Fiyatı olmayan ve LiteLLM'in de bilmediği ücretli modeller `cost_src: unknown` ile 0 kaydedilir. Bu modeller bütçeyi aşabilir. Tier'e `price_in` / `price_out` eklenmeli.
- Gün sınırı yerel saate göre; gateway'in saat dilimi değiştirilirse gün de değişir. Ayrı bir saat dilimi ayarı yok.
- Admin üzerinden model ekleme/kapatma ve bütçe değiştirme yok; yalnızca okuma.
- Anahtar başına bütçe yok; sınır genel (tüm anahtarlar için toplam). Anahtar başına sınır sonraki sürüme.
- `magent usage`, `budget`, `models` komutlarının gateway tarafındaki çıktısı, v0.5.0 gateway kurulduktan sonra client üzerinden doğrulanacak.
