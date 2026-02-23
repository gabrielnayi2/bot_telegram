#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import re
import shlex
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("bot_telegram")


HOUSE_CODE = "HOUSE"
ARS = "ARS"
USD = "USD"

PAGA_ARS = "PAGA_ARS"
PAGA_USD = "PAGA_USD"

STATUS_OPEN = "OPEN"
STATUS_VOID = "VOID"

ENTRY_OP = "OP"
ENTRY_COBRO = "COBRO"
ENTRY_PAGO = "PAGO"
ENTRY_LIQ = "LIQ"
ENTRY_COM = "COM"
ENTRY_VOID = "VOID"
ENTRY_AJUSTE = "AJUSTE"

CASHBOXES_BY_CURRENCY = {
    ARS: ["R1_ARS", "R2_ARS"],
    USD: ["R1_USD", "R2_USD"],
}
ALL_CASHBOXES = ["R1_ARS", "R1_USD", "R2_ARS", "R2_USD"]
CLIENT_CODE_RE = re.compile(r"^[A-Z0-9_]{2,32}$")


(
    OP_CLIENTE,
    OP_CONTRAPARTE,
    OP_MONTO_ARS,
    OP_PACTO,
    OP_USD_PACTADOS,
    OP_TC,
    OP_NOTA,
    OP_CONFIRM,
    CASH_CLIENTE,
    CASH_MONEDA,
    CASH_CAJA,
    CASH_MONTO,
    CASH_NOTA,
    CASH_CONFIRM,
    LIQ_CLIENTE,
    LIQ_MONTO_ARS,
    LIQ_TC,
    LIQ_NOTA,
    LIQ_CONFIRM,
) = range(19)


OP_DATA_KEY = "wizard_op"
CASH_DATA_KEY = "wizard_cash"
LIQ_DATA_KEY = "wizard_liq"


class Base(DeclarativeBase):
    pass


class Client(Base):
    __tablename__ = "clients"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    commission_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    commission_ars: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, unique=True)
    client_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("clients.code"), nullable=False, index=True
    )


class Operation(Base):
    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    op_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    cliente: Mapped[str] = mapped_column(String(32), ForeignKey("clients.code"), nullable=False)
    contraparte: Mapped[str] = mapped_column(
        String(32), ForeignKey("clients.code"), nullable=False
    )
    monto_ars: Mapped[float] = mapped_column(Float, nullable=False)
    pacto: Mapped[str] = mapped_column(String(16), nullable=False)
    usd_pactados: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_OPEN)
    nota: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_by_telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by_username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    raw_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)


class Ledger(Base):
    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    client_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("clients.code"), nullable=False, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entry_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CashMovement(Base):
    __tablename__ = "cash_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    cashbox: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    client_code: Mapped[Optional[str]] = mapped_column(
        String(32), ForeignKey("clients.code"), nullable=True, index=True
    )
    ref_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    move_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class OperationInput(BaseModel):
    cliente: str
    contraparte: str
    monto_ars: float = Field(gt=0)
    pacto: str
    usd_pactados: Optional[float] = Field(default=None, gt=0)
    tc: Optional[float] = Field(default=None, gt=0)
    nota: Optional[str] = None

    @field_validator("cliente", "contraparte")
    @classmethod
    def normalize_client(cls, value: str) -> str:
        return normalize_client_code(value)

    @field_validator("pacto")
    @classmethod
    def validate_pacto(cls, value: str) -> str:
        if value not in {PAGA_ARS, PAGA_USD}:
            raise ValueError("Pacto invalido.")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> "OperationInput":
        if self.cliente == self.contraparte:
            raise ValueError("Cliente y contraparte no pueden ser iguales.")
        if self.pacto == PAGA_USD and self.usd_pactados is None:
            raise ValueError("Si pacto=PAGA_USD debes indicar usd_pactados.")
        if self.pacto == PAGA_ARS:
            self.usd_pactados = None
        return self


class CashInput(BaseModel):
    client_code: str
    currency: str
    cashbox: str
    amount: float = Field(gt=0)
    note: Optional[str] = None
    move_type: str

    @field_validator("client_code")
    @classmethod
    def normalize_client(cls, value: str) -> str:
        return normalize_client_code(value)

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.upper().strip()
        if value not in {ARS, USD}:
            raise ValueError("Moneda invalida.")
        return value

    @field_validator("cashbox")
    @classmethod
    def normalize_cashbox(cls, value: str) -> str:
        return value.upper().strip()

    @field_validator("move_type")
    @classmethod
    def validate_move_type(cls, value: str) -> str:
        value = value.upper().strip()
        if value not in {ENTRY_COBRO, ENTRY_PAGO}:
            raise ValueError("Tipo de movimiento invalido.")
        return value

    @model_validator(mode="after")
    def validate_cashbox_currency(self) -> "CashInput":
        valid_boxes = CASHBOXES_BY_CURRENCY.get(self.currency, [])
        if self.cashbox not in valid_boxes:
            raise ValueError("La caja no corresponde a la moneda elegida.")
        return self


class LiquidationInput(BaseModel):
    client_code: str
    amount_ars: float = Field(gt=0)
    tc: float = Field(gt=0)
    note: Optional[str] = None

    @field_validator("client_code")
    @classmethod
    def normalize_client(cls, value: str) -> str:
        return normalize_client_code(value)


ENGINE = None
SessionLocal: Optional[sessionmaker] = None


def get_database_url() -> str:
    raw_url = os.getenv("DATABASE_URL", "sqlite:///ops.db")
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://") and "+psycopg" not in raw_url:
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


def init_db() -> None:
    global ENGINE, SessionLocal

    database_url = get_database_url()
    engine_kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    ENGINE = create_engine(database_url, **engine_kwargs)
    SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(ENGINE)

    with session_scope() as db:
        ensure_house_client(db)

    logger.info("DB inicializada en %s", database_url)


@contextmanager
def session_scope() -> Any:
    if SessionLocal is None:
        raise RuntimeError("La DB no fue inicializada. Ejecuta init_db() primero.")
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def normalize_client_code(raw: str) -> str:
    return raw.strip().upper()


