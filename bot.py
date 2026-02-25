#!/usr/bin/env python3
from __future__ import annotations

import logging
import os
import re
import shlex
import unicodedata
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    delete,
    Float,
    ForeignKey,
    Integer,
    or_,
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
STRUCTURED_OPERATION_FORMAT = (
    "Formato de carga (4 lineas):\n"
    "CLIENTE_ENVIA\n"
    "CLIENTE_RECIBE\n"
    "TC\n"
    "IMPORTE\n\n"
    "Ejemplo:\n"
    "C001\n"
    "C002\n"
    "1250\n"
    "150000"
)


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
    NEW_CLIENT_NAME,
    NEW_CLIENT_COMMISSION,
    NEW_CLIENT_CONFIRM,
) = range(22)


OP_DATA_KEY = "wizard_op"
CASH_DATA_KEY = "wizard_cash"
LIQ_DATA_KEY = "wizard_liq"
NEW_CLIENT_DATA_KEY = "wizard_new_client"


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


class StructuredOperationInput(BaseModel):
    cliente_envia: str
    cliente_recibe: str
    tc: float = Field(ge=1)
    importe: float = Field(gt=0)

    @field_validator("cliente_envia", "cliente_recibe")
    @classmethod
    def normalize_client(cls, value: str) -> str:
        return normalize_client_code(value)

    @model_validator(mode="after")
    def validate_distinct_clients(self) -> "StructuredOperationInput":
        if self.cliente_envia == self.cliente_recibe:
            raise ValueError("CLIENTE_ENVIA y CLIENTE_RECIBE no pueden ser iguales.")
        if self.tc < 1:
            raise ValueError("TC debe ser 1 o mayor.")
        return self


ENGINE = None
SessionLocal: Optional[sessionmaker] = None
CURRENT_DATABASE_URL: Optional[str] = None


def get_database_url() -> str:
    default_sqlite = f"sqlite:///{(Path(__file__).resolve().parent / 'ops.db').as_posix()}"
    raw_url = os.getenv("DATABASE_URL", default_sqlite)
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://") and "+psycopg" not in raw_url:
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return raw_url


