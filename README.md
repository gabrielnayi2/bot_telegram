# bot_telegram

Bot de Telegram en Python para registrar operaciones financieras con flujos paso a paso (wizard).

## Stack

- Python 3.10+ (recomendado 3.11+)
- `python-telegram-bot==21.6`
- SQLAlchemy (ORM)
- Pydantic (validaciones)
- DB por defecto: SQLite (`ops.db`)
- DB opcional: PostgreSQL via `DATABASE_URL`

## Funcionalidades principales

1. **Nueva operacion** (no mueve caja):
   - Genera saldo por cliente/contraparte.
   - Soporta pacto `PAGA_ARS` o `PAGA_USD`.
   - Puede aplicar comision fija por cliente.
2. **Registrar cobro** (mueve caja):
   - Reduce saldo a cobrar del cliente.
   - Incrementa caja efectiva.
3. **Registrar pago** (mueve caja):
   - Reduce saldo a pagar del cliente.
   - Disminuye caja efectiva.
4. **Liquidar ARS→USD** global por cliente (no mueve caja):
   - Convierte ARS a favor en USD a favor con TC indicado.
5. **Anular operacion** (`/void OP-...`):
   - Revierte asientos OP/COM con asientos `VOID`.
6. **Consultas**:
   - saldo por cliente
   - saldo de cajas
   - total de comisiones (HOUSE o por cliente)

## Modelo contable implementado

- `Ledger`: saldo por cliente y moneda.
  - `amount > 0`: a cobrar
  - `amount < 0`: a pagar
- `CashMovement`: movimientos de efectivo por caja.
  - `amount > 0`: entra efectivo
  - `amount < 0`: sale efectivo

Cajas disponibles:

- `R1_ARS`
- `R1_USD`
- `R2_ARS`
- `R2_USD`

Cliente interno de comisiones:

- `HOUSE` (se crea automaticamente al iniciar)

## Instalacion y ejecucion

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configura variables de entorno:

```bash
export TELEGRAM_BOT_TOKEN="TU_TOKEN_AQUI"
# Opcional (si no se define, usa sqlite:///ops.db):
# export DATABASE_URL="postgresql+psycopg://usuario:password@host:5432/bot_telegram"
```

Ejecuta el bot (polling):

```bash
python bot.py
```

## Carga inicial de clientes

Puedes cargar clientes desde Telegram con:

```text
/addcliente C001 "Cliente Uno"
/addcliente C002 "Cliente Dos"
/clientes
```

Tambien puedes vincular tu usuario de Telegram a un cliente:

```text
/setcliente C001
```

Esto permite que varios flujos tomen ese cliente por defecto.

## Comandos

- `/start` - ayuda inicial
- `/menu` - menu con botones
- `/setcliente C001`
- `/saldo [C001]`
- `/cajas`
- `/setcomision C001 1500` (usa 0 para desactivar)
- `/comisiones`
- `/comisiones C001`
- `/comisiones 2026-01-01 2026-01-31`
- `/comisiones C001 2026-01-01 2026-01-31`
- `/addcliente C001 "Nombre"`
- `/clientes`
- `/void OP-YYYYMMDD-C001-0001`
- `/cancel` (durante cualquier wizard)

## IDs humanos

- Operaciones: `OP-YYYYMMDD-<CLIENTE>-0001`
- Liquidaciones: `LQ-YYYYMMDD-<CLIENTE>-0001`

La secuencia es por cliente y fecha.

## Flujos wizard

### 1) Nueva operacion

Inputs:

- cliente (si no esta vinculado)
- contraparte
- monto_ars
- pacto (`PAGA_ARS` / `PAGA_USD`)
- usd_pactados (si corresponde)
- tc (opcional)
- nota
- confirmacion final

Asientos:

- `Ledger(cliente, ARS, +monto_ars, OP)`
- Si `PAGA_USD`: `Ledger(contraparte, USD, -usd_pactados, OP)`
- Si `PAGA_ARS`: `Ledger(contraparte, ARS, -monto_ars, OP)`
- Si comision activa:
  - `Ledger(cliente, ARS, -comision, COM)`
  - `Ledger(HOUSE, ARS, +comision, COM)`

### 2) Cobro

Inputs:

- cliente (si no esta vinculado)
- moneda (`ARS`/`USD`)
- caja compatible con moneda
- monto
- nota
- confirmacion final

Asientos:

- `Ledger(cliente, moneda, -monto, COBRO)`
- `CashMovement(caja, moneda, +monto, COBRO)`

### 3) Pago

Inputs:

- cliente (si no esta vinculado)
- moneda (`ARS`/`USD`)
- caja compatible con moneda
- monto
- nota
- confirmacion final

Asientos:

- `Ledger(cliente, moneda, +monto, PAGO)`
- `CashMovement(caja, moneda, -monto, PAGO)`

### 4) Liquidar ARS→USD (global)

Inputs:

- cliente (si no esta vinculado)
- monto ARS (numero o `todo`)
- tc (obligatorio)
- nota
- confirmacion final

Validaciones:

- saldo ARS del cliente debe ser `> 0`
- monto a liquidar `<=` saldo ARS disponible

Asientos:

- `Ledger(cliente, ARS, -monto_ars, LIQ)`
- `Ledger(cliente, USD, +(monto_ars/tc), LIQ)`

No mueve caja.

## Anulacion de operacion

Con `/void OP-...`:

- solo permite operaciones en `OPEN`
- revierte asientos `OP` y `COM` con asientos `VOID`
- cambia estado de operacion a `VOID`

## Variables de entorno

- `TELEGRAM_BOT_TOKEN` (obligatoria)
- `DATABASE_URL` (opcional)

Ejemplos:

- SQLite por defecto: no definir `DATABASE_URL`
- PostgreSQL:
  - `postgresql+psycopg://user:pass@localhost:5432/bot_telegram`
  - tambien acepta `postgres://...` y lo normaliza automaticamente

## Notas de produccion basica

- No se incluyen credenciales en codigo.
- Manejo de errores con mensajes amigables y logging.
- Todas las tablas se crean automaticamente al iniciar.
- `HOUSE` se asegura automaticamente en el arranque.
