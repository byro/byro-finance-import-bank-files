"""Parser for SWIFT MT940 customer statement messages.

This module is deliberately independent of Django and byro: it turns an MT940
file into plain :class:`Mt940Transaction` objects and raises
:class:`Mt940Error` subclasses for files it cannot interpret. The byro adapter
next to this module (:mod:`.importer`) maps the result onto byro's
``ImportedBankTransaction``.

The heavy lifting is done by the `mt-940`_ library (``mt940``), which knows the
tag grammar and the structured German ``:86:`` sub-fields. This module owns
everything the library leaves open or does differently than byro needs (see
``feature/mt940-importer.md``):

* Bytes are decoded here (UTF-8, then Windows-1252, both strict). The library
  would fall back to cp852 and silently garble umlauts.
* The decoded text is handed to the public ``mt940.parse_statements`` through a
  text stream, never as a string: the library treats a string that happens to
  name an existing file as a path to read.
* The library's tag loggers write complete raw field values (names, IBANs,
  remittance text) at DEBUG and ERROR level. They are silenced when this module
  is imported.
* Amounts are taken as the library's :class:`~decimal.Decimal` without rounding;
  the sign is derived here from the ``:61:`` debit/credit mark (``C``/``RD``
  positive, ``D``/``RC`` negative), independent of library defaults.
* Nothing is guessed or dropped: a zero amount, a missing ``:25:``, a missing
  currency, an unparseable field or a file with several accounts is an error.
  Only the exceptions the library raises for malformed *input* are translated;
  anything else propagates so that bugs stay distinguishable from bad files.
* Error messages and log lines never contain bank data. Statements are only
  ever identified by their 1-based position in the file.

.. _mt-940: https://pypi.org/project/mt-940/
"""

from __future__ import annotations

import codecs
import dataclasses
import datetime
import decimal
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import BinaryIO

import mt940
from mt940 import processors as mt940_processors
from mt940.models import Amount, Transactions
from mt940.tags import Tag

logger = logging.getLogger(__name__)

# ``mt940.tags.<Tag>`` loggers emit the raw value of every field they match
# (DEBUG) or fail to match (ERROR), i.e. complete ``:61:``/``:86:`` lines with
# names, IBANs and remittance text. That must never reach byro's log, so the
# whole tag logger subtree is disabled as soon as this module is imported.
logging.getLogger("mt940.tags").setLevel(logging.CRITICAL + 1)

#: Encodings tried in this order, all strict. ``utf-8-sig`` also accepts a
#: UTF-8 byte order mark; Windows-1252 is the superset of ISO-8859-1 used by
#: German banking software and rejects the five undefined bytes instead of
#: mapping every byte like ISO-8859-1 would.
ENCODINGS = ("utf-8-sig", "cp1252")
_UTF16_32_BOMS = (
    codecs.BOM_UTF32_LE,
    codecs.BOM_UTF32_BE,
    codecs.BOM_UTF16_LE,
    codecs.BOM_UTF16_BE,
)

#: Library behaviour is opted into explicitly (every switch defaults to the
#: 5.0.0 behaviour otherwise; the next major release flips them all).
_OPTIONS = mt940.Options(
    applicant_iban=True,  # ``?31`` is the counterparty account, not part of the name
    merge_keeps_values=True,  # an ``:86:`` without KREF does not erase ``:61:`` data
    reversal_sign=True,  # RC negative (the sign is re-derived below anyway)
    case_insensitive_marks=True,
    timezone_offset=True,
    unbounded_details=True,  # never cut ``:86:`` after 9 x 65 characters
    non_swift_free_text=True,
    floor_limit_blank_mark=True,
    strip_bom=True,
    gvc_leading_text=True,  # keep free text in front of the first ``EREF+`` etc.
)

#: ``:61:`` debit/credit marks and the sign of the resulting amount. ``RC``
#: (reversal of a credit) takes money away like a debit, ``RD`` (reversal of a
#: debit) returns money like a credit.
MARK_SIGNS = {"C": 1, "RD": 1, "D": -1, "RC": -1}
REVERSAL_MARKS = frozenset({"RC", "RD"})

