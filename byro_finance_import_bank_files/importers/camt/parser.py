"""Parser for CAMT.053 (ISO 20022 ``BankToCustomerStatement``) documents.

This module is deliberately independent of Django and byro: it turns a
CAMT.053 file into plain :class:`CamtTransaction` objects and raises
:class:`CamtError` subclasses for files it cannot interpret.
The byro adapter next to this module (:mod:`.importer`) maps the result onto
byro's ``ImportedBankTransaction``.

Design rules (see ``feature/camt053-importer.md``):

* XML is only ever parsed with ``defusedxml`` (no DTDs, no entities, no
  external references, no network access).
* Element lookups use explicit relative paths, never descendant searches, so
  that e.g. the ``Amt`` of a charge is never mistaken for the entry amount.
* Every ``camt.053.001.xx`` version is accepted. Element spellings that
  changed between versions (``BIC``/``BICFI``, ``Nm``/``Pty/Nm``,
  ``Sts``/``Sts/Cd``, ``Amt``/``AmtDtls/TxAmt/Amt``) are all tried.
* Amounts are :class:`~decimal.Decimal`; the sign comes from ``CdtDbtInd``.
* Nothing is guessed: entries that cannot be interpreted raise an error and
  batch entries are only split when the details are complete and consistent.
* Error messages and log lines never contain bank data.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import BinaryIO
from xml.etree.ElementTree import Element  # nosec B405 - type only, parsing is defused

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring

logger = logging.getLogger(__name__)

#: ISO 20022 namespaces look like ``urn:iso:std:iso:20022:tech:xsd:camt.053.001.02``.
#: The Austrian STUZZA profile uses ``ISO:camt.053.001.02`` instead.
_NAMESPACE = re.compile(
    r"^(?:urn:iso:std:iso:20022:tech:xsd:|ISO:)"
    r"camt\.(?P<message>\d{3})\.001\.(?P<version>\d{2})$"
)
_ZIP_MAGIC = b"PK\x03\x04"

CREDIT = "CRDT"
DEBIT = "DBIT"

#: Status of a booked entry. Only these entries become transactions.
STATUS_BOOKED = "BOOK"
#: Known non-final statuses: pending, information only, booked with a future
#: value date. Entries with these statuses are skipped and counted.
SKIPPED_STATUSES = frozenset({"PDNG", "INFO", "FUTR"})

#: Values banks use instead of a real reference. Compared after removing all
#: non-alphanumeric characters and upper-casing, so ``NOT PROVIDED``, ``N/A``
#: and ``-`` are covered as well.
PLACEHOLDER_REFERENCES = frozenset(
    {"", "NOTPROVIDED", "NONREF", "NOTAVAIL", "NA", "NONE", "NULL", "UNKNOWN"}
)

#: Reasons why the details of a batch entry were not split into transactions.
BATCH_MISSING_AMOUNT = "missing_amount"
BATCH_CURRENCY_MISMATCH = "currency_mismatch"
BATCH_SUM_MISMATCH = "sum_mismatch"

#: Shape of a SEPA creditor identifier, e.g. ``DE98ZZZ09999999999``.
_CREDITOR_ID = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{3}[A-Za-z0-9]{1,28}$")
_AMOUNT = re.compile(r"^\d+(?:\.\d+)?$")
_DATE_TIMEZONE = re.compile(r"(?:Z|[+-]\d{2}:\d{2})$")
_WHITESPACE = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_CENT = Decimal("0.01")


# -- errors ------------------------------------------------------------------


class CamtError(Exception):
    """Base class for all parser errors.

    ``str(error)`` is a short technical description that never contains bank
    data. User facing messages are produced by the byro adapter
    (:mod:`.importer`).
    """


class MalformedXml(CamtError):
    """The input is not well-formed XML."""


class UnsafeXml(CamtError):
    """The input uses a DTD, entity declarations or external references."""


class NotCamt053(CamtError):
    """The input is not a CAMT.053 document.

    ``reason`` is ``"zip"`` (a ZIP archive), ``"namespace"`` (no ISO 20022
    camt namespace), ``"root"`` (wrong root element) or ``"structure"``
    (no ``BkToCstmrStmt``/``Stmt``).
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"not a CAMT.053 document ({reason})")