def init_db() -> None:
    global ENGINE, SessionLocal, CURRENT_DATABASE_URL

    database_url = get_database_url()
    CURRENT_DATABASE_URL = database_url
    engine_kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    ENGINE = create_engine(database_url, **engine_kwargs)
    SessionLocal = sessionmaker(bind=ENGINE, autoflush=True, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(ENGINE)

    with session_scope() as db:
        ensure_house_client(db)

    logger.info("DB inicializada en %s (cwd=%s)", database_url, os.getcwd())


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


def normalize_client_name(raw: str) -> str:
    compact = " ".join(raw.strip().split())
    normalized = unicodedata.normalize("NFKD", compact)
    without_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return without_accents.casefold()


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


def parse_commission_percentage(raw: str) -> float:
    cleaned = raw.strip().lower().replace(",", ".").replace("%", "")
    if cleaned in {"", "sin", "none", "no", "0", "0.0", "-"}:
        return 0.0
    try:
        value = float(cleaned)
    except ValueError as exc:
        raise ValueError("Porcentaje de comision invalido.") from exc
    if value < 0:
        raise ValueError("La comision no puede ser negativa.")
    if value > 100:
        raise ValueError("La comision no puede superar 100%.")
    return round(value, 4)


def normalize_label(raw: str) -> str:
    translated = raw.upper().translate(str.maketrans("ÁÉÍÓÚÜ", "AEIOUU"))
    return re.sub(r"[^A-Z0-9]+", " ", translated).strip()


def extract_structured_value(raw_line: str, accepted_labels: set[str]) -> str:
    line = raw_line.strip()
    if ":" not in line:
        return line
    label, value = line.split(":", 1)
    if normalize_label(label) in accepted_labels:
        return value.strip()
    return line


def parse_structured_operation_message(text: str) -> StructuredOperationInput:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 4:
        raise ValueError(
            "Debes enviar exactamente 4 lineas: CLIENTE_ENVIA, CLIENTE_RECIBE, TC, IMPORTE."
        )

    cliente_envia = extract_structured_value(
        lines[0],
        {"CLIENTE ENVIA", "CLIENTE 1", "CLIENTE1"},
    )
    cliente_recibe = extract_structured_value(
        lines[1],
        {"CLIENTE RECIBE", "CLIENTE 2", "CLIENTE2"},
    )
    tc_raw = extract_structured_value(lines[2], {"TC", "TIPO CAMBIO", "TIPO DE CAMBIO"})
    importe_raw = extract_structured_value(lines[3], {"IMPORTE", "MONTO"})

    tc = parse_positive_number(tc_raw, field_name="TC", digits=6)
    importe = parse_positive_number(importe_raw, field_name="IMPORTE", digits=2)
    return StructuredOperationInput(
        cliente_envia=cliente_envia,
        cliente_recibe=cliente_recibe,
        tc=tc,
        importe=importe,
    )


def looks_like_structured_operation_message(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 4:
        return True
    if not lines:
        return False
    head = normalize_label(lines[0])
    return head.startswith("CLIENTE")


def extract_numeric_client_sequence(code: str) -> Optional[int]:
    match = re.match(r"^C(\d+)$", normalize_client_code(code))
    if not match:
        return None
    return int(match.group(1))


def compute_next_client_code(db: Session) -> str:
    codes = db.execute(select(Client.code)).scalars().all()
    max_seq = 0
    for code in codes:
        seq = extract_numeric_client_sequence(code)
        if seq is not None:
            max_seq = max(max_seq, seq)
    next_seq = max_seq + 1
    width = max(3, len(str(next_seq)))
    return f"C{next_seq:0{width}d}"


def find_client_by_normalized_name(
    db: Session, name: str, exclude_code: Optional[str] = None
) -> Optional[Client]:
    target = normalize_client_name(name)
    for client in db.execute(select(Client).order_by(Client.code)).scalars().all():
        if exclude_code and client.code == exclude_code:
            continue
        if normalize_client_name(client.name) == target:
            return client
    return None


def create_client_with_auto_code(
    db: Session,
    name: str,
    commission_pct: float,
) -> Client:
    if not name.strip():
        raise ValueError("El nombre del cliente no puede estar vacio.")
    duplicate = find_client_by_normalized_name(db, name)
    if duplicate:
        raise ValueError(
            f"Ya existe un cliente con ese nombre: {duplicate.code} ({duplicate.name})."
        )

    commission_enabled = commission_pct > 0
    # Reintenta en caso de colision de codigo por concurrencia.
    for _ in range(5):
        code = compute_next_client_code(db)
        if db.get(Client, code):
            continue
        client = Client(
            code=code,
            name=name.strip(),
            commission_enabled=commission_enabled,
            commission_ars=commission_pct,
        )
        db.add(client)
        db.flush()
        return client
    raise RuntimeError("No pude generar un codigo de cliente unico. Reintenta.")


def resolve_client_by_identifier(db: Session, identifier: str) -> tuple[Optional[Client], Optional[str]]:
    normalized = normalize_client_code(identifier)
    by_code = db.get(Client, normalized)
    if by_code:
        return by_code, None

    target_name = normalize_client_name(identifier)
    by_name = [
        client
        for client in db.execute(select(Client).order_by(Client.code)).scalars().all()
        if normalize_client_name(client.name) == target_name
    ]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        codes = ", ".join(client.code for client in by_name)
        return None, (
            f"El identificador '{identifier}' coincide con varios clientes por nombre. "
            f"Usa codigo ({codes})."
        )
    return None, f"No existe cliente con codigo o nombre '{identifier}'."


def _round_optional(value: Optional[float], digits: int) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)


def find_duplicate_operation(
    db: Session,
    *,
    op_date: date,
    cliente: str,
    contraparte: str,
    monto_ars: float,
    pacto: str,
    usd_pactados: Optional[float],
    tc: Optional[float],
) -> Optional[str]:
    candidates = db.execute(
        select(Operation).where(
            Operation.op_date == op_date,
            Operation.cliente == cliente,
            Operation.contraparte == contraparte,
            Operation.pacto == pacto,
            Operation.status == STATUS_OPEN,
        )
    ).scalars().all()
    monto_target = round(float(monto_ars), 2)
    usd_target = _round_optional(usd_pactados, 2)
    tc_target = _round_optional(tc, 6)

    for operation in candidates:
        if round(float(operation.monto_ars), 2) != monto_target:
            continue
        if _round_optional(operation.usd_pactados, 2) != usd_target:
            continue
        if _round_optional(operation.tc, 6) != tc_target:
            continue
        return operation.display_id
    return None


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
    base_amount_ars: float,
    client_entry_sign: int = -1,
    note: Optional[str] = None,
) -> float:
    if not client.commission_enabled:
        return 0.0
    commission_pct = round(float(client.commission_ars or 0.0), 4)
    if commission_pct <= 0:
        return 0.0
    amount = round(base_amount_ars * (commission_pct / 100.0), 2)
    if amount <= 0:
        return 0.0
    ensure_house_client(db)
    note_text = note or f"Comision {commission_pct:.4g}% por operacion."
    add_ledger_entry(
        db=db,
        client_code=client.code,
        currency=ARS,
        amount=client_entry_sign * amount,
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
    context.user_data.pop(NEW_CLIENT_DATA_KEY, None)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Bot de operaciones financieras listo.\n\n"
        "Carga una operacion enviando un mensaje de 4 lineas con este formato:\n"
        f"{STRUCTURED_OPERATION_FORMAT}\n\n"
        "Comandos principales:\n"
        "/menu - ayuda rapida sin botones\n"
        "/formato - muestra plantilla de carga\n"
        "/dbinfo - muestra DB activa y conteos\n"
        "/setcliente C001 - vincula tu usuario a un cliente\n"
        "/saldo [C001] - saldo por cliente\n"
        "/cajas - saldo de cajas\n"
        "/setcomision C001 1.5 - comision porcentual (% sobre importe)\n"
        "/comisiones [C001] [YYYY-MM-DD YYYY-MM-DD]\n"
        "/nuevocliente - alta guiada con ID automatico\n"
        "/importclientes - alta masiva por listado\n"
        "/delcliente C001 - elimina cliente sin movimientos\n"
        "/addcliente C001 \"Cliente Ejemplo\"\n"
        "/clientes\n"
        "/void OP-YYYYMMDD-C001-0001"
    )
    await reply(update, text)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Menu rapido:\n"
        "1) Para registrar operacion: envia 4 lineas (usa /formato)\n"
        "2) Consultas: /saldo [C001], /cajas y /dbinfo\n"
        "3) Clientes: /nuevocliente, /importclientes, /delcliente, /clientes, /setcliente\n"
        "4) Comisiones: /setcomision y /comisiones\n"
        "5) Anular: /void OP-YYYYMMDD-C001-0001\n"
        "6) Flujos legacy por wizard (opcionales): /op, /cobro, /pago, /liquidar"
    )
    await reply(update, text)


