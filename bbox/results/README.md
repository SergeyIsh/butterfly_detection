# Результаты bbox

Запускайте из корня `butterfly_detection`

## Аргументы

| Команда | Аргумент / опция | По умолчанию / назначение |
|---|---|---|
| `tools.py` | `-h`, `--help` | Общая справка; подкоманды `prepare`, `convert` |
| `prepare` | `--input CSV` | `data/bbox_input_all_400.csv` |
| `prepare` | `--metadata CSV` | `data/metadata_all_400.csv` |
| `prepare` | `--out DIR` | `bbox/results/tasks` |
| `prepare` | `-h`, `--help` | Справка подкоманды |
| `convert` | `INPUT_TSV_OR_JSONL` | Обязательный позиционный путь к ответам |
| `convert` | `--input CSV` | `data/bbox_input_all_400.csv`; источник ID и URL |
| `convert` | `--out OUTPUT_JSONL` | Обязательный путь к результату |
| `convert` | `-h`, `--help` | Справка подкоманды |

`prepare` создаёт `pilot.tsv` (20 фото), `main.tsv` (380 из исходных 400) и `manifest.json` с разбиением. Если результат уже существует, выберите новый `--out`: команды ничего не перезаписывают.

## Ответы и review

`convert` принимает нативный `.tsv` или JSONL с ровно шестью полями: `photo_id`, `worker_id`, `assignment_id`, `status`, `boxes`, `comment`. Выходной JSONL добавляет `image_url`; одна строка — один ответ на одно фото.

Столбцы TSV: `INPUT:photo_id`, `ASSIGNMENT:worker_id`, `ASSIGNMENT:assignment_id`, `OUTPUT:status`, `OUTPUT:boxes`, `OUTPUT:comment`. Если есть `INPUT:image_url`, он должен совпадать с исходным CSV.

Рамка: `left`, `top`, `width`, `height` — угол и размеры относительно полного изображения, в пределах `[0,1]`. Размеры положительны, рамка целиком в кадре.

| `status` | `boxes` | `comment` |
|---|---|---|
| `objects` | Одна или больше рамок | Строка |
| `empty` | `[]` | Строка |
| `load_error` | `[]` | Непустая строка |
| `uncertain` | `[]` или уверенные рамки | Непустая строка |

Сохраняйте исходные экспорты неизменными в `raw/`, экзамен — отдельно. Проверка скриптом подтверждает структуру, а не качество.

Все 400 фото требуют независимого review: настройте и проверьте запрет самопроверки по `worker_id`. Разберите `load_error`/`uncertain` и сохраните один проверенный итог на фото вместе с историей исправлений.
