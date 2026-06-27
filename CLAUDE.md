# Модуль wms — контекст для Claude

**Тип:** git submodule → https://github.com/aidzmitry-gif/SKL-5.git (правка = коммит в этот репозиторий, а не в суперпроект)
**API-префикс:** `/wms`
**Схема БД:** `wms`
**Статус:** рабочий. Журнал движений + воронка операций + **остатки-зеркало 1С (read-only)** + **инвентаризация** (документ сверки против 1С). RBAC объявлен (`wms.read`/`wms.count`). Нет workflow/telegram/on_startup; события только потребляются, наружу не публикуются.

> **Контракт остатков (жёстко):** 1С/integrations = истина остатка (фаза 1), WMS только ДУБЛИРУЕТ движения и ЧИТАЕТ остаток через фасад `core.services.stock` (`stock_by_sku`). В 1С НЕ пишем; корректировка остатков 1С заморожена до фазы 2. Не трогать `core.services.stock`, `modules/integrations`, shared-kernel.

## Новое (2026-06-27, полоса Склад/WMS)
- **Остатки-зеркало:** `GET /wms/stock` (`require_permission("wms.read")`) — перебор `Sku` из shared-kernel + `stock_by_sku` по каждому (N+1, ponytail: bulk-метод на StockGateway — будущее). `gateway:false`, если integrations выключен. Фронт: `app/erp/wms/stock`, `components/erp/wms-stock-table.tsx`, `lib/wms-stock.ts`.
- **Инвентаризация:** таблицы `wms.inventory_count`/`inventory_line` (миграция **0058**, down=0057). Ожидаемое — снимок из 1С через шлюз, факт вносит кладовщик, расхождение/деньги — на чтение. Эндпоинты `/wms/inventory*` (чтение — `wms.read`, запись — `wms.count`): list/create/detail/populate/lines/complete. Фронт: `app/erp/wms/inventory[/[id]]`, `components/erp/wms-inventory-{list,detail}.tsx`, `lib/wms-inventory.ts`.
- **RBAC:** `permissions.py` — `wms.read` (warehouse/logistics/production/hr), `wms.count` (warehouse). Защита роутов канон `Depends(require_permission(...))`.
- **Складской поток приёмка→QC→put-away→pick→pack→отгрузка + деньго-защита (2026-06-28):**
  - **Приёмка+QC** (миграция **0064**): `Receipt`/`ReceiptLine`. `on_goods_received` теперь рождает документ `pending_qc` (БЕЗ движения, `entity_ref` сохранён). `/wms/receipts`(+`{id}`/`/qc`/`/accept`). `accept` пишет приход по принятому (брак НЕ на свободный остаток), идемпотентен по статусу, авто-создаёт put-away.
  - **Задачи** (миграция **0066**): `Task` (putaway|pick). `/wms/tasks`(+PATCH). Завершение putaway→transfer приёмная→постоянная (нужна to_location), pick→out reason=pick. `on_stock_reserved` доп. создаёт pick-задачу. `/wms/pack` — balance-нейтральная пара reason=pack.
  - **Сверка с 1С** `/wms/reconciliation`: WMS (in−out) vs 1С qty_available, diff×себес, сорт по |деньгам|; gateway=false честно.
  - **Low-stock** (миграция **0068**): `StockThreshold`, `/wms/thresholds`, `/wms/alerts` (free=available−reserved < min → severity out_of_stock/below_min). Заявку в закупку НЕ дёргаем (граница модулей — TODO событие).
  - **Цикл-каунт** (миграция **0069**): `CycleCountPlan`, `/wms/cycle-plans`(+`/run`→создаёт InventoryCount, populate из 1С, сдвиг next_due_date). populate вынесен в `_fill_inventory_from_1c`.
  - **Дашборд** `/wms/dashboard`: очередь QC/задачи/low-stock/инвентаризации/сверка/движения сегодня.
  - Фронт: `/erp/wms` = дашборд (воронка → `/operations`); экраны receipts(+detail), tasks, reconciliation, alerts, cycle-counts; этикетки ячеек (Code 39, `lib/barcode.ts`). Провенанс 1С — через `<SourceTag source="1c">`. Все новые ручки под `require_permission` (read=`wms.read`, write=`wms.count`).
- **Операционное ядро (движения/остаток/ячейки):** миграция **0059** (down=0058) — таблица `wms.location` (зоны/ячейки) + новые колонки `wms.stock_movement` (`reason`/`location_id`/`batch_ref`/`doc_ref`/`note`). Операции (все — движения в журнал WMS, 1С не трогаем): `POST /wms/receipt|shipment|transfer|adjustment` (`wms.count`). Перемещение = пара out@from+in@to с общим `doc_ref=TRF-…`. `GET /wms/balances` — оперативный остаток = знаковая сумма движений (in−out) по (SKU, склад, ячейка, партия); это ТЕНЕВОЙ учёт, сверять с 1С (≠ /wms/stock = зеркало 1С). Ячейки: `GET/POST /wms/locations`, `PATCH /wms/locations/{id}`. События: добавлен `on_stock_released` (sales.stock.released → приход reason=release); `on_*` теперь проставляют `reason`. Проведение инвентаризации пишет корректирующие движения (`reason=adjustment`) в журнал WMS (не в 1С). Фронт: `app/erp/wms/{movements,balances,locations}`, `components/erp/wms-{movements,balances,locations}.tsx`, `lib/wms-ops.ts`(+test).