#: Values banks use instead of a real reference. Compared after removing all
#: non-alphanumeric characters and upper-casing, so ``NOT PROVIDED``, ``N/A``
#: and ``-`` are covered as well.
PLACEHOLDER_REFERENCES = frozenset(
    {"", "NOTPROVIDED", "NONREF", "NOTAVAIL", "NA", "NONE", "NULL", "UNKNOWN"}
)

_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
_BIC = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?$")
_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")

#: Keys of the parsed ``:61:`` line that are kept apart from the ``:86:`` data.
_STATEMENT_LINE_KEYS = (
    "status",
    "amount",
    "id",
    "customer_reference",
    "bank_reference",
    "extra_details",
    "date",
    "entry_date",
)
#: Statement level keys the library fills from ``:62F:``, ``:62M:`` and ``:62:``.
_CLOSING_BALANCE_KEYS = (
    "final_closing_balance",
    "intermediate_closing_balance",
    "closing_balance",
)
_OPENING_BALANCE_KEYS = (
    "final_opening_balance",
    "intermediate_opening_balance",
    "opening_balance",
)


# -- errors ------------------------------------------------------------------


class Mt940Error(Exception):
    """Base class for all parser errors.

    ``str(error)`` is a short technical description that never contains bank
    data. User facing messages are produced by the byro adapter
    (:mod:`.importer`).
    """


class UnsupportedEncoding(Mt940Error):
    """The file starts with a UTF-16 or UTF-32 byte order mark."""

    def __init__(self):
        super().__init__("UTF-16/UTF-32 byte order mark")


class UndecodableFile(Mt940Error):
    """The bytes are neither valid UTF-8 nor valid Windows-1252."""

    def __init__(self):
        super().__init__("not decodable as " + " or ".join(ENCODINGS))


class NotMt940(Mt940Error):
    """The text contains no MT940 statement (no ``:20:`` block)."""

    def __init__(self):
        super().__init__("no MT940 statement found")


class MultipleAccounts(Mt940Error):
    """The statements in the file belong to more than one bank account."""

    def __init__(self, count: int):
        self.count = count
        super().__init__(f"statements for {count} different accounts")


class StatementError(Mt940Error):
    """A statement could not be interpreted.

    ``statement`` is the 1-based position of the statement within the file.
    """

    def __init__(self, statement: int, reason: str):
        self.statement = statement
        self.reason = reason
        super().__init__(f"statement {statement}: {reason}")


class UnparseableField(StatementError):
    """A field of the statement does not match the MT940 grammar.

    ``tag`` is the field tag without colons, e.g. ``"61"`` or ``"60F"``.
    """

    def __init__(self, statement: int, tag: str):
        self.tag = tag
        super().__init__(statement, f"field :{tag}: could not be parsed")


class MissingAccountIdentification(StatementError):
    def __init__(self, statement: int):
        super().__init__(statement, "no account identification (:25:)")


class MissingCurrency(StatementError):
    def __init__(self, statement: int):
        super().__init__(statement, "no balance provides a currency")


class InvalidStatement(StatementError):
    pass


class InvalidEntry(StatementError):
    """A statement line (``:61:``) could not be interpreted.

    ``position`` is the 1-based number of the line within its statement.
    """

    def __init__(self, statement: int, position: int, reason: str):
        self.position = position
        super().__init__(statement, f"line {position}: {reason}")
        self.reason = reason


# -- result types ------------------------------------------------------------


@dataclass(frozen=True)
class Mt940Transaction:
    """One bank transaction as described by a ``:61:`` line and its ``:86:``.

    Field names and semantics match byro's ``ImportedBankTransaction`` so the
    importer can pass them through unchanged. ``amount`` is signed: positive
    for money arriving (``C``, ``RD``), negative for money leaving (``D``,
    ``RC``).
    """

    booking_date: datetime.date
    amount: Decimal
    currency: str
    value_date: datetime.date | None = None
    memo: str = ""
    counterparty_name: str | None = None
    counterparty_iban: str | None = None
    counterparty_bic: str | None = None
    external_id: str | None = None
    end_to_end_id: str | None = None
    mandate_id: str | None = None
    creditor_id: str | None = None
    bank_reference: str | None = None
    transaction_code: str | None = None
    data: dict = field(default_factory=dict)


