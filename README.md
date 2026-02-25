# bot_telegram

Bot de Telegram en Python para registrar operaciones financieras.
Modo principal: alta de operaciones por mensaje estructurado de 4 lineas (sin botones).

## Stack

- Python 3.10+ (recomendado 3.11+)
- `python-telegram-bot==21.6`
- SQLAlchemy (ORM)
- Pydantic (validaciones)
- DB por defecto: SQLite (`ops.db`)
- DB opcional: PostgreSQL via `DATABASE_URL`

## Funcionalidades principales

1. **Nueva operacion por mensaje estructurado** (no mueve caja):
   - El bot procesa automaticamente mensajes de 4 lineas:
     - `CLIENTE_ENVIA`
     - `CLIENTE_RECIBE`
     - `TC`
     - `IMPORTE`
   - Genera fecha e ID de operacion de forma automatica.
   - Aplica reglas por TC y comision porcentual por cliente.
2. **Alta de clientes con ID automatico**:
   - Alta guiada (`/nuevocliente`) y alta masiva (`/importclientes`).
   - IDs correlativos: `C001`, `C002`, ...
   - Eliminacion segura (`/delcliente C001`) solo si no tiene historial.
3. **Registrar cobro** (mueve caja):
   - Reduce saldo a cobrar del cliente.
   - Incrementa caja efectiva.
4. **Registrar pago** (mueve caja):
   - Reduce saldo a pagar del cliente.
   - Disminuye caja efectiva.
5. **Liquidar ARS→USD** global por cliente (no mueve caja):
   - Convierte ARS a favor en USD a favor con TC indicado.
6. **Anular operacion** (`/void OP-...`):
   - Revierte asientos OP/COM con asientos `VOID`.
7. **Consultas**:
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
# Compatibilidad temporal (si creaste el secret con typo):
# export ELEGRAM_BOT_TOKEN="TU_TOKEN_AQUI"
# Opcional (si no se define, usa sqlite:///ops.db):
# export DATABASE_URL="postgresql+psycopg://usuario:password@host:5432/bot_telegram"
```

Ejecuta el bot (polling):

```bash
python bot.py
```

## Carga inicial de clientes

Opciones recomendadas:

```text
/nuevocliente
# (el bot pide nombre y comision %, y genera ID automatico: C001, C002, ...)

/importclientes
Cliente Uno;1.5
Cliente Dos;sin
Cliente Tres;0

/clientes
```

Validacion de nombres:

- No se permite crear dos clientes con el mismo nombre (comparacion case-insensitive, sin acentos y sin duplicar espacios).

Tambien existe comando legacy manual:

```text
/addcliente C001 "Cliente Uno"
```

Tambien puedes vincular tu usuario de Telegram a un cliente:

```text
/setcliente C001
```

Esto permite que varios flujos tomen ese cliente por defecto.

## Comandos

- `/start` - ayuda inicial
- `/menu` - ayuda rapida (sin botones)
- `/formato` - muestra plantilla de carga estructurada
- `/setcliente C001`
- `/saldo [C001]`
- `/cajas`
- `/setcomision C001 1.5` (porcentaje; usa 0 para desactivar)
- `/comisiones`
- `/comisiones C001`
- `/comisiones 2026-01-01 2026-01-31`
- `/comisiones C001 2026-01-01 2026-01-31`
- `/nuevocliente` (alta guiada con ID automatico)
- `/importclientes` (alta masiva por listado)
- `/delcliente C001` (elimina cliente sin movimientos)
- `/addcliente C001 "Nombre"` (legacy/manual)
- `/clientes`
- `/void OP-YYYYMMDD-C001-0001`
- `/cancel` (durante flujos wizard legacy)

## IDs humanos

- Clientes: `C001`, `C002`, ... (auto-generados por el bot)
- Operaciones: `OP-YYYYMMDD-<CLIENTE>-0001`
- Liquidaciones: `LQ-YYYYMMDD-<CLIENTE>-0001`

La secuencia es por cliente y fecha.

## Registro de operacion por mensaje (recomendado)

Envia un mensaje de **4 lineas**:

```text
CLIENTE_ENVIA
CLIENTE_RECIBE
TC
IMPORTE
```

Ejemplo:

```text
C001
C002
1250
150000
```

Tambien puedes usar etiquetas:

```text
CLIENTE ENVIA: C001
CLIENTE RECIBE: C002
TC: 1250
IMPORTE: 150000
```

Reglas aplicadas por el bot:

- Fecha de operacion: fecha del mensaje recibido.
- ID humano: `OP-YYYYMMDD-<CLIENTE_ENVIA>-0001` (correlativo por cliente y dia).
- Si `TC = 1`:
  - `CLIENTE_ENVIA` queda en ARS a pagar (`-IMPORTE`).
  - `CLIENTE_RECIBE` queda en ARS a cobrar (`+IMPORTE`).
- Si `TC > 1`:
  - `CLIENTE_ENVIA` queda en ARS a pagar (`-IMPORTE`).
  - `CLIENTE_RECIBE` queda en USD a cobrar (`+(IMPORTE/TC)` redondeado a 2 decimales).
- Si `CLIENTE_ENVIA` tiene comision activa (`/setcomision`), se aplica automaticamente:
  - comision ARS = `IMPORTE * (%/100)`
  - el saldo a pagar de `CLIENTE_ENVIA` se reduce por esa comision.
- No permite cargar dos operaciones iguales en la misma fecha (misma dupla cliente/contraparte, monto, pacto, tc/usd pactados).

## Flujos wizard (legacy / opcionales)

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
  - `comision = monto_ars * (%/100)`
  - `Ledger(cliente, ARS, -comision, COM)`  (flujo legacy)
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
- `ELEGRAM_BOT_TOKEN` (fallback compatible por typo; recomendado corregir a TELEGRAM_BOT_TOKEN)
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