## Назначение
Складской модуль (WMS): ведёт журнал движений по складу (приход/расход SKU) и
воронку складских операций по логистическому циклу (ожидание поступления → приёмка →
контроль качества → размещение → подготовка → отгрузка). Журнал движений наполняется
не только напрямую через API, но и реактивно — событиями других модулей (резерв из
sales, приёмка из закупок/производства).

## Файлы
- `module.py` — `WmsModule(ModuleContract)` + фабрика `get_module()`; `register()` подключает роутер, 3 подписки и виджет.
- `models.py` — 2 ORM-модели схемы `wms`: `StockMovement`, `WarehouseOp`.
- `schemas.py` — Pydantic-схемы API (создание/чтение движений и операций, смена стадии).
- `routes.py` — весь HTTP-API (`router`, tags=`["wms"]`); монтируется под `/wms`.
- `events.py` — обработчики событий шины: `on_stock_reserved`, `on_goods_received`.
- `stages.py` — `STAGES`: 7 стадий воронки операций (источник истины для доски/группировки).
- `__init__.py` — пустой докстринг-маркер пакета.

## Что регистрирует в ядре (register())
- Роуты: префикс `/wms` (движения, операции, плоский список, доска-воронка).
- Подписки на события:
  - `sales.stock.reserved` → `on_stock_reserved` (резерв под заказ → расход)
  - `procurement.received` → `on_goods_received` (приёмка из закупок → приход)
  - `production.completed` → `on_goods_received` (выпуск из производства → приход)
- Widget: `Widget("wms", "Склад", source="wms.movements")`.
- Permissions / roles / workflow / telegram / on_startup — **не регистрируются**.

## События
- **Публикует**: нет (модуль не вызывает `event_bus.emit`).
- **Подписан на**:
  - `sales.stock.reserved` → `on_stock_reserved`
  - `procurement.received` → `on_goods_received` ✅ (подписан)
  - `production.completed` → `on_goods_received` ✅ (подписан)

## Модель данных (таблицы схемы wms)
- **stock_movement** (`StockMovement`): журнал движений по складу. `sku_code`, `warehouse` (default «Главный»), `kind` (`in`|`out`), `qty` (Numeric 14,2), `created_at`. Без FK на SKU (мягкая связь по коду).
- **warehouse_op** (`WarehouseOp`): складская операция в воронке. `number`, `counterparty`, `title`, `items_count`, `amount` (Numeric 14,2), `zone`, `priority` (default «Средний»), `owner`, `stage` (default `inbound`), `op_date` (строка, nullable), `created_at`.

## API-эндпоинты (ключевые)
- `GET /movements` — журнал движений (DESC по id).
- `POST /movements` — зафиксировать движение по складу (201).
- `GET /ops` — складские операции плоским списком (DESC по id).
- `GET /board` — воронка: операции сгруппированы по стадиям (`build_board(STAGES, ...)` из `core.runtime.funnel`).
- `POST /ops` — создать операцию; если `number` пуст — генерируется `ОП-2026-{id:04d}` (201).
- `PATCH /ops/{op_id}` — сменить стадию операции (404 если не найдена).

## Межмодульные связи и зависимости
- Реагирует на события sales / procurement / production, превращая их в `StockMovement` (расход на резерв, приход на приёмку/выпуск).
- **StockGateway (`core.services.stock`) — НЕ используется.** Резерв из sales идёт штатно через шлюз на стороне integrations; wms лишь дублирует факт резерва в свой журнал движений как расход. Реальный учёт остатков (StockItem) — за integrations, не за wms.
- Ядро: `core.runtime.funnel` (`build_board`, `FunnelCard`, `FunnelBoardOut`), `core.runtime.deps.get_session`, `core.db.base.Base`.
- Связь с SKU — мягкая (`sku_code`/`item` из payload), без cross-schema FK.

## Подводные камни / детали
- **Коммит в роутах.** `POST /movements`, `POST /ops`, `PATCH /ops/{id}` сами вызывают `session.commit()` (+`refresh`) — отступление от паттерна «транзакцией владеет вызывающий код» (здесь роут и есть владелец, но коммит явный, не делегированный).
- **Обработчики событий — сигнатура `(payload, ctx)`** (2 параметра). Оба возвращаются молча при `ctx is None`. Они только `ctx.session.add(...)` движение — **commit не делают**: фиксацию выполняет цикл доставки шины (relay) в своей транзакции. Это отличается от стиля роутов.
- **`on_goods_received` един** для procurement и production; sku читается как `payload["sku_code"]` с fallback на `payload["item"]`, qty/warehouse — с дефолтами (`0` / «Главный»). `on_stock_reserved` итерирует `payload["items"]` (список `{sku_code, warehouse, qty}`).
- **Стадии воронки** (`stages.py`, id): `inbound`, `receiving`, `qc`, `putaway`, `picking`, `ready`, `shipped`. Модель по умолчанию `stage="inbound"`.
- В `_to_card` (routes.py) для стадий `receiving`/`qc` собирается soft-панель пересчёта (План/Принято/Отклонения) — пока заглушечная (Принято = План, Отклонения = «нет»). Подпись кнопки карточки задаётся словарём `_WMS_ACTIONS` по стадии.
- `op_date` — обычная строка (`String(32)`), не дата; форматирование на стороне клиента.
- Модуль не объявляет permissions/roles — RBAC по складу пока отсутствует.