def validate_client_code_format(code: str) -> bool:
    return bool(CLIENT_CODE_RE.match(code))


def parse_positive_number(raw: str, field_name: str = "valor", digits: int = 2) -> float:
    cleaned = raw.strip().replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field_name} invalido.") from exc
    if value <= 0:
        raise ValueError(f"{field_name} debe ser mayor a 0.")
    return round(value, digits)


def parse_optional_tc(raw: str) -> Optional[float]:
    cleaned = raw.strip().lower().replace(",", ".")
    if cleaned in {"", "-", "no", "none", "omitir", "skip"}:
        return None
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError("TC invalido.") from exc
    if value <= 0:
        raise ValueError("TC debe ser mayor a 0.")
    return round(value, 6)


def normalize_note(raw: str) -> Optional[str]:
    cleaned = raw.strip()
    if cleaned in {"", "-", "none", "sin", "ninguna"}:
        return None
    return cleaned


def next_sequence(existing_ids: list[str]) -> int:
    max_seq = 0
    for value in existing_ids:
        if not value:
            continue
        tail = value.rsplit("-", 1)[-1]
        if tail.isdigit():
            max_seq = max(max_seq, int(tail))
    return max_seq + 1


def compute_display_id(db: Session, op_date: date, client_code: str) -> str:
    prefix = f"OP-{op_date:%Y%m%d}-{client_code}-"
    rows = db.execute(
        select(Operation.display_id).where(Operation.display_id.like(f"{prefix}%"))
    ).scalars().all()
    seq = next_sequence(rows)
    return f"{prefix}{seq:04d}"


def compute_liq_id(db: Session, liq_date: date, client_code: str) -> str:
    prefix = f"LQ-{liq_date:%Y%m%d}-{client_code}-"
    rows = db.execute(
        select(Ledger.ref_id)
        .where(Ledger.entry_type == ENTRY_LIQ)
        .where(Ledger.ref_id.like(f"{prefix}%"))
        .distinct()
    ).scalars().all()
    seq = next_sequence(rows)
    return f"{prefix}{seq:04d}"


def ensure_house_client(db: Session) -> Client:
    house = db.get(Client, HOUSE_CODE)
    if house:
        return house
    house = Client(
        code=HOUSE_CODE,
        name="HOUSE",
        commission_enabled=False,
        commission_ars=0.0,
    )
    db.add(house)
    db.flush()
    return house


def get_linked_client_code(db: Session, telegram_user_id: int) -> Optional[str]:
    link = db.get(TelegramLink, telegram_user_id)
    if not link:
        return None
    return link.client_code


def get_client_balances(db: Session, client_code: str) -> dict[str, float]:
    balances = {ARS: 0.0, USD: 0.0}
    rows = db.execute(
        select(Ledger.currency, func.coalesce(func.sum(Ledger.amount), 0.0))
        .where(Ledger.client_code == client_code)
        .group_by(Ledger.currency)
    ).all()
    for currency, total in rows:
        balances[currency] = float(total or 0.0)
    return balances


def get_cashbox_balances(db: Session) -> dict[str, float]:
    balances = {box: 0.0 for box in ALL_CASHBOXES}
    rows = db.execute(
        select(CashMovement.cashbox, func.coalesce(func.sum(CashMovement.amount), 0.0))
        .group_by(CashMovement.cashbox)
    ).all()
    for cashbox, total in rows:
        balances[cashbox] = float(total or 0.0)
    return balances


def add_ledger_entry(
    db: Session,
    client_code: str,
    currency: str,
    amount: float,
    ref_id: str,
    entry_type: str,
    note: Optional[str] = None,
) -> None:
    db.add(
        Ledger(
            ts=datetime.utcnow(),
            client_code=client_code,
            currency=currency,
            amount=round(amount, 2),
            ref_id=ref_id,
            entry_type=entry_type,
            note=note,
        )
    )


def add_cash_movement(
    db: Session,
    cashbox: str,
    currency: str,
    amount: float,
    move_type: str,
    client_code: Optional[str],
    ref_id: Optional[str],
    note: Optional[str],
) -> None:
    db.add(
        CashMovement(
            ts=datetime.utcnow(),
            cashbox=cashbox,
            currency=currency,
            amount=round(amount, 2),
            client_code=client_code,
            ref_id=ref_id,
            move_type=move_type,
            note=note,
        )
    )


def apply_commission_if_any(
    db: Session,
    client: Client,
    ref_id: str,
    note: Optional[str] = None,
) -> float:
    if not client.commission_enabled:
        return 0.0
    amount = round(float(client.commission_ars or 0.0), 2)
    if amount <= 0:
        return 0.0
    ensure_house_client(db)
    note_text = note or "Comision fija por operacion."
    add_ledger_entry(
        db=db,
        client_code=client.code,
        currency=ARS,
        amount=-amount,
        ref_id=ref_id,
        entry_type=ENTRY_COM,
        note=note_text,
    )
    add_ledger_entry(
        db=db,
        client_code=HOUSE_CODE,
        currency=ARS,
        amount=amount,
        ref_id=ref_id,
        entry_type=ENTRY_COM,
        note=note_text,
    )
    return amount


def fmt_amount(amount: float, currency: Optional[str] = None) -> str:
    if currency:
        return f"{currency} {amount:.2f}"
    return f"{amount:.2f}"


def parse_yyyy_mm_dd(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


async def reply(update: Update, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


async def ack_query(update: Update) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            logger.debug("No se pudo responder callback query.", exc_info=True)


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Nueva operacion", callback_data="menu:new_op")],
            [InlineKeyboardButton("💵 Registrar cobro", callback_data="menu:new_cobro")],
            [InlineKeyboardButton("💸 Registrar pago", callback_data="menu:new_pago")],
            [InlineKeyboardButton("🔁 Liquidar ARS→USD", callback_data="menu:new_liq")],
            [InlineKeyboardButton("📌 Ver saldo", callback_data="menu:saldo")],
            [InlineKeyboardButton("💰 Ver cajas", callback_data="menu:cajas")],
        ]
    )


