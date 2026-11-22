# 2026-11-22 · Gateway anahtarları: istemci başına anahtar, Keychain, GATEWAY_API_KEY kaldırıldı · commit bekliyor

## Durum

`commit bekliyor`. Çalışma ağacında; `main` üzerinde commit yok. Kararlar: anahtarlar Keychain'de, her istemci için ayrı anahtar, `GATEWAY_API_KEY` tamamen kaldırıldı.

## Değişenler

- **Anahtar deposu** (`middle_agentic/server/api_keys.py`): anahtarlar gateway'in SQLite veritabanında yalnızca SHA-256 özetiyle tutulur. Düz metin anahtar üretildiği anda bir kez gösterilir.
- **`magent key create|list|revoke`**: gateway makinesinde çalışır. İptal anında etkili olur.
- **Auth**: her REST ve MCP isteği anahtar deposunda aktif bir anahtar arar. Paylaşılan gizli anahtar yok.
- **`GATEWAY_API_KEY` kaldırıldı**: config alanı, env eşlemesi ve `magent setup` içindeki otomatik üretim silindi. `.env` içindeki eski satır artık etkisiz.
- **İstemci anahtarı**: `magent token set gateway` (Keychain/DPAPI). `MAGENT_API_KEY` ortamda varsa önce o kullanılır.
- **LaunchAgent** (macOS): `magent token login-env` anahtarı her girişte oturum ortamına alır; GUI uygulamaları bu sayede görür. Plist anahtarın kendisini değil, `security` komutunu içerir.
- **Hook betiği**: anahtarı ortamdan, yoksa kurulumdaki Python ile token deposundan okur. Anahtar yazdırılmaz.
- **MCP kurulumu**: `magent integrate` Cursor, Claude Code ve Codex için HTTP girdisi yazar. Adres `MAGENT_URL` / `MAGENT_DEFAULT_NODE` çözümünden gelir, anahtar `${MAGENT_API_KEY}` (Codex için `bearer_token_env_var`) olarak referans verilir. Antigravity ve Continue stdio'da kalır.
- **Testler**: 129 geçiyor. Yeni: anahtar deposu, iptal, CLI yaşam döngüsü, MCP ve REST auth, LaunchAgent plist'i.

## Doğrulama

- `magent key create|list|revoke` geçici bir veritabanında uçtan uca çalıştı; anahtar yalnızca bir kez yazdırıldı, iptal sonrası listede `revoked` göründü.
- Gerçek gateway Mac'te, Cursor'da ve Keychain'de uçtan uca doğrulama yapılmadı.

## Açık kalanlar

- Gateway Mac: kodu çek, `magent key create <ad>` ile istemci anahtarlarını üret, `magent integrate all` ile MCP girdilerini yeniden yaz, servisi yeniden başlat.
- İstemci Mac'ler: `magent token set gateway` ile anahtarı gir, `magent token login-env` çalıştır, `magent integrate all` ile girdileri yeniden yaz, Cursor'u tamamen kapatıp aç.
- Eski `GATEWAY_API_KEY` değerleri artık çalışmaz; tüm istemciler yeni anahtarlara geçmeli.
- Continue ve Antigravity için HTTP MCP desteği ortam değişkenini doğrulayana kadar stdio'da kalıyor.
- Hook başına Keychain okuması gerekiyorsa her hook çağrısı birkaç yüz milisaniye ekler. Gerekirse hook betiğine kısa süreli önbellek eklenebilir.