async def formato_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, STRUCTURED_OPERATION_FORMAT)


async def dbinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    database_url = CURRENT_DATABASE_URL or get_database_url()
    lines = [f"DB URL: {database_url}"]

    if database_url.startswith("sqlite:///"):
        sqlite_path = Path(database_url.replace("sqlite:///", "", 1))
        lines.append(f"Archivo: {sqlite_path}")
        lines.append(f"Existe: {'si' if sqlite_path.exists() else 'no'}")

    try:
        with session_scope() as db:
            clients_total = db.scalar(select(func.count(Client.code))) or 0
            operations_total = db.scalar(select(func.count(Operation.id))) or 0
            ledger_total = db.scalar(select(func.count(Ledger.id))) or 0
            cash_total = db.scalar(select(func.count(CashMovement.id))) or 0
        lines.append(f"Clientes: {int(clients_total)}")
        lines.append(f"Operaciones: {int(operations_total)}")
        lines.append(f"Asientos ledger: {int(ledger_total)}")
        lines.append(f"Movimientos caja: {int(cash_total)}")
    except Exception:
        lines.append("No pude consultar conteos de DB en este momento.")

    await reply(update, "\n".join(lines))


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


async def structured_operation_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not looks_like_structured_operation_message(text):
        return

    try:
        payload = parse_structured_operation_message(text)
    except (ValueError, ValidationError) as exc:
        await reply(
            update,
            (
                f"No pude interpretar el mensaje.\n{exc}\n\n"
                f"{STRUCTURED_OPERATION_FORMAT}"
            ),
        )
        return

    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username if user and user.username else None
    op_date = message.date.date() if message.date else date.today()

    try:
        with session_scope() as db:
            cliente_envia, error_envia = resolve_client_by_identifier(db, payload.cliente_envia)
            if error_envia:
                await reply(update, f"CLIENTE_ENVIA invalido. {error_envia}")
                return
            cliente_recibe, error_recibe = resolve_client_by_identifier(db, payload.cliente_recibe)
            if error_recibe:
                await reply(update, f"CLIENTE_RECIBE invalido. {error_recibe}")
                return
            if cliente_envia.code == cliente_recibe.code:
                await reply(update, "CLIENTE_ENVIA y CLIENTE_RECIBE no pueden ser el mismo cliente.")
                return

            display_id = compute_display_id(db, op_date, cliente_envia.code)
            tc_is_one = abs(payload.tc - 1.0) < 1e-9
            pacto = PAGA_ARS if tc_is_one else PAGA_USD
            contraparte_currency = ARS if tc_is_one else USD
            contraparte_amount = (
                payload.importe if tc_is_one else round(payload.importe / payload.tc, 2)
            )
            usd_pactados = None if tc_is_one else contraparte_amount

            duplicate_id = find_duplicate_operation(
                db,
                op_date=op_date,
                cliente=cliente_envia.code,
                contraparte=cliente_recibe.code,
                monto_ars=payload.importe,
                pacto=pacto,
                usd_pactados=usd_pactados,
                tc=payload.tc,
            )
            if duplicate_id:
                await reply(
                    update,
                    (
                        "Operacion duplicada detectada para la misma fecha. "
                        f"Ya existe: {duplicate_id}"
                    ),
                )
                return

            db.add(
                Operation(
                    display_id=display_id,
                    op_date=op_date,
                    cliente=cliente_envia.code,
                    contraparte=cliente_recibe.code,
                    monto_ars=payload.importe,
                    pacto=pacto,
                    usd_pactados=usd_pactados,
                    tc=payload.tc,
                    status=STATUS_OPEN,
                    nota="Carga por mensaje estructurado",
                    created_by_telegram_user_id=user_id,
                    created_by_username=username,
                    raw_message=text,
                )
            )

            # Regla pedida: cliente 1 envia ARS => queda a pagar en ARS.
            add_ledger_entry(
                db=db,
                client_code=cliente_envia.code,
                currency=ARS,
                amount=-payload.importe,
                ref_id=display_id,
                entry_type=ENTRY_OP,
                note="Alta automatica por mensaje estructurado",
            )

            # Regla pedida:
            # - TC=1 => cliente 2 recibe ARS => queda a cobrar en ARS.
            # - TC>1 => cliente 2 debe entregar USD => queda a cobrar en USD.
            add_ledger_entry(
                db=db,
                client_code=cliente_recibe.code,
                currency=contraparte_currency,
                amount=contraparte_amount,
                ref_id=display_id,
                entry_type=ENTRY_OP,
                note="Alta automatica por mensaje estructurado",
            )

            commission_applied = apply_commission_if_any(
                db=db,
                client=cliente_envia,
                ref_id=display_id,
                base_amount_ars=payload.importe,
                client_entry_sign=1,
                note=f"Comision por {display_id}",
            )

            balance_envia = get_client_balances(db, cliente_envia.code)
            balance_recibe = get_client_balances(db, cliente_recibe.code)

        response_lines = [
            f"Operacion registrada: {display_id}",
            f"Fecha: {op_date.isoformat()}",
            (
                f"Detalle: {cliente_envia.code} ARS -{payload.importe:.2f} | "
                f"{cliente_recibe.code} {contraparte_currency} +{contraparte_amount:.2f}"
            ),
            (
                f"Saldo {cliente_envia.code}: "
                f"ARS {balance_envia[ARS]:.2f} | USD {balance_envia[USD]:.2f}"
            ),
            (
                f"Saldo {cliente_recibe.code}: "
                f"ARS {balance_recibe[ARS]:.2f} | USD {balance_recibe[USD]:.2f}"
            ),
        ]
        if commission_applied > 0:
            response_lines.append(
                f"Comision aplicada a {cliente_envia.code}: ARS {commission_applied:.2f}"
            )
        await reply(update, "\n".join(response_lines))
    except Exception as exc:
        logger.exception("Error registrando operacion por mensaje", exc_info=exc)
        await reply(update, "No pude registrar la operacion por un error interno.")


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