def pacto_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("PAGA_ARS", callback_data="op:pacto:ARS"),
                InlineKeyboardButton("PAGA_USD", callback_data="op:pacto:USD"),
            ]
        ]
    )


def cash_currency_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("ARS", callback_data="cash:currency:ARS"), InlineKeyboardButton("USD", callback_data="cash:currency:USD")]]
    )


def cashbox_keyboard(currency: str) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(box, callback_data=f"cash:cashbox:{box}")
        for box in CASHBOXES_BY_CURRENCY[currency]
    ]
    return InlineKeyboardMarkup([[btn] for btn in buttons])


def confirm_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirmar", callback_data=f"{prefix}:confirm"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"{prefix}:cancel"),
            ]
        ]
    )


def pretty_balance_line(amount: float, currency: str) -> str:
    direction = "a cobrar" if amount > 0 else "a pagar" if amount < 0 else "en cero"
    return f"- {currency}: {amount:.2f} ({direction})"


def clear_wizard_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(OP_DATA_KEY, None)
    context.user_data.pop(CASH_DATA_KEY, None)
    context.user_data.pop(LIQ_DATA_KEY, None)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Bot de operaciones financieras listo.\n\n"
        "Comandos principales:\n"
        "/menu - abre menu rapido\n"
        "/setcliente C001 - vincula tu usuario a un cliente\n"
        "/saldo [C001] - saldo por cliente\n"
        "/cajas - saldo de cajas\n"
        "/setcomision C001 1500 - comision fija por operacion\n"
        "/comisiones [C001] [YYYY-MM-DD YYYY-MM-DD]\n"
        "/addcliente C001 \"Cliente Ejemplo\"\n"
        "/clientes\n"
        "/void OP-YYYYMMDD-C001-0001\n\n"
        "En cualquier wizard puedes usar /cancel."
    )
    await reply(update, text, reply_markup=menu_keyboard())


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "Selecciona una opcion:", reply_markup=menu_keyboard())


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_wizard_data(context)
    await reply(update, "Operacion cancelada.")
    return ConversationHandler.END


async def menu_shortcuts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ack_query(update)
    data = update.callback_query.data if update.callback_query else ""
    if data == "menu:saldo":
        await send_saldo(update, context, requested_code=None)
        return
    if data == "menu:cajas":
        await send_cajas(update)
        return


async def setcliente_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    if len(context.args) != 1:
        await reply(update, "Uso: /setcliente C001")
        return
    client_code = normalize_client_code(context.args[0])
    with session_scope() as db:
        client = db.get(Client, client_code)
        if not client:
            await reply(update, f"Cliente {client_code} no existe. Usa /clientes.")
            return
        link = db.get(TelegramLink, update.effective_user.id)
        if not link:
            link = TelegramLink(
                telegram_user_id=update.effective_user.id,
                client_code=client_code,
            )
            db.add(link)
        else:
            link.client_code = client_code
    await reply(update, f"Listo. Tu usuario quedo vinculado a {client_code} ({client.name}).")


async def send_saldo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    requested_code: Optional[str],
) -> None:
    if not update.effective_user:
        return
    with session_scope() as db:
        if requested_code:
            client_code = normalize_client_code(requested_code)
        else:
            linked = get_linked_client_code(db, update.effective_user.id)
            if not linked:
                await reply(update, "No tienes cliente vinculado. Usa /setcliente C001 o /saldo C001.")
                return
            client_code = linked
        client = db.get(Client, client_code)
        if not client:
            await reply(update, f"Cliente {client_code} no existe.")
            return
        balances = get_client_balances(db, client_code)
    text = (
        f"Saldo de {client.code} - {client.name}\n"
        f"{pretty_balance_line(balances[ARS], ARS)}\n"
        f"{pretty_balance_line(balances[USD], USD)}"
    )
    await reply(update, text)


async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) > 1:
        await reply(update, "Uso: /saldo [C001]")
        return
    requested_code = context.args[0] if context.args else None
    await send_saldo(update, context, requested_code=requested_code)


async def send_cajas(update: Update) -> None:
    with session_scope() as db:
        balances = get_cashbox_balances(db)
    text = (
        "Saldos de cajas:\n"
        f"- R1_ARS: {balances['R1_ARS']:.2f}\n"
        f"- R1_USD: {balances['R1_USD']:.2f}\n"
        f"- R2_ARS: {balances['R2_ARS']:.2f}\n"
        f"- R2_USD: {balances['R2_USD']:.2f}"
    )
    await reply(update, text)


async def cajas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_cajas(update)


async def addcliente_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.text:
        return
    try:
        tokens = shlex.split(update.effective_message.text)
    except ValueError:
        await reply(update, "No pude leer el comando. Usa comillas bien cerradas.")
        return
    if len(tokens) < 3:
        await reply(update, 'Uso: /addcliente C001 "Nombre del cliente"')
        return

    client_code = normalize_client_code(tokens[1])
    if not validate_client_code_format(client_code):
        await reply(update, "Codigo invalido. Usa letras, numeros o guion bajo.")
        return
    name = " ".join(tokens[2:]).strip()
    if not name:
        await reply(update, "Debes indicar un nombre.")
        return

    with session_scope() as db:
        client = db.get(Client, client_code)
        if client:
            client.name = name
            action = "actualizado"
        else:
            db.add(
                Client(
                    code=client_code,
                    name=name,
                    commission_enabled=False,
                    commission_ars=0.0,
                )
            )
            action = "creado"
    await reply(update, f"Cliente {client_code} {action}: {name}")


async def clientes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with session_scope() as db:
        clients = db.execute(select(Client).order_by(Client.code)).scalars().all()
    if not clients:
        await reply(update, "No hay clientes cargados.")
        return

    lines = []
    for client in clients:
        if client.commission_enabled and client.commission_ars > 0:
            lines.append(f"- {client.code}: {client.name} | comision {client.commission_ars:.2f} ARS")
        else:
            lines.append(f"- {client.code}: {client.name}")
    await reply(update, "Clientes:\n" + "\n".join(lines))


