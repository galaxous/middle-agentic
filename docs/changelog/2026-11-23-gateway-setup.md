# 2026-11-23 · Gateway'i repodan kurtarma: `magent gateway-setup` · commit bekliyor

## Durum

`commit bekliyor`. Gateway Mac'te bir kez çalıştırılacak; sonraki güncellemeler sürüm kurulumuyla yapılır.

## Değişenler

- **`magent gateway-setup [--from-repo PATH] [--force]`**: gateway'i repo çalışmasından kurulu pakete taşır.
  1. Eski servisi durdurur (veritabanı yazılırken kopyalanmasın).
  2. `agent_memory.db`'yi SQLite yedekleme API'siyle kopyalar (WAL içindeki veri dahil). Repo'daki dosya değişmez.
  3. `.env`'i 0600 izinle kopyalar; `MEMORY_DB_PATH` satırını düşürür (yeni konum kullanılsın diye).
  4. `config.yaml`'ı kopyalar (ev dizininde yoksa).
  5. Servisi kurulu paketle kurar ve başlatır (`magent service install`).
  6. `/health` yanıtını bekler.
- Mevcut dosyalar `--force` olmadan üzerine yazılmaz.
- **Sürüm güncellemesi** (gateway'de): `pipx install --force <wheel>` ardından `magent service install` (yeni sürümle yeniden başlatır).

## Doğrulama

- Testler: 170 geçiyor. Repo dosyalarının değişmediği, WAL verisinin kopyaya geçtiği, `MEMORY_DB_PATH`'in düştüğü, `.env` izninin 0600 olduğu, mevcut veritabanının `--force` olmadan korunduğu ve servis sırasının (durdur → kopyala → kur → sağlık) doğru olduğu kontrol ediliyor.
- Gerçek gateway Mac'te henüz çalıştırılmadı.

## Gateway Mac adımları

```bash
cd ~/middle-agentic && git pull                                # kodu al (repo sadece okunur kalır)
pipx install --force ~/middle-agentic                          # bir kez: kurulu kopya oluşturur
~/.local/bin/magent gateway-setup --from-repo ~/middle-agentic
curl -s http://127.0.0.1:8000/health
```

Sonraki sürümler:

```bash
pipx install --force <yeni wheel URL'si veya yolu>
magent service install
```

## Açık kalanlar

- `magent update` gibi bir komut (son sürümü otomatik bulup doğrulayarak kurmak) henüz yok.
- Eski repo tabanlı launchd girdisi `gateway-setup` ile değiştirilir; elle düzenlenmiş bir plist varsa ayrıca kontrol edin.
