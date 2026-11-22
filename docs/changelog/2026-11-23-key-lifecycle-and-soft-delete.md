# 2026-11-23 · Anahtar süresi, kotalar, istek kaydı, bellekte soft delete · commit bekliyor

## Durum

`commit bekliyor`. Çalışma ağacında; `main` üzerinde commit yok. Önceki kayıtlar: `2026-11-22-gateway-keys.md`, `2026-11-22-key-scopes.md`.

## Değişenler

- **Anahtar süresi**: yeni anahtarlar varsayılan 90 gün geçerli (`--expires-days`, 0 = süresiz). Süresi dolan anahtar 401 alır ve mesajı "expired" der. `magent key renew <ad> --expires-days N` ile yenilenir.
- **Kotalar**: `--per-minute` (süreç içinde sayılır) ve `--daily-limit` (UTC günü, istek kaydından sayılır). Aşımda 429. `magent key set-limits <ad>`; 0 kaldırır.
- **İstek kaydı (audit log)**: `api_audit` tablosu. Her HTTP isteği için anahtar adı (kimlik yoksa `-`), yöntem, yol, MCP araç adı, durum kodu ve istemci adresi. `/health` kaydedilmez. Başarısız denemeler de kaydedilir.
  - `magent key audit [ad] --hours 24`
  - `magent key audit-purge --older-than-days N --yes` (varsayılan kuru çalıştırma)
- **Soft delete**: `forget` artık satırı silmez; `deleted_at`, `deleted_by` (işlemi yapan anahtar adı) ve `delete_reason` yazar. Bellekteki tüm okuma yolları (arama, bağlam, oturum listesi, graf, istatistik) soft-deleted satırları dışarıda bırakır. Yeni bağlantılar da silinmiş düğümlere bağlanmaz.
  - `magent memory deleted [--session S]` ile incelenir.
  - `magent memory purge --older-than-days N --yes` ile saklama süresi dolanlar kalıcı silinir.
- **Eski veritabanları**: yeni kolonlar ve indeksler başlangıçta otomatik eklenir.
- **`forget` isteği**: `reason` alanı (en fazla 200 karakter) kabul eder.

## Doğrulama

- Testler: 154 geçiyor. `tests/test_lifecycle.py` süre dolumu, yenileme, dakika ve gün kotaları, istek kaydı (anahtar ve MCP araç adı dahil), `/health` hariç tutulması, soft-delete görünürlüğü, dry run, saklama temizliği, eski veritabanı migrasyonu ve `forget` ile işlemi yapan anahtarın kaydını kapsıyor.
- Geçici veritabanında CLI komutları çalıştırıldı.
- Bu sırada bir hata bulundu ve düzeltildi: eski veritabanlarında indeks, kolon eklenmeden oluşturulmaya çalışılıyordu.

## Açık kalanlar

- Dakika kotası süreç içinde sayılır; gateway birden çok süreçle çalışırsa her süreç ayrı sayar. Şu an tek süreç.
- Günlük kota, istek kaydını okuyarak sayılır; eşzamanlı isteklerde birkaç istek fazla geçebilir.
- Audit kaydı, gateway'in yerel veritabanında tutulur; dışa aktarma (örn. harici SIEM) yok.
- Silinmiş kayıtları görmek için uzaktan bir uç nokta yok; yalnızca gateway makinesinde CLI. Bu bilinçli bir tercih.
- Gateway'de saklama (retention) politikası otomatik çalışmıyor; `purge` elle çalıştırılır.