class UnsupportedCamtMessage(CamtError):
    """A different camt message, for example camt.052 or camt.054."""

    def __init__(self, message_type: str):
        self.message_type = message_type
        super().__init__(f"unsupported message type {message_type}")


class MultipleAccounts(CamtError):
    """The statements in the file belong to more than one bank account."""

    def __init__(self, count: int):
        self.count = count
        super().__init__(f"statements for {count} different accounts")


class EntryError(CamtError):
    """An entry could not be interpreted.

    ``position`` is the 1-based number of the entry within the whole file.
    """

    def __init__(self, position: int, reason: str):
        self.position = position
        self.reason = reason
        super().__init__(f"entry {position}: {reason}")


class MissingBookingDate(EntryError):
    def __init__(self, position: int):
        super().__init__(position, "neither booking date nor value date")


class InvalidEntry(EntryError):
    pass


# -- result types ------------------------------------------------------------


@dataclass(frozen=True)
class CamtTransaction:
    """One bank transaction as described by the CAMT document.

    Field names and semantics match byro's ``ImportedBankTransaction`` so the
    importer can pass them through unchanged. ``amount`` is signed: positive
    for credits (``CRDT``, money arriving), negative for debits (``DBIT``).
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
class CamtStatement:
    id: str | None
    account_id: str | None
    currency: str | None
    transactions: list[CamtTransaction] = field(default_factory=list)
    entry_count: int = 0
    skipped_entries: int = 0
    batch_fallbacks: int = 0


@dataclass
class CamtDocument:
    version: str
    statements: list[CamtStatement] = field(default_factory=list)

    @property
    def transactions(self) -> list[CamtTransaction]:
        return [tx for statement in self.statements for tx in statement.transactions]

    @property
    def entry_count(self) -> int:
        return sum(statement.entry_count for statement in self.statements)

    @property
    def skipped_entries(self) -> int:
        return sum(statement.skipped_entries for statement in self.statements)

    @property
    def batch_fallbacks(self) -> int:
        return sum(statement.batch_fallbacks for statement in self.statements)


# -- normalisation helpers ---------------------------------------------------


def normalize_text(value: str | None) -> str | None:
    """Collapse whitespace and strip. Returns ``None`` for empty values."""
    if value is None:
        return None
    value = _WHITESPACE.sub(" ", value).strip()
    return value or None


def normalize_iban(value: str | None) -> str | None:
    """Canonical IBAN: no whitespace, upper case. Nothing else is changed."""
    if value is None:
        return None
    value = _WHITESPACE.sub("", value).upper()
    return value or None


def is_placeholder(value: str | None) -> bool:
    """Whether ``value`` is a placeholder such as ``NOTPROVIDED`` or ``N/A``."""
    if value is None:
        return True
    return _NON_ALNUM.sub("", value).upper() in PLACEHOLDER_REFERENCES


def normalize_reference(value: str | None) -> str | None:
    """Normalise a reference; placeholders become ``None``."""
    value = normalize_text(value)
    return None if is_placeholder(value) else value


def parse_amount_text(text: str | None) -> Decimal:
    """Parse a CAMT amount into a :class:`~decimal.Decimal` with two places.

    Raises :class:`ValueError` for anything that is not a plain, non-negative
    decimal number or that cannot be represented in cents exactly.
    """
    text = (text or "").strip()
    if not _AMOUNT.match(text):
        raise ValueError("not a plain decimal number")
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - excluded by the regex
        raise ValueError("not a decimal number") from None
    quantized = value.quantize(_CENT)
    if quantized != value:
        raise ValueError("more than two decimal places")
    return quantized


def select_external_id(
    entry_reference: str | None, references: dict, split: bool
) -> str | None:
    """Pick the bank assigned reference used for duplicate detection.

    Order: the entry's ``AcctSvcrRef``, the transaction's ``AcctSvcrRef``,
    ``TxId``, ``UETR``. Parts of a split batch entry never use the entry's
    reference, because byro would treat them as duplicates of each other.
    All values are expected to be placeholder free already.
    """
    candidates = [] if split else [entry_reference]
    candidates += [
        references.get("AcctSvcrRef"),
        references.get("TxId"),
        references.get("UETR"),
    ]
    return next((candidate for candidate in candidates if candidate), None)


def _put(data: dict, key: str, value) -> None:
    if value is not None:
        data[key] = value


# -- parser ------------------------------------------------------------------


@dataclass
class _Party:
    name: str | None = None
    iban: str | None = None
    bic: str | None = None
    account_id: str | None = None
    ultimate_name: str | None = None


@dataclass
class _Entry:
    """Data shared by all transactions created from one ``Ntry``."""

    position: int
    element: Element
    statement: CamtStatement
    currency: str
    booking_date: datetime.date
    value_date: datetime.date | None
    booking_date_source: str | None
    account_servicer_reference: str | None
    entry_reference: str | None
    reversal: bool
    additional_entry_info: str | None


class _Camt053Parser:
    def __init__(self, root: Element, namespace: str, version: str):
        self.root = root
        self.ns = namespace
        self.version = version
        self.position = 0

    # -- element helpers --------------------------------------------------

    def _find(self, element: Element | None, path: str) -> Element | None:
        if element is None:
            return None
        for tag in path.split("/"):
            element = element.find(f"{{{self.ns}}}{tag}")
            if element is None:
                return None
        return element

    def _findall(self, element: Element | None, path: str) -> list[Element]:
        head, _, tag = path.rpartition("/")
        parent = self._find(element, head) if head else element
        if parent is None:
            return []
        return parent.findall(f"{{{self.ns}}}{tag}")

    def _text(self, element: Element | None, path: str | None = None) -> str | None:
        target = self._find(element, path) if path else element
        return normalize_text(target.text) if target is not None else None

    def _first_text(self, element: Element | None, *paths: str) -> str | None:
        for path in paths:
            value = self._text(element, path)
            if value:
                return value
        return None

    def _texts(self, element: Element | None, path: str) -> list[str]:
        values = (normalize_text(child.text) for child in self._findall(element, path))
        return [value for value in values if value]

    # -- document ---------------------------------------------------------

    def parse(self) -> CamtDocument:
        report = self._find(self.root, "BkToCstmrStmt")
        if report is None:
            raise NotCamt053("structure")
        statements = self._findall(report, "Stmt")
        if not statements:
            raise NotCamt053("structure")
        document = CamtDocument(version=self.version)
        for statement in statements:
            document.statements.append(self._parse_statement(statement))
        self._check_single_account(document)
        self._drop_ambiguous_external_ids(document)
        return document

    def _check_single_account(self, document: CamtDocument) -> None:
        accounts = {s.account_id for s in document.statements if s.account_id}
        if len(accounts) > 1:
            raise MultipleAccounts(len(accounts))

    def _drop_ambiguous_external_ids(self, document: CamtDocument) -> None:
        """A reference that appears on several transactions of one file is
        evidently not unique and must not be used for duplicate detection."""
        counts = Counter(
            tx.external_id for tx in document.transactions if tx.external_id
        )
        ambiguous = {reference for reference, count in counts.items() if count > 1}
        if not ambiguous:
            return
        logger.warning(
            "CAMT document: %s bank reference(s) occur on several transactions "
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

    # -- statement --------------------------------------------------------

    def _parse_statement(self, element: Element) -> CamtStatement:
        statement = CamtStatement(
            id=self._text(element, "Id"),
            account_id=normalize_iban(self._text(element, "Acct/Id/IBAN"))
            or self._text(element, "Acct/Id/Othr/Id"),
            currency=self._text(element, "Acct/Ccy"),
        )
        for entry in self._findall(element, "Ntry"):
            self.position += 1
            statement.entry_count += 1
            transactions = self._parse_entry(entry, statement)
            if transactions is None:
                statement.skipped_entries += 1
            else:
                statement.transactions.extend(transactions)
        return statement

    # -- entry ------------------------------------------------------------

    def _parse_entry(
        self, element: Element, statement: CamtStatement
    ) -> list[CamtTransaction] | None:
        """Return the transactions of one ``Ntry`` or ``None`` if it is skipped."""
        position = self.position
        status = self._entry_status(element, position)
        if status != STATUS_BOOKED:
            logger.debug("CAMT entry %s skipped: status %s", position, status)
            return None

        read = self._read_amount(element, position)
        if read is None:
            raise InvalidEntry(position, "missing amount")
        amount, currency = read
        if amount == 0:
            logger.info("CAMT entry %s skipped: zero amount", position)
            return None
        direction = self._direction(element, position)
        signed_amount = amount if direction == CREDIT else -amount

        booking_date, value_date, date_source = self._entry_dates(element, position)
        entry = _Entry(
            position=position,
            element=element,
            statement=statement,
            currency=currency,
            booking_date=booking_date,
            value_date=value_date,
            booking_date_source=date_source,
            account_servicer_reference=normalize_reference(
                self._text(element, "AcctSvcrRef")
            ),
            entry_reference=normalize_reference(self._text(element, "NtryRef")),
            reversal=(self._text(element, "RvslInd") or "").lower() in ("true", "1"),
            additional_entry_info=self._text(element, "AddtlNtryInf"),
        )

        details, batches = self._transaction_details(element)
        if len(details) >= 2:
            parts = self._split_amounts(
                details, signed_amount, currency, direction, position
            )
            if isinstance(parts, str):
                statement.batch_fallbacks += 1
                logger.warning(
                    "CAMT entry %s: %s transaction details not split (%s)",
                    position,
                    len(details),
                    parts,
                )
                batch = self._merge_batches(batches)
                batch.setdefault("batch_transaction_count", len(details))
                batch["batch_details_not_split"] = parts
                return [
                    self._build(entry, None, signed_amount, direction, batch, False)
                ]
            return [
                self._build(entry, detail, part_amount, part_direction, batch, True)
                for (detail, batch), (part_amount, part_direction) in zip(
                    details, parts
                )
            ]

        if details:
            detail, batch = details[0]
        else:
            detail, batch = None, self._merge_batches(batches)
        return [self._build(entry, detail, signed_amount, direction, batch, False)]

    def _entry_status(self, element: Element, position: int) -> str:
        status = self._find(element, "Sts")
        if status is None:
            raise InvalidEntry(position, "missing status")
        code = self._text(status, "Cd")
        if code is None:
            if self._find(status, "Prtry") is not None:
                raise InvalidEntry(position, "proprietary status")
            code = self._text(status)
        if code is None:
            raise InvalidEntry(position, "missing status")
        code = code.upper()
        if code == STATUS_BOOKED or code in SKIPPED_STATUSES:
            return code
        raise InvalidEntry(position, "unsupported status")

    def _read_amount(
        self, element: Element | None, position: int, path: str = "Amt"
    ) -> tuple[Decimal, str] | None:
        """Return ``(amount, currency)`` of an ``Amt`` element or ``None`` if
        there is no such element."""
        amount = self._find(element, path)
        if amount is None:
            return None
        currency = normalize_text(amount.get("Ccy"))
        if not currency:
            raise InvalidEntry(position, "amount without currency")
        try:
            value = parse_amount_text(amount.text)
        except ValueError:
            raise InvalidEntry(position, "invalid amount") from None
        return value, currency.upper()

    def _direction(
        self, element: Element | None, position: int, default: str | None = None
    ) -> str:
        value = self._text(element, "CdtDbtInd")
        if value is None:
            if default is None:
                raise InvalidEntry(position, "missing credit/debit indicator")
            return default
        value = value.upper()
        if value not in (CREDIT, DEBIT):
            raise InvalidEntry(position, "invalid credit/debit indicator")
        return value

    def _entry_dates(
        self, element: Element, position: int
    ) -> tuple[datetime.date, datetime.date | None, str | None]:
        booking_date = self._date(element, "BookgDt", position)
        value_date = self._date(element, "ValDt", position)
        if booking_date is not None:
            return booking_date, value_date, None
        if value_date is None:
            raise MissingBookingDate(position)
        return value_date, value_date, "value_date"

    def _date(self, element: Element, path: str, position: int) -> datetime.date | None:
        """Read a ``DateAndDateTimeChoice`` (``Dt`` or ``DtTm``) as a date.

        The date component is taken as written; a ``DtTm`` is not converted to
        another timezone.
        """
        choice = self._find(element, path)
        if choice is None:
            return None
        date_text = self._text(choice, "Dt")
        datetime_text = self._text(choice, "DtTm")
        try:
            if date_text:
                return datetime.date.fromisoformat(_DATE_TIMEZONE.sub("", date_text))
            if datetime_text:
                return datetime.datetime.fromisoformat(datetime_text).date()
        except ValueError:
            raise InvalidEntry(position, f"invalid {path}") from None
        return None

    # -- transaction details ----------------------------------------------

    def _transaction_details(
        self, element: Element
    ) -> tuple[list[tuple[Element, dict]], list[dict]]:
        """Collect all ``TxDtls`` (with the batch info of their ``NtryDtls``)
        and the batch info of every ``NtryDtls``."""
        details = []
        batches = []
        for entry_details in self._findall(element, "NtryDtls"):
            batch = self._batch_info(entry_details)
            batches.append(batch)
            for detail in self._findall(entry_details, "TxDtls"):
                details.append((detail, batch))
        return details, batches

    def _batch_info(self, entry_details: Element) -> dict:
        batch = self._find(entry_details, "Btch")
        info: dict = {}
        if batch is None:
            return info
        _put(info, "batch_message_id", normalize_reference(self._text(batch, "MsgId")))
        _put(
            info,
            "batch_payment_information_id",
            normalize_reference(self._text(batch, "PmtInfId")),
        )
        count = self._text(batch, "NbOfTxs")
        if count and count.isdigit():
            info["batch_transaction_count"] = int(count)
        return info

    def _merge_batches(self, batches: list[dict]) -> dict:
        """Batch info for an entry level transaction. Identifiers are only
        kept when they are unambiguous, i.e. there is exactly one batch."""
        filled = [batch for batch in batches if batch]
        if len(filled) == 1:
            return dict(filled[0])
        counts = [
            b["batch_transaction_count"]
            for b in filled
            if "batch_transaction_count" in b
        ]
        return {"batch_transaction_count": sum(counts)} if counts else {}

    def _split_amounts(
        self,
        details: list[tuple[Element, dict]],
        entry_amount: Decimal,
        currency: str,
        direction: str,
        position: int,
    ) -> list[tuple[Decimal, str]] | str:
        """Signed amounts and directions of the batch details, or the reason
        why the entry must not be split."""
        parts = []
        for detail, _batch in details:
            read = self._read_amount(detail, position) or self._read_amount(
                detail, position, "AmtDtls/TxAmt/Amt"
            )
            if read is None:
                return BATCH_MISSING_AMOUNT
            amount, detail_currency = read
            if detail_currency != currency:
                return BATCH_CURRENCY_MISMATCH
            detail_direction = self._direction(detail, position, direction)
            parts.append(
                (amount if detail_direction == CREDIT else -amount, detail_direction)
            )
        if sum(amount for amount, _direction in parts) != entry_amount:
            return BATCH_SUM_MISMATCH
        return parts

    # -- transaction ------------------------------------------------------

    def _build(
        self,
        entry: _Entry,
        detail: Element | None,
        amount: Decimal,
        direction: str,
        batch: dict,
        split: bool,
    ) -> CamtTransaction:
        party = self._counterparty(detail, direction, entry.reversal)
        references = self._references(detail)
        code, proprietary_code, issuer = self._bank_transaction_code(
            detail, entry.element
        )

        data: dict = {"camt_version": self.version}
        _put(data, "statement_id", entry.statement.id)
        _put(data, "entry_reference", entry.entry_reference)
        _put(data, "account_servicer_reference", entry.account_servicer_reference)
        _put(data, "transaction_id", references.get("TxId"))
        _put(data, "instruction_id", references.get("InstrId"))
        _put(data, "uetr", references.get("UETR"))
        _put(data, "creditor_reference", self._creditor_reference(detail))
        _put(data, "purpose_code", self._text(detail, "Purp/Cd"))
        _put(data, "proprietary_bank_transaction_code", proprietary_code)
        _put(data, "proprietary_bank_transaction_code_issuer", issuer)
        _put(data, "additional_entry_info", entry.additional_entry_info)
        if entry.reversal:
            data["reversal"] = True
        _put(
            data,
            "return_reason",
            self._first_text(detail, "RtrInf/Rsn/Cd", "RtrInf/Rsn/Prtry"),
        )
        _put(
            data,
            "return_reason_info",
            " ".join(self._texts(detail, "RtrInf/AddtlInf")) or None,
        )
        _put(data, "booking_date_source", entry.booking_date_source)
        data.update(batch)
        _put(data, "ultimate_counterparty_name", party.ultimate_name)
        _put(data, "counterparty_account_id", party.account_id)

        return CamtTransaction(
            booking_date=entry.booking_date,
            amount=amount,
            currency=entry.currency,
            value_date=entry.value_date,
            memo=self.extract_remittance_information(detail, entry.element),
            counterparty_name=party.name,
            counterparty_iban=party.iban,
            counterparty_bic=party.bic,
            external_id=select_external_id(
                entry.account_servicer_reference, references, split
            ),
            end_to_end_id=references.get("EndToEndId"),
            mandate_id=references.get("MndtId"),
            creditor_id=self._creditor_id(detail),
            bank_reference=references.get("AcctSvcrRef")
            or entry.account_servicer_reference,
            transaction_code=code,
            data=data,
        )

    def _counterparty(
        self, detail: Element | None, direction: str, reversal: bool
    ) -> _Party:
        """The other party: the debtor for credits, the creditor for debits.

        For reversals (``RvslInd``) the related parties keep the roles of the
        original transaction, so a returned direct debit (a debit entry) has
        the original debtor as its other party.
        """
        party = _Party()
        if detail is None:
            return party
        other_is_debtor = (direction == CREDIT) != reversal
        role, agent = ("Dbtr", "DbtrAgt") if other_is_debtor else ("Cdtr", "CdtrAgt")
        parties = self._find(detail, "RltdPties")
        party.name = self._first_text(parties, f"{role}/Nm", f"{role}/Pty/Nm")
        party.ultimate_name = self._first_text(
            parties, f"Ultmt{role}/Nm", f"Ultmt{role}/Pty/Nm"
        )
        account = self._find(parties, f"{role}Acct/Id")
        party.iban = normalize_iban(self._text(account, "IBAN"))
        if party.iban is None:
            party.account_id = self._text(account, "Othr/Id")
        agents = self._find(detail, "RltdAgts")
        party.bic = self._first_text(
            agents, f"{agent}/FinInstnId/BICFI", f"{agent}/FinInstnId/BIC"
        )
        return party

    def _references(self, detail: Element | None) -> dict:
        refs = self._find(detail, "Refs")
        if refs is None:
            return {}
        result = {}
        for tag in ("AcctSvcrRef", "EndToEndId", "MndtId", "TxId", "InstrId", "UETR"):
            _put(result, tag, normalize_reference(self._text(refs, tag)))
        return result

    def _creditor_id(self, detail: Element | None) -> str | None:
        """The SEPA creditor identifier of the creditor, if one is given.

        It is an ``Othr/Id`` below ``Cdtr[/Pty]/Id/PrvtId`` (or ``OrgId``),
        usually without a scheme name. An ``Othr`` with scheme ``SEPA`` wins,
        otherwise the first value shaped like a creditor identifier is used.
        Other identifiers are not reported as creditor ID.
        """
        parties = self._find(detail, "RltdPties")
        for path in ("Cdtr/Id", "Cdtr/Pty/Id"):
            identification = self._find(parties, path)
            if identification is None:
                continue
            others = self._findall(identification, "PrvtId/Othr") + self._findall(
                identification, "OrgId/Othr"
            )
            values = [
                (
                    normalize_reference(self._text(other, "Id")),
                    self._text(other, "SchmeNm/Prtry"),
                )
                for other in others
            ]
            for value, scheme in values:
                if value and (scheme or "").upper() == "SEPA":
                    return value
            for value, _scheme in values:
                if value and _CREDITOR_ID.match(value):
                    return value
        return None

    def _creditor_reference(self, detail: Element | None) -> str | None:
        for structured in self._findall(detail, "RmtInf/Strd"):
            reference = normalize_reference(self._text(structured, "CdtrRefInf/Ref"))
            if reference:
                return reference
        return None

    def _bank_transaction_code(
        self, detail: Element | None, entry: Element
    ) -> tuple[str | None, str | None, str | None]:
        """``(transaction code, proprietary code, proprietary issuer)``.

        The ISO domain code (``PMNT/RCDT/ESCT``) is preferred; German banks
        often leave ``Ntry/BkTxCd`` empty and only provide a proprietary code
        (``NTRF+166``) on the transaction details, which is then used.
        """
        domain_code = proprietary = issuer = None
        for element in (detail, entry):
            code = self._find(element, "BkTxCd")
            if code is None:
                continue
            if domain_code is None:
                domain = self._find(code, "Domn")
                parts = [
                    self._text(domain, "Cd"),
                    self._text(domain, "Fmly/Cd"),
                    self._text(domain, "Fmly/SubFmlyCd"),
                ]
                domain_code = "/".join(part for part in parts if part) or None
            if proprietary is None:
                proprietary = self._text(code, "Prtry/Cd")
                issuer = self._text(code, "Prtry/Issr") if proprietary else None
        return domain_code or proprietary, proprietary, issuer

    def extract_remittance_information(
        self, detail: Element | None, entry: Element
    ) -> str:
        """The purpose of the payment.

        Priority: all unstructured ``Ustrd`` lines joined with single spaces;
        else the structured creditor reference and additional remittance
        lines; else the return information; else ``AddtlTxInf``; else the
        entry's ``AddtlNtryInf``; else an empty string.
        """
        if detail is not None:
            remittance = self._find(detail, "RmtInf")
            unstructured = self._texts(remittance, "Ustrd")
            if unstructured:
                return " ".join(unstructured)
            structured = []
            for element in self._findall(remittance, "Strd"):
                reference = self._text(element, "CdtrRefInf/Ref")
                if reference:
                    structured.append(reference)
                structured.extend(self._texts(element, "AddtlRmtInf"))
            if structured:
                return " ".join(structured)
            return_info = self._texts(detail, "RtrInf/AddtlInf")
            if return_info:
                return " ".join(return_info)
            additional = self._text(detail, "AddtlTxInf")
            if additional:
                return additional
        return self._text(entry, "AddtlNtryInf") or ""


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local = tag[1:].partition("}")
        return namespace, local
    return "", tag


def parse_camt053(source: bytes | BinaryIO) -> CamtDocument:
    """Parse a CAMT.053 document given as bytes or a binary file object.

    Raises a :class:`CamtError` subclass if the input is not a usable
    CAMT.053 document. The input is always parsed as bytes so that the XML
    declaration decides the encoding.
    """
    data = source.read() if hasattr(source, "read") else bytes(source)
    if data[: len(_ZIP_MAGIC)] == _ZIP_MAGIC:
        raise NotCamt053("zip")
    try:
        root = fromstring(
            data, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
    except DefusedXmlException as e:
        raise UnsafeXml(type(e).__name__) from e
    except ParseError as e:
        raise MalformedXml(str(e)) from e

    namespace, local_name = _split_tag(root.tag)
    if local_name != "Document":
        raise NotCamt053("root")
    match = _NAMESPACE.match(namespace)
    if not match:
        raise NotCamt053("namespace")
    version = f"camt.{match.group('message')}.001.{match.group('version')}"
    if match.group("message") != "053":
        raise UnsupportedCamtMessage(version)
    return _Camt053Parser(root, namespace, version).parse()