def parse_import_client_line(raw_line: str) -> tuple[str, float]:
    line = raw_line.strip()
    if not line:
        raise ValueError("Linea vacia.")
    for sep in (";", "|"):
        if sep in line:
            name, commission_raw = line.split(sep, 1)
            name = name.strip()
            commission_pct = parse_commission_percentage(commission_raw)
            if not name:
                raise ValueError("Nombre vacio en una linea del listado.")
            return name, commission_pct
    return line, 0.0


async def nuevocliente_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[NEW_CLIENT_DATA_KEY] = {}
    await reply(update, "Alta de cliente nueva.\nIngresa nombre del cliente:")
    return NEW_CLIENT_NAME


async def nuevocliente_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = (update.effective_message.text if update.effective_message else "").strip()
    if not name:
        await reply(update, "El nombre no puede estar vacio. Ingresa nombre del cliente:")
        return NEW_CLIENT_NAME
    if len(name) > 120:
        await reply(update, "Nombre demasiado largo (maximo 120 caracteres). Intenta de nuevo:")
        return NEW_CLIENT_NAME

    with session_scope() as db:
        duplicate = find_client_by_normalized_name(db, name)
    if duplicate:
        await reply(
            update,
            (
                f"Ya existe un cliente con ese nombre: {duplicate.code} ({duplicate.name}).\n"
                "Ingresa otro nombre:"
            ),
        )
        return NEW_CLIENT_NAME

    context.user_data[NEW_CLIENT_DATA_KEY] = {"name": name}
    await reply(
        update,
        "Ingresa comision % (ej: 1.5) o escribe 'sin' para no cobrar comision:",
    )
    return NEW_CLIENT_COMMISSION


