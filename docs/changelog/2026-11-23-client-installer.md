# 2026-11-23 · İstemci kurulum sihirbazı ve GitHub Release kurulumu · v0.3.0

## Durum

`commit bekliyor` (yayın için commit ve push gerekli; yayın etiketi bu commit'e işaret eder).

## Değişenler

- **`magent client-setup`** (etkileşimli): gateway adresini ve API anahtarını sorar, önce doğrular, sonra onay ister.
  - Doğrulama: `/health` (gerçek bir magent gateway'i mi), MCP `initialize` (anahtar kabul ediliyor mu), `/v1/memory/context` (memory:read var mı).
  - Herhangi bir doğrulama başarısızsa hiçbir şey yazılmaz.
  - Onaydan sonra: anahtar işletim sistemi deposuna, adres `~/.config/middle-agentic/.env`'e yazılır, `integrate all` çalışır, macOS'ta login-env kurulur.
- **Linux anahtar deposu**: D-Bus oturumu ve `secret-tool` varsa libsecret; yoksa (Raspberry Pi SSH gibi) `~/.config/middle-agentic/secrets/` altında 0600 izinli dosya.
- **Kurulum betikleri** (GitHub Release'te): `install.sh` (macOS, Linux, Raspberry Pi) ve `install.ps1` (Windows). Betik pipx'i kurar, wheel'i indirir, SHA-256'sını betiğe gömülü değerle karşılaştırır, eşleşmezse durur, sonra `magent client-setup` çalıştırır.
- **Yayın derlemesi**: `scripts/build_release.py` wheel'i üretir ve betiklere wheel'in SHA-256'sını yazar.
- **Sürüm**: 0.3.0.

## Doğrulama

- Testler: 165 geçiyor. Sihirbaz (başarı, iptal, anahtar reddi, sağlık hatası, memory:read eksik), Linux deposu (secret-service ve dosya yolu, 0600 izni) ve URL normalizasyonu kapsanıyor.
- `install.sh` sözdizimi (`sh -n`) geçiyor. Wheel içinde `bridge/magent_hook.py` ve `setup_client.py` var.
- Betiklerdeki SHA-256, üretilen wheel'in hash'iyle birebir aynı.
- **Doğrulanmadı:** `install.ps1` bu makinede çalıştırılmadı (PowerShell yok). Raspberry Pi ve Windows üzerinde gerçek kurulum denenmedi.

## Açık kalanlar

- Windows'ta `install.ps1` gerçek makinede denenmeli.
- Raspberry Pi'de pipx ve dosya tabanlı anahtar deposu gerçek cihazda denenmeli.
- Yayınlanan betik tek kanaldan (GitHub) geliyor; imza (sigstore) ileride eklenebilir.