async def setcomision_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await reply(update, "Uso: /setcomision C001 1500")
        return

    client_code = normalize_client_code(context.args[0])
    try:
        amount = float(context.args[1].strip().replace(",", "."))
    except ValueError:
        await reply(update, "Monto de comision invalido.")
        return

    if amount < 0:
        await reply(update, "La comision no puede ser negativa.")
        return
    amount = round(amount, 2)

    with session_scope() as db:
        client = db.get(Client, client_code)
        if not client:
            await reply(update, f"Cliente {client_code} no existe.")
            return
        if amount <= 0:
            client.commission_enabled = False
            client.commission_ars = 0.0
            await reply(update, f"Comision desactivada para {client_code}.")
            return
        client.commission_enabled = True
        client.commission_ars = amount
    await reply(update, f"Comision para {client_code}: ARS {amount:.2f} por operacion.")


def parse_comisiones_args(args: list[str]) -> tuple[Optional[str], Optional[date], Optional[date]]:
    if not args:
        return None, None, None
    if len(args) == 1:
        return normalize_client_code(args[0]), None, None
    if len(args) == 2:
        start = parse_yyyy_mm_dd(args[0])
        end = parse_yyyy_mm_dd(args[1])
        return None, start, end
    if len(args) == 3:
        client_code = normalize_client_code(args[0])
        start = parse_yyyy_mm_dd(args[1])
        end = parse_yyyy_mm_dd(args[2])
        return client_code, start, end
    raise ValueError("Uso: /comisiones [C001] [YYYY-MM-DD YYYY-MM-DD]")


async def comisiones_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        client_code, start_date, end_date = parse_comisiones_args(context.args)
    except ValueError as exc:
        await reply(update, str(exc))
        return
    except Exception:
        await reply(update, "Formato de fechas invalido. Usa YYYY-MM-DD.")
        return

    if start_date and end_date and start_date > end_date:
        await reply(update, "Rango de fechas invalido: fecha inicial mayor a la final.")
        return

    with session_scope() as db:
        ensure_house_client(db)
        base_query = select(func.coalesce(func.sum(Ledger.amount), 0.0)).where(
            Ledger.entry_type == ENTRY_COM,
            Ledger.currency == ARS,
        )
        if start_date and end_date:
            start_dt = datetime.combine(start_date, time.min)
            end_dt = datetime.combine(end_date + timedelta(days=1), time.min)
            base_query = base_query.where(Ledger.ts >= start_dt, Ledger.ts < end_dt)

        if client_code:
            client = db.get(Client, client_code)
            if not client:
                await reply(update, f"Cliente {client_code} no existe.")
                return
            total_client = db.scalar(base_query.where(Ledger.client_code == client_code)) or 0.0
            total = -float(total_client)  # al cliente se le descuenta en negativo
            label = f"Comisiones cobradas a {client_code}"
        else:
            total_house = db.scalar(base_query.where(Ledger.client_code == HOUSE_CODE)) or 0.0
            total = float(total_house)
            label = "Comisiones acumuladas en HOUSE"

    if start_date and end_date:
        label += f" ({start_date.isoformat()} a {end_date.isoformat()})"
    await reply(update, f"{label}: ARS {total:.2f}")


async def void_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await reply(update, "Uso: /void OP-YYYYMMDD-C001-0001")
        return
    display_id = context.args[0].strip().upper()
    if not display_id.startswith("OP-"):
        await reply(update, "ID invalido. Debe empezar con OP-.")
        return

    with session_scope() as db:
        operation = db.execute(
            select(Operation).where(Operation.display_id == display_id)
        ).scalar_one_or_none()
        if not operation:
            await reply(update, f"No existe la operacion {display_id}.")
            return
        if operation.status != STATUS_OPEN:
            await reply(update, f"La operacion {display_id} ya no esta OPEN.")
            return

        linked_cash_entries = db.scalar(
            select(func.count(Ledger.id)).where(
                Ledger.ref_id == display_id,
                Ledger.entry_type.in_([ENTRY_COBRO, ENTRY_PAGO]),
            )
        ) or 0
        if linked_cash_entries > 0:
            await reply(
                update,
                "No se puede anular: existen cobros/pagos asociados a esa operacion.",
            )
            return

        original_entries = db.execute(
            select(Ledger).where(
                Ledger.ref_id == display_id,
                Ledger.entry_type.in_([ENTRY_OP, ENTRY_COM]),
            )
        ).scalars().all()
        if not original_entries:
            await reply(update, "No hay asientos OP/COM para revertir.")
            return

        for entry in original_entries:
            add_ledger_entry(
                db=db,
                client_code=entry.client_code,
                currency=entry.currency,
                amount=-entry.amount,
                ref_id=display_id,
                entry_type=ENTRY_VOID,
                note=f"Reversa de {entry.entry_type} por /void",
            )

        operation.status = STATUS_VOID

    await reply(update, f"Operacion {display_id} anulada. Se generaron asientos VOID.")


async def op_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    context.user_data[OP_DATA_KEY] = {}
    data = context.user_data[OP_DATA_KEY]

    user = update.effective_user
    if user:
        with session_scope() as db:
            linked = get_linked_client_code(db, user.id)
            if linked and db.get(Client, linked):
                data["cliente"] = linked
                await reply(
                    update,
                    f"Nueva operacion iniciada.\nCliente preseleccionado: {linked}\nIngresa codigo de contraparte:",
                )
                return OP_CONTRAPARTE

    await reply(update, "Nueva operacion iniciada.\nIngresa codigo de cliente (ej: C001):")
    return OP_CLIENTE


async def op_cliente_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    client_code = normalize_client_code(text)
    with session_scope() as db:
        client = db.get(Client, client_code)
        if not client:
            await reply(update, f"Cliente {client_code} no existe. Intenta de nuevo:")
            return OP_CLIENTE

    context.user_data[OP_DATA_KEY]["cliente"] = client_code
    await reply(update, "Ingresa codigo de contraparte:")
    return OP_CONTRAPARTE