async def nuevocliente_commission_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    commission_raw = (update.effective_message.text if update.effective_message else "").strip()
    try:
        commission_pct = parse_commission_percentage(commission_raw)
    except ValueError as exc:
        await reply(update, f"{exc} Intenta nuevamente:")
        return NEW_CLIENT_COMMISSION

    data = context.user_data.get(NEW_CLIENT_DATA_KEY, {})
    name = data.get("name")
    if not name:
        await reply(update, "No encuentro el nombre del cliente. Reinicia con /nuevocliente.")
        return ConversationHandler.END

    with session_scope() as db:
        preview_code = compute_next_client_code(db)

    data["commission_pct"] = commission_pct
    context.user_data[NEW_CLIENT_DATA_KEY] = data

    summary = (
        "Resumen alta de cliente:\n"
        f"- ID sugerido: {preview_code}\n"
        f"- Nombre: {name}\n"
        f"- Comision: {commission_pct:.4g}%\n\n"
        "Responde CONFIRMAR para crear o CANCELAR para abortar."
    )
    await reply(update, summary)
    return NEW_CLIENT_CONFIRM


async def nuevocliente_confirm_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answer = (update.effective_message.text if update.effective_message else "").strip().lower()
    if answer in {"cancelar", "cancel", "no"}:
        context.user_data.pop(NEW_CLIENT_DATA_KEY, None)
        await reply(update, "Alta de cliente cancelada.")
        return ConversationHandler.END
    if answer not in {"confirmar", "confirm", "si", "sí"}:
        await reply(update, "Respuesta invalida. Escribe CONFIRMAR o CANCELAR.")
        return NEW_CLIENT_CONFIRM

    data = context.user_data.get(NEW_CLIENT_DATA_KEY, {})
    name = data.get("name")
    commission_pct = float(data.get("commission_pct", 0.0))
    if not name:
        await reply(update, "No encuentro datos del cliente. Reinicia con /nuevocliente.")
        return ConversationHandler.END

    try:
        with session_scope() as db:
            client = create_client_with_auto_code(db, name=name, commission_pct=commission_pct)
    except ValueError as exc:
        await reply(update, str(exc))
    except Exception as exc:
        logger.exception("Error creando cliente nuevo", exc_info=exc)
        await reply(update, "No pude crear el cliente por un error interno.")
    else:
        await reply(
            update,
            (
                f"Cliente creado: {client.code}\n"
                f"Nombre: {client.name}\n"
                f"Comision: {commission_pct:.4g}%"
            ),
        )
    finally:
        context.user_data.pop(NEW_CLIENT_DATA_KEY, None)
    return ConversationHandler.END