@dataclass
class Mt940Statement:
    reference: str | None
    account_id: str
    currency: str
    statement_number: str | None = None
    sequence_number: str | None = None
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    transactions: list[Mt940Transaction] = field(default_factory=list)
    #: ``:86:`` texts that belong to the statement, not to a transaction.
    information: list[str] = field(default_factory=list)
    #: ``True`` if opening balance + transactions != closing balance.
    balance_mismatch: bool = False

    @property
    def entry_count(self) -> int:
        return len(self.transactions)


@dataclass
class Mt940Document:
    encoding: str
    statements: list[Mt940Statement] = field(default_factory=list)

    @property
    def transactions(self) -> list[Mt940Transaction]:
        return [tx for statement in self.statements for tx in statement.transactions]

    @property
    def entry_count(self) -> int:
        return sum(statement.entry_count for statement in self.statements)

    @property
    def balance_mismatches(self) -> int:
        return sum(1 for statement in self.statements if statement.balance_mismatch)


# -- normalisation helpers ---------------------------------------------------


def normalize_text(value: str | None) -> str | None:
    """Collapse whitespace (including line breaks) and strip. ``None`` if empty."""
    if value is None:
        return None
    value = _WHITESPACE.sub(" ", value).strip()
    return value or None


def normalize_compact(value: str | None) -> str | None:
    """Remove all whitespace and upper-case (IBANs, BICs, account ids)."""
    if value is None:
        return None
    value = _WHITESPACE.sub("", value).upper()
    return value or None


def is_placeholder(value: str | None) -> bool:
    """Whether ``value`` is a placeholder such as ``NONREF`` or ``N/A``."""
    if value is None:
        return True
    return _NON_ALNUM.sub("", value).upper() in PLACEHOLDER_REFERENCES


def normalize_reference(value: str | None) -> str | None:
    """Normalise a reference; placeholders become ``None``."""
    value = normalize_text(value)
    return None if is_placeholder(value) else value


def classify_account(value: str | None) -> tuple[str | None, str | None]:
    """``(iban, other_account_id)`` for a counterparty account field.

    The value is only reported as IBAN when it has the shape of one; anything
    else (a legacy account number, garbage) is kept as plain account id.
    """
    compact = normalize_compact(value)
    if compact is None:
        return None, None
    if _IBAN.match(compact):
        return compact, None
    return None, normalize_text(value)


def classify_bank(value: str | None) -> tuple[str | None, str | None]:
    """``(bic, other_bank_code)`` for a counterparty bank field (BIC or BLZ)."""
    compact = normalize_compact(value)
    if compact is None:
        return None, None
    if _BIC.match(compact):
        return compact, None
    return None, normalize_text(value)


def decode(data: bytes) -> tuple[str, str]:
    """Decode the file bytes strictly. Returns ``(text, encoding name)``.

    A UTF-16/UTF-32 byte order mark is rejected explicitly instead of being
    decoded as Windows-1252 garbage that merely looks like "not MT940".
    """
    if data.startswith(_UTF16_32_BOMS):
        raise UnsupportedEncoding()
    for encoding in ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text, "utf-8" if encoding == "utf-8-sig" else encoding
    raise UndecodableFile()


def _put(data: dict, key: str, value) -> None:
    if value is not None:
        data[key] = value


def _plain_date(value: datetime.date) -> datetime.date:
    """A ``datetime.date`` (the library returns its own subclass)."""
    return datetime.date(value.year, value.month, value.day)


def _balance_amount(data: dict, keys: tuple[str, ...]) -> Decimal | None:
    """Signed amount of the first present balance among ``keys``."""
    for key in keys:
        balance = data.get(key)
        if balance is None:
            continue
        amount = getattr(balance, "amount", None)
        if isinstance(amount, Amount):
            return amount.amount
        return None
    return None


# -- library hooks -----------------------------------------------------------