async def op_contraparte_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    contraparte = normalize_client_code(text)
    data = context.user_data.get(OP_DATA_KEY, {})
    cliente = data.get("cliente")
    if not cliente:
        await reply(update, "No encuentro el cliente inicial. Reinicia con /menu.")
        return ConversationHandler.END
    if contraparte == cliente:
        await reply(update, "Contraparte no puede ser igual al cliente. Ingresa otro codigo:")
        return OP_CONTRAPARTE

    with session_scope() as db:
        exists = db.get(Client, contraparte)
        if not exists:
            await reply(update, f"Contraparte {contraparte} no existe. Intenta de nuevo:")
            return OP_CONTRAPARTE

    data["contraparte"] = contraparte
    await reply(update, "Ingresa monto ARS de la operacion:")
    return OP_MONTO_ARS


async def op_monto_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    try:
        monto_ars = parse_positive_number(text, field_name="monto_ars")
    except ValueError as exc:
        await reply(update, f"{exc} Intenta de nuevo:")
        return OP_MONTO_ARS

    context.user_data[OP_DATA_KEY]["monto_ars"] = monto_ars
    await reply(update, "Selecciona pacto:", reply_markup=pacto_keyboard())
    return OP_PACTO


async def op_pacto_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    query_data = update.callback_query.data if update.callback_query else ""
    suffix = query_data.rsplit(":", 1)[-1]
    if suffix not in {ARS, USD}:
        await reply(update, "Pacto invalido. Reintenta:")
        return OP_PACTO
    pacto = PAGA_USD if suffix == USD else PAGA_ARS
    context.user_data[OP_DATA_KEY]["pacto"] = pacto
    if pacto == PAGA_USD:
        await reply(update, "Ingresa usd_pactados:")
        return OP_USD_PACTADOS
    await reply(update, "Ingresa TC opcional (usa '-' para omitir):")
    return OP_TC


async def op_usd_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    try:
        usd_pactados = parse_positive_number(text, field_name="usd_pactados")
    except ValueError as exc:
        await reply(update, f"{exc} Intenta de nuevo:")
        return OP_USD_PACTADOS
    context.user_data[OP_DATA_KEY]["usd_pactados"] = usd_pactados
    await reply(update, "Ingresa TC opcional (usa '-' para omitir):")
    return OP_TC


async def op_tc_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    try:
        tc = parse_optional_tc(text)
    except ValueError as exc:
        await reply(update, f"{exc} Ingresa TC nuevamente o '-' para omitir:")
        return OP_TC
    context.user_data[OP_DATA_KEY]["tc"] = tc
    await reply(update, "Ingresa nota (o '-' para omitir):")
    return OP_NOTA


async def op_nota_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    note_text = (update.effective_message.text if update.effective_message else "").strip()
    note = normalize_note(note_text)
    data = context.user_data.get(OP_DATA_KEY, {})
    data["nota"] = note
    context.user_data[OP_DATA_KEY] = data

    try:
        model = OperationInput(**data)
    except ValidationError as exc:
        await reply(update, f"Datos invalidos:\n{exc}\nReinicia con /menu.")
        return ConversationHandler.END

    with session_scope() as db:
        client = db.get(Client, model.cliente)
        commission_preview = 0.0
        if client and client.commission_enabled and client.commission_ars > 0:
            commission_preview = round(client.commission_ars, 2)

    summary_lines = [
        "Resumen nueva operacion:",
        f"- Cliente: {model.cliente}",
        f"- Contraparte: {model.contraparte}",
        f"- Monto ARS: {model.monto_ars:.2f}",
        f"- Pacto: {model.pacto}",
    ]
    if model.usd_pactados:
        summary_lines.append(f"- USD pactados: {model.usd_pactados:.2f}")
    if model.tc:
        summary_lines.append(f"- TC: {model.tc}")
    if commission_preview > 0:
        summary_lines.append(f"- Comision aplicada: ARS {commission_preview:.2f}")
    if model.nota:
        summary_lines.append(f"- Nota: {model.nota}")

    await reply(update, "\n".join(summary_lines), reply_markup=confirm_keyboard("op"))
    return OP_CONFIRM


async def op_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    data = context.user_data.get(OP_DATA_KEY)
    if not data:
        await reply(update, "No hay datos para confirmar.")
        return ConversationHandler.END

    try:
        payload = OperationInput(**data)
    except ValidationError as exc:
        await reply(update, f"Error de validacion:\n{exc}")
        return ConversationHandler.END

    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user and user.username else None

    try:
        with session_scope() as db:
            cliente = db.get(Client, payload.cliente)
            contraparte = db.get(Client, payload.contraparte)
            if not cliente or not contraparte:
                await reply(update, "Cliente o contraparte no existen. Reintenta.")
                return ConversationHandler.END

            display_id = compute_display_id(db, date.today(), payload.cliente)
            operation = Operation(
                display_id=display_id,
                op_date=date.today(),
                cliente=payload.cliente,
                contraparte=payload.contraparte,
                monto_ars=payload.monto_ars,
                pacto=payload.pacto,
                usd_pactados=payload.usd_pactados,
                tc=payload.tc,
                status=STATUS_OPEN,
                nota=payload.nota,
                created_by_telegram_user_id=user_id,
                created_by_username=username,
                raw_message=str(data),
            )
            db.add(operation)

            add_ledger_entry(
                db=db,
                client_code=payload.cliente,
                currency=ARS,
                amount=payload.monto_ars,
                ref_id=display_id,
                entry_type=ENTRY_OP,
                note=payload.nota,
            )
            if payload.pacto == PAGA_USD:
                add_ledger_entry(
                    db=db,
                    client_code=payload.contraparte,
                    currency=USD,
                    amount=-float(payload.usd_pactados),
                    ref_id=display_id,
                    entry_type=ENTRY_OP,
                    note=payload.nota,
                )
            else:
                add_ledger_entry(
                    db=db,
                    client_code=payload.contraparte,
                    currency=ARS,
                    amount=-payload.monto_ars,
                    ref_id=display_id,
                    entry_type=ENTRY_OP,
                    note=payload.nota,
                )

            commission_applied = apply_commission_if_any(
                db=db,
                client=cliente,
                ref_id=display_id,
                note=f"Comision por {display_id}",
            )

            cliente_bal = get_client_balances(db, payload.cliente)
            contraparte_bal = get_client_balances(db, payload.contraparte)

        lines = [
            f"Operacion registrada: {display_id}",
            f"Saldo {payload.cliente}: ARS {cliente_bal[ARS]:.2f} | USD {cliente_bal[USD]:.2f}",
            f"Saldo {payload.contraparte}: ARS {contraparte_bal[ARS]:.2f} | USD {contraparte_bal[USD]:.2f}",
        ]
        if commission_applied > 0:
            lines.append(f"Comision aplicada: ARS {commission_applied:.2f}")
        await reply(update, "\n".join(lines))
    except Exception as exc:
        logger.exception("Error registrando operacion", exc_info=exc)
        await reply(update, "No pude registrar la operacion por un error interno.")
    finally:
        context.user_data.pop(OP_DATA_KEY, None)

    return ConversationHandler.END