async def nuevocliente_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(NEW_CLIENT_DATA_KEY, None)
    await reply(update, "Alta de cliente cancelada.")
    return ConversationHandler.END


async def importclientes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    lines = message.text.splitlines()
    raw_lines = [line for line in lines[1:] if line.strip()]
    if not raw_lines:
        await reply(
            update,
            (
                "Uso:\n"
                "/importclientes\n"
                "Nombre Cliente A;1.5\n"
                "Nombre Cliente B;sin\n"
                "Nombre Cliente C"
            ),
        )
        return

    parsed: list[tuple[str, float]] = []
    try:
        for line in raw_lines:
            parsed.append(parse_import_client_line(line))
    except ValueError as exc:
        await reply(update, f"Error en listado: {exc}")
        return

    seen_in_payload: dict[str, str] = {}
    for idx, (name, _pct) in enumerate(parsed, start=1):
        normalized_name = normalize_client_name(name)
        if normalized_name in seen_in_payload:
            first_name = seen_in_payload[normalized_name]
            await reply(
                update,
                (
                    f"Error en listado (linea {idx}): nombre duplicado '{name}'.\n"
                    f"Ya aparece como '{first_name}'."
                ),
            )
            return
        seen_in_payload[normalized_name] = name

    try:
        with session_scope() as db:
            for idx, (name, _commission_pct) in enumerate(parsed, start=1):
                duplicate = find_client_by_normalized_name(db, name)
                if duplicate:
                    await reply(
                        update,
                        (
                            f"Error en listado (linea {idx}): '{name}' ya existe "
                            f"como {duplicate.code} ({duplicate.name})."
                        ),
                    )
                    return

            created: list[Client] = []
            for name, commission_pct in parsed:
                created.append(
                    create_client_with_auto_code(
                        db,
                        name=name,
                        commission_pct=commission_pct,
                    )
                )
        lines_out = [
            f"- {client.code}: {client.name} | comision {client.commission_ars:.4g}%"
            for client in created
        ]
        await reply(update, "Clientes creados:\n" + "\n".join(lines_out))
    except Exception as exc:
        logger.exception("Error importando clientes", exc_info=exc)
        await reply(update, "No pude importar clientes por un error interno.")


