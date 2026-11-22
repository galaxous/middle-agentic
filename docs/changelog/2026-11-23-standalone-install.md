# 2026-11-23 · Bağımsız kurulum: magent repo dışında çalışır · commit bekliyor

## Durum

`commit bekliyor`. Çalışma ağacında; `main` üzerinde commit yok.

## Değişenler

- **Ev dizini**: magent'in dosyaları (`.env`, `config.yaml`, `agent_memory.db`) artık `$MAGENT_HOME` veya `~/.config/middle-agentic` altında. Repo'nun kendi `.env`, `config.yaml` ve veritabanı kullanılmıyor.
- **Hook betiği paketin içine taşındı**: `middle_agentic/bridge/magent_hook.py`. `integrations/hooks/` kaldırıldı. Hook'lar ve `integrate` artık kurulu paketin dosyasını gösterir, repo'yu değil.
- **Gateway servisi**: çalışma klasörü repo yerine ev dizini. `magent service install` yeniden çalıştırılmalı.
- **`setup`**: `config.yaml` ve `.env` ev dizinine yazılır, `.env.example` kopyalanmaz.
- **Testler**: her test kendi geçici ev dizinini kullanır (`MAGENT_HOME`), gerçek `.env` veya veritabanı okunmaz. 154 geçiyor.

## Kurulum (her makine)

```bash
brew install pipx
cd <repo> && pipx install --force .          # kopyalar, repo'ya bağımlı kalmaz
mkdir -p ~/.config/middle-agentic
grep -E '^(MAGENT_URL|MAGENT_DEFAULT_NODE)=' .env > ~/.config/middle-agentic/.env
magent integrate all --scope user            # hook ve MCP yollarını kurulu pakete yazar
```

Gateway Mac'te ek olarak: `magent service install` ve veritabanını `~/.config/middle-agentic/agent_memory.db` konumuna taşıyın (gateway durdurulduktan sonra, `-wal` ve `-shm` dosyalarıyla birlikte), ya da `MEMORY_DB_PATH` ile mevcut yolu gösterin.

## Doğrulama

- `pipx install` sonrası kurulu paketin `config.py`, `integrations.py` ve hook betiği yolları repo dışında (`~/Library/Application Support/pipx/venvs/middle-agentic/...`).
- Testler: 154 geçti.

## Açık kalanlar

- Bu Mac'te `~/.config/middle-agentic/.env` dosyası henüz oluşturulmadı; `integrate` bu dosya olmadan adresi bulamaz.
- Repo güncellendiğinde kurulumu yenilemek için `pipx install --force <repo>` gerekir.
- Gateway'deki eski veritabanı taşınmadan gateway doğru veriyi görmez.
