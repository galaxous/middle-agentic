# 2026-11-23 · Installer düzeltmesi: curl | sh ve Homebrew yan etkisi · v0.4.1 (yayınlanmadı)

## Durum

`commit bekliyor`. Kod ve testler hazır (176 geçiyor). Sürüm etiketi `v0.4.1` henüz oluşturulmadı; yayın için onay bekleniyor.

## Sorun

Gateway Mac'te `curl -fsSL …/install.sh | MAGENT_DATA_DIR=… sh -s -- --role gateway` komutu yarıda kaldı:

1. `brew install pipx` stdin'den okuyup betiğin kalan kısmını tüketti. Gateway kurulumu hiç çalışmadı.
2. Homebrew, pipx'in bağımlılıklarıyla `python@3.15`, `openssl@4` ve `lz4` kurdu ve `/opt/homebrew/bin/python3` bağlantılarını değiştirdi.
3. Bu sırada eski gateway servisi zaten durdurulmuştu; `gateway-setup` çalışmadığı için servis kapalı kaldı (geri açma adımı: `cd ~/middle-agentic && ./venv/bin/magent service install`).

## Düzeltme

- Betiğin tamamı `main()` içinde; `curl | sh` ile çalışırken sh komutları okurken betik tüketilemez.
- pipx Homebrew ile değil, kendi venv'inde kuruluyor (`~/.local/share/magent-pipx`). Homebrew'ın Python'ı değişmez.
- `python3 -m venv` Debian/Raspberry Pi'de yoksa `python3-venv` apt ile kurulur.
- stdin tüketebilecek komutlar `</dev/null` ile çalışır (`curl`, `pipx`, `gateway-setup`).
- `build_release.py` `dist/` içindeki eski wheel'leri siler. Bu sorun yerelde yakalandı: eski 0.4.0 wheel'i yakalandı ve SHA-256 kontrolü kurulumu durdurdu.

## Doğrulama

- Testler: 176 geçiyor. Yeni testler: betiğin `main()` içinde olduğu, Homebrew kullanmadığı ve stdin korumasının olduğu.
- Yerel sandbox (gerçek HOME değil, geçici klasör, gateway-setup taklit edildi):
  - pipx zaten varken: SHA doğrulandı, magent 0.4.1 kuruldu, gateway-setup çağrıldı.
  - pipx yokken: pipx kendi venv'inde kuruldu, magent 0.4.1 kuruldu, gateway-setup çağrıldı.
- Gerçek gateway Mac'te v0.4.1 henüz denenmedi.

## Açık kalanlar

- v0.4.1 yayınlanmadan gateway Mac bu düzeltmeyi kullanamaz.
- Gateway'in yeniden açılması (`magent service install` ile repo tabanlı servisi geri kurmak) yayından önce yapılmalı.