async def op_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    context.user_data.pop(OP_DATA_KEY, None)
    await reply(update, "Nueva operacion cancelada.")
    return ConversationHandler.END


async def cash_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    move_type: str,
) -> int:
    await ack_query(update)
    context.user_data[CASH_DATA_KEY] = {"move_type": move_type}
    data = context.user_data[CASH_DATA_KEY]

    user = update.effective_user
    if user:
        with session_scope() as db:
            linked = get_linked_client_code(db, user.id)
            if linked and db.get(Client, linked):
                data["client_code"] = linked
                await reply(
                    update,
                    f"Registro de {move_type} iniciado.\nCliente preseleccionado: {linked}\nSelecciona moneda:",
                    reply_markup=cash_currency_keyboard(),
                )
                return CASH_MONEDA

    await reply(update, f"Registro de {move_type} iniciado.\nIngresa codigo de cliente (ej: C001):")
    return CASH_CLIENTE


async def cash_start_cobro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await cash_start(update, context, ENTRY_COBRO)


async def cash_start_pago(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await cash_start(update, context, ENTRY_PAGO)


async def cash_cliente_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    client_code = normalize_client_code(text)
    with session_scope() as db:
        client = db.get(Client, client_code)
        if not client:
            await reply(update, f"Cliente {client_code} no existe. Intenta de nuevo:")
            return CASH_CLIENTE

    context.user_data[CASH_DATA_KEY]["client_code"] = client_code
    await reply(update, "Selecciona moneda:", reply_markup=cash_currency_keyboard())
    return CASH_MONEDA


async def cash_currency_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    query_data = update.callback_query.data if update.callback_query else ""
    currency = query_data.rsplit(":", 1)[-1]
    if currency not in {ARS, USD}:
        await reply(update, "Moneda invalida. Reintenta:")
        return CASH_MONEDA
    context.user_data[CASH_DATA_KEY]["currency"] = currency
    await reply(update, f"Selecciona caja {currency}:", reply_markup=cashbox_keyboard(currency))
    return CASH_CAJA


async def cash_caja_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    query_data = update.callback_query.data if update.callback_query else ""
    cashbox = query_data.rsplit(":", 1)[-1].upper()
    currency = context.user_data.get(CASH_DATA_KEY, {}).get("currency")
    if currency not in {ARS, USD}:
        await reply(update, "No encuentro la moneda del flujo. Reinicia con /menu.")
        return ConversationHandler.END
    if cashbox not in CASHBOXES_BY_CURRENCY[currency]:
        await reply(update, "Caja invalida para esa moneda. Reintenta:")
        return CASH_CAJA
    context.user_data[CASH_DATA_KEY]["cashbox"] = cashbox
    await reply(update, "Ingresa monto:")
    return CASH_MONTO


async def cash_monto_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    try:
        amount = parse_positive_number(text, field_name="monto")
    except ValueError as exc:
        await reply(update, f"{exc} Intenta de nuevo:")
        return CASH_MONTO
    context.user_data[CASH_DATA_KEY]["amount"] = amount
    await reply(update, "Ingresa nota (o '-' para omitir):")
    return CASH_NOTA


async def cash_nota_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    note_text = (update.effective_message.text if update.effective_message else "").strip()
    data = context.user_data.get(CASH_DATA_KEY, {})
    data["note"] = normalize_note(note_text)
    context.user_data[CASH_DATA_KEY] = data

    try:
        model = CashInput(**data)
    except ValidationError as exc:
        await reply(update, f"Datos invalidos:\n{exc}\nReinicia con /menu.")
        return ConversationHandler.END

    sign_ledger = "-" if model.move_type == ENTRY_COBRO else "+"
    sign_cash = "+" if model.move_type == ENTRY_COBRO else "-"
    summary = (
        f"Resumen {model.move_type}:\n"
        f"- Cliente: {model.client_code}\n"
        f"- Moneda: {model.currency}\n"
        f"- Caja: {model.cashbox}\n"
        f"- Monto: {model.amount:.2f}\n"
        f"- Ledger: {sign_ledger}{model.amount:.2f}\n"
        f"- Caja efectivo: {sign_cash}{model.amount:.2f}\n"
    )
    if model.note:
        summary += f"- Nota: {model.note}\n"

    await reply(update, summary.strip(), reply_markup=confirm_keyboard("cash"))
    return CASH_CONFIRM


async def cash_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    data = context.user_data.get(CASH_DATA_KEY)
    if not data:
        await reply(update, "No hay datos para confirmar.")
        return ConversationHandler.END

    try:
        model = CashInput(**data)
    except ValidationError as exc:
        await reply(update, f"Error de validacion:\n{exc}")
        return ConversationHandler.END

    user = update.effective_user
    suffix = user.id if user else 0
    ref_id = f"{model.move_type[:3]}-{datetime.utcnow():%Y%m%d%H%M%S}-{suffix}"

    try:
        with session_scope() as db:
            client = db.get(Client, model.client_code)
            if not client:
                await reply(update, f"Cliente {model.client_code} no existe.")
                return ConversationHandler.END

            ledger_amount = -model.amount if model.move_type == ENTRY_COBRO else model.amount
            cash_amount = model.amount if model.move_type == ENTRY_COBRO else -model.amount

            add_ledger_entry(
                db=db,
                client_code=model.client_code,
                currency=model.currency,
                amount=ledger_amount,
                ref_id=ref_id,
                entry_type=model.move_type,
                note=model.note,
            )
            add_cash_movement(
                db=db,
                cashbox=model.cashbox,
                currency=model.currency,
                amount=cash_amount,
                move_type=model.move_type,
                client_code=model.client_code,
                ref_id=ref_id,
                note=model.note,
            )

            balances = get_client_balances(db, model.client_code)
            cash_balances = get_cashbox_balances(db)

        await reply(
            update,
            (
                f"{model.move_type} registrado ({ref_id}).\n"
                f"Saldo cliente {model.client_code}: ARS {balances[ARS]:.2f} | USD {balances[USD]:.2f}\n"
                f"Saldo caja {model.cashbox}: {cash_balances[model.cashbox]:.2f}"
            ),
        )
    except Exception as exc:
        logger.exception("Error registrando %s", model.move_type, exc_info=exc)
        await reply(update, f"No pude registrar el {model.move_type.lower()} por un error interno.")
    finally:
        context.user_data.pop(CASH_DATA_KEY, None)

    return ConversationHandler.END


async def cash_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    context.user_data.pop(CASH_DATA_KEY, None)
    await reply(update, "Flujo de cobro/pago cancelado.")
    return ConversationHandler.END


def liquidation_prompt(client_code: str, balance_ars: float) -> str:
    return (
        f"Liquidacion ARS→USD para {client_code}\n"
        f"Saldo ARS disponible: {balance_ars:.2f}\n"
        "Ingresa monto ARS a liquidar o escribe 'todo':"
    )


async def liq_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    context.user_data[LIQ_DATA_KEY] = {}
    data = context.user_data[LIQ_DATA_KEY]

    user = update.effective_user
    if user:
        with session_scope() as db:
            linked = get_linked_client_code(db, user.id)
            if linked and db.get(Client, linked):
                balances = get_client_balances(db, linked)
                if balances[ARS] <= 0:
                    await reply(
                        update,
                        f"Cliente {linked} no tiene saldo ARS a favor para liquidar (saldo: {balances[ARS]:.2f}).",
                    )
                    return ConversationHandler.END
                data["client_code"] = linked
                await reply(update, liquidation_prompt(linked, balances[ARS]))
                return LIQ_MONTO_ARS

    await reply(update, "Liquidacion ARS→USD iniciada.\nIngresa codigo de cliente (ej: C001):")
    return LIQ_CLIENTE


async def liq_cliente_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    client_code = normalize_client_code(text)

    with session_scope() as db:
        client = db.get(Client, client_code)
        if not client:
            await reply(update, f"Cliente {client_code} no existe. Intenta de nuevo:")
            return LIQ_CLIENTE
        balances = get_client_balances(db, client_code)
        if balances[ARS] <= 0:
            await reply(
                update,
                f"Cliente {client_code} no tiene saldo ARS a favor para liquidar (saldo: {balances[ARS]:.2f}).",
            )
            return LIQ_CLIENTE

    context.user_data[LIQ_DATA_KEY]["client_code"] = client_code
    await reply(update, liquidation_prompt(client_code, balances[ARS]))
    return LIQ_MONTO_ARS


async def liq_monto_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data.get(LIQ_DATA_KEY, {})
    client_code = data.get("client_code")
    if not client_code:
        await reply(update, "No encuentro cliente del flujo. Reinicia con /menu.")
        return ConversationHandler.END

    text = (update.effective_message.text if update.effective_message else "").strip().lower()
    with session_scope() as db:
        balances = get_client_balances(db, client_code)
        saldo_ars = balances[ARS]

    if saldo_ars <= 0:
        await reply(update, "El saldo ARS ya no es positivo. Flujo cancelado.")
        return ConversationHandler.END

    if text == "todo":
        amount_ars = round(saldo_ars, 2)
    else:
        try:
            amount_ars = parse_positive_number(text, field_name="monto_ars")
        except ValueError as exc:
            await reply(update, f"{exc} Intenta de nuevo o escribe 'todo':")
            return LIQ_MONTO_ARS
        if amount_ars > saldo_ars:
            await reply(
                update,
                f"Monto excede saldo ARS disponible ({saldo_ars:.2f}). Intenta de nuevo:",
            )
            return LIQ_MONTO_ARS

    data["amount_ars"] = amount_ars
    context.user_data[LIQ_DATA_KEY] = data
    await reply(update, "Ingresa TC (obligatorio, mayor a 0):")
    return LIQ_TC


async def liq_tc_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text if update.effective_message else "").strip()
    try:
        tc = parse_positive_number(text, field_name="TC", digits=6)
    except ValueError as exc:
        await reply(update, f"{exc} Intenta de nuevo:")
        return LIQ_TC

    context.user_data[LIQ_DATA_KEY]["tc"] = tc
    await reply(update, "Ingresa nota (o '-' para omitir):")
    return LIQ_NOTA