class _ParseState:
    """Per-call bookkeeping done through the library's processor hooks.

    ``mt940.parse_statements`` splits the file at every ``:20:`` and parses
    the blocks one after another. Every block starts with ``:20:``, so the
    ``:20:`` hook counts statements; while parsing, ``len(statement_lines)``
    is the 1-based position of the statement being parsed.
    """

    def __init__(self):
        #: Number of ``:61:`` lines per statement.
        self.statement_lines: list[int] = []
        #: Raw statement level ``:86:`` texts per statement.
        self.information: list[list[str]] = []

    @property
    def statement(self) -> int:
        return len(self.statement_lines)

    def processors(self) -> dict:
        return {
            "pre_transaction_reference_number": [self.start_statement],
            # The library's default pre-processor silently changes a February
            # date such as the 30th to the last day of the month. An invalid
            # date must be an error instead.
            "pre_statement": [],
            "post_statement": [
                mt940_processors.date_cleanup_post_processor,
                mt940_processors.transactions_to_transaction("transaction_reference"),
                self.record_statement_line,
            ],
            "post_transaction_details": [
                mt940_processors.transaction_details_post_processor,
                self.statement_information,
            ],
        }

    def start_statement(self, transactions, tag, tag_dict, *args):
        self.statement_lines.append(0)
        self.information.append([])
        return tag_dict

    def record_statement_line(self, transactions, tag, tag_dict, result):
        """Count the ``:61:`` and keep its fields apart from the ``:86:``.

        The library merges every ``:86:`` into the transaction dictionary; a
        ``KREF+`` customer reference would be appended to the ``:61:``
        customer reference. The ``:61:`` fields are therefore stored under
        ``statement_line`` and the colliding key is removed from the top level.
        """
        self.statement_lines[-1] += 1
        result["statement_line"] = {
            key: result.get(key) for key in _STATEMENT_LINE_KEYS
        }
        result.pop("customer_reference", None)
        return result

    def statement_information(self, transactions, tag, tag_dict, result):
        """Keep a statement level ``:86:`` away from the transactions.

        An ``:86:`` before the first ``:61:`` or after the closing balance
        describes the statement (MT940 "information to account owner"). The
        library would drop the former and append the latter to the last
        transaction.
        """
        outside = not transactions.transactions or any(
            key in transactions.data for key in _CLOSING_BALANCE_KEYS
        )
        if not outside:
            return result
        text = normalize_text(tag_dict.get("transaction_details"))
        if text:
            self.information[-1].append(text)
        return {}


# -- parser ------------------------------------------------------------------