async def addcliente_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_message.text:
        return
    try:
        tokens = shlex.split(update.effective_message.text)
    except ValueError:
        await reply(update, "No pude leer el comando. Usa comillas bien cerradas.")
        return
    if len(tokens) < 3:
        await reply(update, 'Uso legacy: /addcliente C001 "Nombre del cliente"')
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
        duplicate = find_client_by_normalized_name(
            db, name, exclude_code=client_code if client else None
        )
        if duplicate:
            await reply(
                update,
                (
                    f"No se puede guardar: el nombre ya existe en "
                    f"{duplicate.code} ({duplicate.name})."
                ),
            )
            return
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
            lines.append(f"- {client.code}: {client.name} | comision {client.commission_ars:.4g}%")
        else:
            lines.append(f"- {client.code}: {client.name}")
    await reply(update, "Clientes:\n" + "\n".join(lines))


async def setcomision_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 2:
        await reply(update, "Uso: /setcomision C001 1.5")
        return

    client_code = normalize_client_code(context.args[0])
    try:
        commission_pct = parse_commission_percentage(context.args[1])
    except ValueError as exc:
        await reply(update, str(exc))
        return

    with session_scope() as db:
        client = db.get(Client, client_code)
        if not client:
            await reply(update, f"Cliente {client_code} no existe.")
            return
        if commission_pct <= 0:
            client.commission_enabled = False
            client.commission_ars = 0.0
            await reply(update, f"Comision desactivada para {client_code}.")
            return
        client.commission_enabled = True
        client.commission_ars = commission_pct
    await reply(update, f"Comision para {client_code}: {commission_pct:.4g}% sobre importe enviado.")


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
        house_query = select(func.coalesce(func.sum(Ledger.amount), 0.0)).where(
            Ledger.entry_type == ENTRY_COM,
            Ledger.currency == ARS,
            Ledger.client_code == HOUSE_CODE,
        )
        client_query = select(func.coalesce(func.sum(func.abs(Ledger.amount)), 0.0)).where(
            Ledger.entry_type == ENTRY_COM,
            Ledger.currency == ARS,
        )
        if start_date and end_date:
            start_dt = datetime.combine(start_date, time.min)
            end_dt = datetime.combine(end_date + timedelta(days=1), time.min)
            house_query = house_query.where(Ledger.ts >= start_dt, Ledger.ts < end_dt)
            client_query = client_query.where(Ledger.ts >= start_dt, Ledger.ts < end_dt)

        if client_code:
            client = db.get(Client, client_code)
            if not client:
                await reply(update, f"Cliente {client_code} no existe.")
                return
            total_client = db.scalar(client_query.where(Ledger.client_code == client_code)) or 0.0
            total = float(total_client)
            label = f"Comisiones cobradas a {client_code}"
        else:
            total_house = db.scalar(house_query) or 0.0
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


