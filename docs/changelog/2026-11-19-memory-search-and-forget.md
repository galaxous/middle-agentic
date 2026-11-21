# 2026-11-19 · FTS5 arama ve hafıza silme · main'de

## Durum

`main'de`. Commit'ler: `0c74270`, `5616bd8`, `b69e002`.

## Değişenler

- **FTS5 arama** (`0c74270`): hafıza aramasında FTS5 kullanılır. `decision` ve `fact` kayıtları sıralamada öne alınır. Hook URL çözümü ve `magent rules` bu commit'te geldi.
- **Hafıza silme** (`5616bd8`): `POST /v1/memory/forget` ve `magent hook forget`.
  - Varsayılan `dry_run`: yalnızca eşleşenleri listeler.
  - Silmek için `--yes` gerekir (API'de `dry_run: false`).
  - Bir soru silinince cevabı da gider; `--no-answers` cevabı korur.
  - Eşleştirme `--id` ile veya metin içeriğiyle yapılır, `--role` ile filtrelenir.
- **Dokümantasyon** (`b69e002`): README'ye forget komutları ve API satırı eklendi.

## Doğrulama

- Yerel testler: 73 geçti (forget testleri dahil).
- Gateway Mac eski kodda kaldığı için canlı silme 404 döndü; canlı doğrulama yapılmadı.

## Açık kalanlar

- Gateway Mac'te `git pull` ve servis yeniden başlatma yapılmalı.
- Deneme kayıtlarının (12 soru, 24 kayıt) silinmesi onay bekliyor. Karar ve fact kayıtlarına dokunulmayacak.