async def liq_nota_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    note_text = (update.effective_message.text if update.effective_message else "").strip()
    data = context.user_data.get(LIQ_DATA_KEY, {})
    data["note"] = normalize_note(note_text)
    context.user_data[LIQ_DATA_KEY] = data

    try:
        model = LiquidationInput(**data)
    except ValidationError as exc:
        await reply(update, f"Datos invalidos:\n{exc}\nReinicia con /menu.")
        return ConversationHandler.END

    usd_result = round(model.amount_ars / model.tc, 2)
    summary = (
        "Resumen liquidacion global:\n"
        f"- Cliente: {model.client_code}\n"
        f"- Monto ARS: {model.amount_ars:.2f}\n"
        f"- TC: {model.tc}\n"
        f"- Resultado USD: {usd_result:.2f}\n"
        "- Caja: no afecta efectivo\n"
    )
    if model.note:
        summary += f"- Nota: {model.note}\n"

    await reply(update, summary.strip(), reply_markup=confirm_keyboard("liq"))
    return LIQ_CONFIRM


async def liq_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    data = context.user_data.get(LIQ_DATA_KEY)
    if not data:
        await reply(update, "No hay datos para confirmar.")
        return ConversationHandler.END

    try:
        model = LiquidationInput(**data)
    except ValidationError as exc:
        await reply(update, f"Error de validacion:\n{exc}")
        return ConversationHandler.END

    try:
        with session_scope() as db:
            client = db.get(Client, model.client_code)
            if not client:
                await reply(update, f"Cliente {model.client_code} no existe.")
                return ConversationHandler.END

            balances = get_client_balances(db, model.client_code)
            saldo_ars = balances[ARS]
            if model.amount_ars > saldo_ars:
                await reply(
                    update,
                    f"El monto a liquidar excede el saldo ARS actual ({saldo_ars:.2f}).",
                )
                return ConversationHandler.END

            liq_id = compute_liq_id(db, date.today(), model.client_code)
            usd_amount = round(model.amount_ars / model.tc, 2)

            add_ledger_entry(
                db=db,
                client_code=model.client_code,
                currency=ARS,
                amount=-model.amount_ars,
                ref_id=liq_id,
                entry_type=ENTRY_LIQ,
                note=model.note,
            )
            add_ledger_entry(
                db=db,
                client_code=model.client_code,
                currency=USD,
                amount=usd_amount,
                ref_id=liq_id,
                entry_type=ENTRY_LIQ,
                note=model.note,
            )

            post_balances = get_client_balances(db, model.client_code)

        await reply(
            update,
            (
                f"Liquidacion registrada: {liq_id}\n"
                f"ARS -> {model.amount_ars:.2f}\n"
                f"USD + {usd_amount:.2f}\n"
                f"Nuevo saldo {model.client_code}: ARS {post_balances[ARS]:.2f} | USD {post_balances[USD]:.2f}"
            ),
        )
    except Exception as exc:
        logger.exception("Error registrando liquidacion", exc_info=exc)
        await reply(update, "No pude registrar la liquidacion por un error interno.")
    finally:
        context.user_data.pop(LIQ_DATA_KEY, None)

    return ConversationHandler.END


