# 2026-11-23 · README kurulum akışı: repo olmadan ağ, SSH ve magent · commit bekliyor

## Durum

`commit bekliyor`. Yalnızca dokümantasyon değişti; kod değişmedi.

## Değişenler

- README'nin "Hızlı kurulum" bölümü yeniden yazıldı. Eski `bootstrap.py` akışı (repo klonlama) ayrı bir "Repo ile geliştirme" bölümüne taşındı.
- **Adım 1 (ağ):** `scripts/bootstrap-unix.sh` ve `scripts/bootstrap-windows.ps1`, v0.4.0 etiketinden (`raw.githubusercontent.com`) çağrılır. SSH, Tailscale ve gerekiyorsa Ollama kurulur; `--skip-venv --skip-service` ile repo ve gateway servisi gerekmez.
- **Adım 2 (magent):** `install.sh --role gateway` / `--role client` ve Windows karşılıkları, v0.4.0 Release'inden.
- **Güncelleme:** aynı kurulum komutu yeni etiketle yeniden çalıştırılır.

## Doğrulama

- Komutlardaki parametreler `bootstrap-unix.sh` ve `bootstrap-windows.ps1` dosyalarının parametre tanımlarıyla karşılaştırıldı.
- Raw URL'ler ve Release dosyaları anonim olarak erişilebilir (public repo).
- Gerçek makinelerde çalıştırılmadı; Windows ve Raspberry Pi adımları denenmedi.

## Açık kalanlar

- `bootstrap-unix.sh` ile `install.sh`'ı tek komutta birleştirmek (ağ + magent) ileride yapılabilir; şu an iki ayrı komut.