class _Mt940Parser:
    def __init__(self, text: str, encoding: str):
        self.text = text
        self.encoding = encoding
        self.state = _ParseState()

    def parse(self) -> Mt940Document:
        blocks = self._parse_blocks()
        if not blocks:
            raise NotMt940()
        document = Mt940Document(encoding=self.encoding)
        for index, (block, line_count, information) in enumerate(
            zip(
                blocks, self.state.statement_lines, self.state.information, strict=True
            ),
            start=1,
        ):
            document.statements.append(
                self._map_statement(index, block, line_count, information)
            )
        self._check_single_account(document)
        self._drop_ambiguous_external_ids(document)
        return document

    def _parse_blocks(self) -> list[Transactions]:
        """Run the library. Only its input related exceptions are translated.

        The library exceptions are not chained: their messages quote the raw
        field value, which must not travel along with the error.
        """
        state = self.state
        try:
            return mt940.parse_statements(
                io.StringIO(self.text), processors=state.processors(), options=_OPTIONS
            )
        except RuntimeError as e:
            # ``Tag.parse`` raises ``RuntimeError(message, tag, value)`` when a
            # field does not match its pattern.
            tag = e.args[1] if len(e.args) > 1 else None
            if not isinstance(tag, Tag):
                raise
            raise UnparseableField(state.statement, str(tag.id)) from None
        except ValueError:
            # Date components out of range (``datetime`` constructor).
            raise InvalidStatement(state.statement, "invalid date") from None
        except decimal.InvalidOperation:
            # Amount text such as ``1,2,3`` that is not a decimal number.
            raise InvalidStatement(state.statement, "invalid amount") from None

    # -- statement --------------------------------------------------------

    def _map_statement(
        self, index: int, block: Transactions, line_count: int, information: list[str]
    ) -> Mt940Statement:
        data = block.data
        account_id = normalize_compact(data.get("account_identification"))
        if account_id is None:
            raise MissingAccountIdentification(index)
        currency = normalize_compact(block.currency)
        if currency is None:
            raise MissingCurrency(index)
        if len(block.transactions) != line_count:
            # A ``:61:`` without transaction type code makes the library merge
            # the following ``:61:`` into the same transaction.
            raise InvalidStatement(index, "statement lines could not be separated")

        statement = Mt940Statement(
            reference=normalize_text(data.get("transaction_reference")),
            account_id=account_id,
            currency=currency,
            statement_number=normalize_text(data.get("statement_number")),
            sequence_number=normalize_text(data.get("sequence_number")),
            opening_balance=_balance_amount(data, _OPENING_BALANCE_KEYS),
            closing_balance=_balance_amount(data, _CLOSING_BALANCE_KEYS),
            information=list(information),
        )
        for position, transaction in enumerate(block.transactions, start=1):
            statement.transactions.append(
                self._map_transaction(index, position, transaction.data, statement)
            )
        self._check_balances(index, statement)
        if statement.information:
            logger.debug(
                "MT940 statement %s: %s statement level :86: field(s) not attached "
                "to transactions",
                index,
                len(statement.information),
            )
        return statement

    def _check_balances(self, index: int, statement: Mt940Statement) -> None:
        """Opening balance + transactions should give the closing balance.

        A mismatch is reported, never "fixed" and never a reason to reject the
        file: banks book fees or interest outside the statement lines.
        """
        if statement.opening_balance is None or statement.closing_balance is None:
            return
        total = statement.opening_balance + sum(
            (tx.amount for tx in statement.transactions), Decimal(0)
        )
        if total != statement.closing_balance:
            statement.balance_mismatch = True
            logger.warning(
                "MT940 statement %s: opening balance plus transactions does not "
                "equal the closing balance",
                index,
            )

    def _check_single_account(self, document: Mt940Document) -> None:
        accounts = {statement.account_id for statement in document.statements}
        if len(accounts) > 1:
            raise MultipleAccounts(len(accounts))

    def _drop_ambiguous_external_ids(self, document: Mt940Document) -> None:
        """A bank reference that appears on several transactions of one file is
        evidently not unique and must not be used for duplicate detection."""
        counts = Counter(
            tx.external_id for tx in document.transactions if tx.external_id
        )
        ambiguous = {reference for reference, count in counts.items() if count > 1}
        if not ambiguous:
            return
        logger.warning(
            "MT940 document: %s bank reference(s) occur on several transactions "
            "and are not used as external IDs",
            len(ambiguous),
        )
        for statement in document.statements:
            statement.transactions = [
                (
                    dataclasses.replace(tx, external_id=None)
                    if tx.external_id in ambiguous
                    else tx
                )
                for tx in statement.transactions
            ]

    # -- transaction ------------------------------------------------------

    def _map_transaction(
        self, index: int, position: int, data: dict, statement: Mt940Statement
    ) -> Mt940Transaction:
        line = data["statement_line"]
        # A structured ``:86:`` (``166?00...?20...``) has been split into its
        # sub-fields by the library; ``transaction_code`` (the German GVC) is
        # set (possibly to ``None``) for every structured ``:86:``.
        structured = "transaction_code" in data

        mark = (line.get("status") or "").upper()
        if mark not in MARK_SIGNS:
            raise InvalidEntry(index, position, "invalid credit/debit mark")
        amount = abs(line["amount"].amount)
        if not amount.is_finite():
            raise InvalidEntry(index, position, "invalid amount")
        if amount == 0:
            raise InvalidEntry(index, position, "zero amount")
        if MARK_SIGNS[mark] < 0:
            amount = -amount

        value_date = _plain_date(line["date"])
        entry_date = line.get("entry_date")
        booking_date = _plain_date(entry_date) if entry_date else value_date

        iban, account_id = classify_account(
            data.get("applicant_iban") or data.get("gvc_applicant_iban")
        )
        bic, bank_code = classify_bank(
            data.get("applicant_bin") or data.get("gvc_applicant_bin")
        )
        bank_reference = normalize_reference(line.get("bank_reference"))

        meta: dict = {"structured_details": structured}
        _put(meta, "statement_reference", statement.reference)
        _put(meta, "statement_number", statement.statement_number)
        _put(meta, "statement_sequence_number", statement.sequence_number)
        meta["debit_credit_mark"] = mark
        if mark in REVERSAL_MARKS:
            meta["reversal"] = True
        _put(
            meta,
            "business_transaction_code",
            normalize_text(data.get("transaction_code")),
        )
        _put(meta, "posting_text", normalize_text(data.get("posting_text")))
        _put(meta, "prima_nota", normalize_text(data.get("prima_nota")))
        _put(
            meta,
            "account_owner_reference",
            normalize_reference(line.get("customer_reference")),
        )
        _put(
            meta,
            "customer_reference",
            normalize_reference(data.get("customer_reference")),
        )
        _put(meta, "supplementary_details", normalize_text(line.get("extra_details")))
        _put(meta, "text_key_extension", normalize_text(data.get("return_debit_notes")))
        _put(meta, "purpose_code", normalize_text(data.get("purpose_code")))
        _put(
            meta, "ultimate_debtor_name", normalize_text(data.get("deviate_applicant"))
        )
        _put(
            meta,
            "ultimate_creditor_name",
            normalize_text(data.get("deviate_recipient")),
        )
        _put(meta, "mandate_date", normalize_text(data.get("additional_position_date")))
        _put(meta, "sequence_type", normalize_text(data.get("FRST_ONE_OFF_RECC")))
        _put(meta, "original_creditor_id", normalize_reference(data.get("old_SEPA_CI")))
        _put(
            meta,
            "original_mandate_id",
            normalize_reference(data.get("old_SEPA_additional_position_reference")),
        )
        _put(meta, "debtor_id", normalize_reference(data.get("debitor_identifier")))
        _put(
            meta, "compensation_amount", normalize_text(data.get("compensation_amount"))
        )
        _put(meta, "original_amount", normalize_text(data.get("original_amount")))
        _put(meta, "settlement_date", normalize_text(data.get("settlement_tag")))
        if not entry_date:
            meta["booking_date_source"] = "value_date"
        _put(meta, "counterparty_account_id", account_id)
        _put(meta, "counterparty_bank_code", bank_code)

        return Mt940Transaction(
            booking_date=booking_date,
            amount=amount,
            currency=statement.currency,
            value_date=value_date,
            memo=self.extract_memo(data, structured),
            counterparty_name=normalize_text(data.get("applicant_name")),
            counterparty_iban=iban,
            counterparty_bic=bic,
            external_id=bank_reference,
            end_to_end_id=normalize_reference(data.get("end_to_end_reference")),
            mandate_id=normalize_reference(data.get("additional_position_reference")),
            creditor_id=normalize_reference(data.get("applicant_creditor_id")),
            bank_reference=bank_reference,
            transaction_code=normalize_compact(line.get("id")),
            data=meta,
        )

    @staticmethod
    def extract_memo(data: dict, structured: bool) -> str:
        """The purpose of the payment.

        Structured ``:86:``: the purpose (``SVWZ+`` or the plain ``?20``-``?29``
        text) followed by the ``?60``-``?65`` continuation, joined without a
        separator because the sub-fields are a hard wrapped stream; if both are
        empty, the posting text (``?00``). Unstructured ``:86:``: all lines
        joined with single spaces. Both are appended when a transaction has
        both kinds. Nothing is extracted from free text.
        """
        parts = []
        if structured:
            purpose = "".join(
                value
                for value in (data.get("purpose"), data.get("additional_purpose"))
                if value
            )
            parts.append(
                normalize_text(purpose) or normalize_text(data.get("posting_text"))
            )
        parts.append(normalize_text(data.get("transaction_details")))
        return " ".join(part for part in parts if part)


def parse_mt940(source: bytes | BinaryIO) -> Mt940Document:
    """Parse an MT940 file given as bytes or a binary file object.

    Raises an :class:`Mt940Error` subclass if the input is not a usable MT940
    file. Statements are separated at every ``:20:`` (one statement per
    ``:20:`` block, each with its own account, balances and currency).
    """
    data = source.read() if hasattr(source, "read") else bytes(source)
    text, encoding = decode(data)
    return _Mt940Parser(text, encoding).parse()