async def liq_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await ack_query(update)
    context.user_data.pop(LIQ_DATA_KEY, None)
    await reply(update, "Liquidacion cancelada.")
    return ConversationHandler.END


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "Comando no reconocido. Usa /menu para ver opciones.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Error no controlado", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Ocurrio un error inesperado. Intenta nuevamente en unos segundos."
        )


def build_operation_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("nuevaoperacion", op_start),
            CommandHandler("op", op_start),
            CallbackQueryHandler(op_start, pattern=r"^menu:new_op$"),
        ],
        states={
            OP_CLIENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_cliente_received)],
            OP_CONTRAPARTE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, op_contraparte_received)
            ],
            OP_MONTO_ARS: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_monto_received)],
            OP_PACTO: [CallbackQueryHandler(op_pacto_received, pattern=r"^op:pacto:(ARS|USD)$")],
            OP_USD_PACTADOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_usd_received)],
            OP_TC: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_tc_received)],
            OP_NOTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, op_nota_received)],
            OP_CONFIRM: [
                CallbackQueryHandler(op_confirm, pattern=r"^op:confirm$"),
                CallbackQueryHandler(op_cancel, pattern=r"^op:cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CallbackQueryHandler(op_cancel, pattern=r"^op:cancel$"),
        ],
        allow_reentry=True,
    )


def build_cash_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("cobro", cash_start_cobro),
            CommandHandler("pago", cash_start_pago),
            CallbackQueryHandler(cash_start_cobro, pattern=r"^menu:new_cobro$"),
            CallbackQueryHandler(cash_start_pago, pattern=r"^menu:new_pago$"),
        ],
        states={
            CASH_CLIENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_cliente_received)],
            CASH_MONEDA: [
                CallbackQueryHandler(cash_currency_received, pattern=r"^cash:currency:(ARS|USD)$")
            ],
            CASH_CAJA: [CallbackQueryHandler(cash_caja_received, pattern=r"^cash:cashbox:[A-Z0-9_]+$")],
            CASH_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_monto_received)],
            CASH_NOTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, cash_nota_received)],
            CASH_CONFIRM: [
                CallbackQueryHandler(cash_confirm, pattern=r"^cash:confirm$"),
                CallbackQueryHandler(cash_cancel, pattern=r"^cash:cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CallbackQueryHandler(cash_cancel, pattern=r"^cash:cancel$"),
        ],
        allow_reentry=True,
    )


def build_liq_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("liquidar", liq_start),
            CallbackQueryHandler(liq_start, pattern=r"^menu:new_liq$"),
        ],
        states={
            LIQ_CLIENTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, liq_cliente_received)],
            LIQ_MONTO_ARS: [MessageHandler(filters.TEXT & ~filters.COMMAND, liq_monto_received)],
            LIQ_TC: [MessageHandler(filters.TEXT & ~filters.COMMAND, liq_tc_received)],
            LIQ_NOTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, liq_nota_received)],
            LIQ_CONFIRM: [
                CallbackQueryHandler(liq_confirm, pattern=r"^liq:confirm$"),
                CallbackQueryHandler(liq_cancel, pattern=r"^liq:cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CallbackQueryHandler(liq_cancel, pattern=r"^liq:cancel$"),
        ],
        allow_reentry=True,
    )


def register_handlers(application: Application) -> None:
    application.add_handler(build_operation_conversation())
    application.add_handler(build_cash_conversation())
    application.add_handler(build_liq_conversation())

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("setcliente", setcliente_command))
    application.add_handler(CommandHandler("saldo", saldo_command))
    application.add_handler(CommandHandler("cajas", cajas_command))
    application.add_handler(CommandHandler("setcomision", setcomision_command))
    application.add_handler(CommandHandler("comisiones", comisiones_command))
    application.add_handler(CommandHandler("addcliente", addcliente_command))
    application.add_handler(CommandHandler("clientes", clientes_command))
    application.add_handler(CommandHandler("void", void_command))

    application.add_handler(
        CallbackQueryHandler(menu_shortcuts_callback, pattern=r"^menu:(saldo|cajas)$")
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en variables de entorno.")

    init_db()

    application = Application.builder().token(token).build()
    register_handlers(application)

    logger.info("Bot iniciado por polling.")
    application.run_polling()


if __name__ == "__main__":
    main()
