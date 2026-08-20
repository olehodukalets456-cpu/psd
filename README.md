# PSD Meta → Google Sheets

Цей репозиторій замінює Make-сценарій для щоденного оновлення `raw_meta` у PSD Meta Dashboard.

## Що робить

- Забирає Meta Insights для `act_369275069245313` від `2026-01-01` до поточної дати.
- Рівень: campaign, `time_increment=1`, attribution: `7d_click + 1d_view`.
- Автоматично проходить усю Meta pagination, а не тільки перші 500 рядків.
- Зберігає ту саму логіку полів, що була в Make:
  - spend
  - impressions
  - reach
  - all clicks
  - site clicks
  - IG/other clicks = all clicks - site clicks
  - onsite_web_purchase
  - purchase value
  - campaign type: PSD / TRAFF / other
- Зберігає Make-фільтр `spend > 0`.
- Спочатку повністю отримує й перевіряє дані Meta, і тільки після цього очищає `raw_meta!A3:K`.
- Записує значення через Google Sheets API з `RAW`, тому `26.07` більше не перетвориться на дату.
- Оновлює дані великими пакетами, а не по одному рядку.

## GitHub Secrets

В репозиторій не комітяться токени або Google credentials. Потрібні два encrypted Actions secrets:

### `META_ACCESS_TOKEN`

Meta access token з попереднього Make HTTP-модуля.

### `GOOGLE_SERVICE_ACCOUNT_JSON`

Повний JSON ключ Google service account, який має доступ на редагування Google Sheet.

Після створення service account потрібно відкрити Google Sheet і дати email з поля `client_email` доступ **Editor**.

GitHub: `Settings → Secrets and variables → Actions → New repository secret`.

## Запуск

Ручний:

`Actions → Sync Meta to Google Sheets → Run workflow`

Автоматичний запуск заданий у `.github/workflows/sync-meta.yml` на `06:05 UTC` щодня. У літній час Києва це `09:05`.

## Безпека

Скрипт не очищає існуючий `raw_meta`, якщо Meta повернула помилку, порожню відповідь або невалідні дані. Access token не друкується в логах.