async def delcliente_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await reply(update, "Uso: /delcliente C001")
        return

    identifier = " ".join(context.args).strip()
    if not identifier:
        await reply(update, "Uso: /delcliente C001")
        return

    with session_scope() as db:
        client, error = resolve_client_by_identifier(db, identifier)
        if error or not client:
            await reply(update, error or "Cliente no encontrado.")
            return
        if client.code == HOUSE_CODE:
            await reply(update, "No se puede eliminar el cliente interno HOUSE.")
            return

        operations_count = db.scalar(
            select(func.count(Operation.id)).where(
                or_(Operation.cliente == client.code, Operation.contraparte == client.code)
            )
        ) or 0
        ledger_count = db.scalar(
            select(func.count(Ledger.id)).where(Ledger.client_code == client.code)
        ) or 0
        cash_count = db.scalar(
            select(func.count(CashMovement.id)).where(CashMovement.client_code == client.code)
        ) or 0

        if operations_count > 0 or ledger_count > 0 or cash_count > 0:
            await reply(
                update,
                (
                    f"No se puede eliminar {client.code}: tiene historial.\n"
                    f"- Operaciones: {operations_count}\n"
                    f"- Asientos ledger: {ledger_count}\n"
                    f"- Movimientos de caja: {cash_count}"
                ),
            )
            return

        links_deleted = db.scalar(
            select(func.count(TelegramLink.telegram_user_id)).where(
                TelegramLink.client_code == client.code
            )
        ) or 0
        if links_deleted > 0:
            db.execute(delete(TelegramLink).where(TelegramLink.client_code == client.code))

        deleted_code = client.code
        deleted_name = client.name
        db.delete(client)

    await reply(
        update,
        (
            f"Cliente eliminado: {deleted_code} - {deleted_name}\n"
            f"Vinculos Telegram eliminados: {int(links_deleted)}"
        ),
    )


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
        commission_pct = 0.0
        if client and client.commission_enabled and client.commission_ars > 0:
            commission_pct = round(client.commission_ars, 4)
            commission_preview = round(model.monto_ars * (commission_pct / 100.0), 2)

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
        summary_lines.append(
            f"- Comision estimada: ARS {commission_preview:.2f} ({commission_pct:.4g}%)"
        )
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

            duplicate_id = find_duplicate_operation(
                db,
                op_date=date.today(),
                cliente=payload.cliente,
                contraparte=payload.contraparte,
                monto_ars=payload.monto_ars,
                pacto=payload.pacto,
                usd_pactados=payload.usd_pactados,
                tc=payload.tc,
            )
            if duplicate_id:
                await reply(
                    update,
                    (
                        "Operacion duplicada detectada para la fecha de hoy. "
                        f"Ya existe: {duplicate_id}"
                    ),
                )
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
                base_amount_ars=payload.monto_ars,
                client_entry_sign=-1,
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
    await reply(
        update,
        "Comando no reconocido. Usa /menu, /formato, /dbinfo o /nuevocliente para ver opciones.",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Error no controlado", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "Ocurrio un error inesperado. Intenta nuevamente en unos segundos."
        )


def build_new_client_conversation() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("nuevocliente", nuevocliente_start)],
        states={
            NEW_CLIENT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, nuevocliente_name_received)
            ],
            NEW_CLIENT_COMMISSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, nuevocliente_commission_received)
            ],
            NEW_CLIENT_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, nuevocliente_confirm_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", nuevocliente_cancel)],
        allow_reentry=True,
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
        per_message=True,
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
        per_message=True,
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
        per_message=True,
        allow_reentry=True,
    )


def register_handlers(application: Application) -> None:
    application.add_handler(build_new_client_conversation())
    application.add_handler(build_operation_conversation())
    application.add_handler(build_cash_conversation())
    application.add_handler(build_liq_conversation())

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("formato", formato_command))
    application.add_handler(CommandHandler("dbinfo", dbinfo_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("setcliente", setcliente_command))
    application.add_handler(CommandHandler("saldo", saldo_command))
    application.add_handler(CommandHandler("cajas", cajas_command))
    application.add_handler(CommandHandler("setcomision", setcomision_command))
    application.add_handler(CommandHandler("comisiones", comisiones_command))
    application.add_handler(CommandHandler("importclientes", importclientes_command))
    application.add_handler(CommandHandler("delcliente", delcliente_command))
    application.add_handler(CommandHandler("addcliente", addcliente_command))
    application.add_handler(CommandHandler("clientes", clientes_command))
    application.add_handler(CommandHandler("void", void_command))

    application.add_handler(
        CallbackQueryHandler(menu_shortcuts_callback, pattern=r"^menu:(saldo|cajas)$")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, structured_operation_message_handler)
    )
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)


def main() -> None:
    primary_token = os.getenv("TELEGRAM_BOT_TOKEN")
    fallback_token = os.getenv("ELEGRAM_BOT_TOKEN")
    token = primary_token or fallback_token
    if not token:
        raise RuntimeError(
            "Falta TELEGRAM_BOT_TOKEN en variables de entorno "
            "(tambien se acepta ELEGRAM_BOT_TOKEN como fallback)."
        )
    if fallback_token and not primary_token:
        logger.warning(
            "Usando ELEGRAM_BOT_TOKEN como fallback. Recomendado: renombrar a TELEGRAM_BOT_TOKEN."
        )

    init_db()

    application = Application.builder().token(token).build()
    register_handlers(application)

    logger.info("Bot iniciado por polling.")
    application.run_polling()


if __name__ == "__main__":
    main()
