"""Tests for the byro adapter: parser output → ImportedBankTransaction.

These tests need Django (translations) but no database.
"""

import dataclasses
import logging
import pathlib
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core.files.base import ContentFile

from byro.bookkeeping.bank_import import ImportedBankTransaction, InvalidImportFile
from byro_finance_import_bank_files.importers.mt940 import parser
from byro_finance_import_bank_files.importers.mt940.importer import (
    Mt940Importer,
    translate_error,
)

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "mt940"

SENSITIVE = ("Mustermann", "Musterfrau", "DE89", "DE02", "NLL", "GARBAGE", "Traceback")


def load(name):
    return (FIXTURES / name).read_bytes()


def source_for(name):
    """A stand-in for ``RealTransactionSource`` with a ``source_file``."""
    return SimpleNamespace(pk=1, source_file=ContentFile(load(name), name=name))


@pytest.fixture
def importer():
    return Mt940Importer()


def test_identifier_and_label(importer):
    assert importer.identifier == "byro_finance_import_bank_files.mt940"
    assert str(importer.label) == "MT940 bank statement"


def test_parse_yields_imported_bank_transactions(importer):
    transactions = list(importer.parse(source_for("structured_86.mt940")))
    assert len(transactions) == 3
    for tx in transactions:
        assert isinstance(tx, ImportedBankTransaction)
        assert tx.currency == "EUR"
        assert tx.counterparty_iban.startswith("DE")
    assert [tx.amount for tx in transactions] == [
        Decimal("25.30"),
        Decimal("12.99"),
        Decimal("-12.99"),
    ]
    assert [tx.external_id for tx in transactions] == [
        "2026011500001",
        "2026011500002",
        "2026011500003",
    ]


def test_fields_are_passed_through_unchanged(importer):
    expected = parser.parse_mt940(load("reversal_debit.mt940")).transactions[0]
    (tx,) = importer.parse(source_for("reversal_debit.mt940"))
    assert dataclasses.asdict(tx) == dataclasses.asdict(expected)
    assert tx.amount == Decimal("12.99")
    assert tx.mandate_id == "MANDAT-987654"
    assert tx.data["reversal"] is True


def test_outgoing_transfer_is_negative(importer):
    (tx,) = importer.parse(source_for("basic_debit.mt940"))
    assert tx.amount == Decimal("-80.00")
    assert tx.counterparty_name == "Stadtwerke Musterstadt GmbH"


def test_optional_fields_may_be_missing(importer):
    (tx,) = importer.parse(source_for("unstructured_86.mt940"))
    assert tx.counterparty_name is None
    assert tx.counterparty_iban is None
    assert tx.counterparty_bic is None
    assert tx.external_id is None
    assert tx.end_to_end_id is None
    assert tx.mandate_id is None
    assert tx.creditor_id is None
    assert tx.bank_reference is None
    assert tx.memo.startswith("Mitglied NLL123")


def test_foreign_currency_is_not_reinterpreted(importer):
    (tx,) = importer.parse(source_for("non_eur.mt940"))
    assert tx.currency == "USD"
    assert tx.amount == Decimal("100.00")


def test_statement_without_transactions_yields_nothing(importer):
    assert list(importer.parse(source_for("no_transactions.mt940"))) == []


@pytest.mark.parametrize(
    "name, fragment",
    [
        ("malformed.mt940", "not an MT940 bank statement"),
        ("utf16_bom.mt940", "encoded as UTF-16 or UTF-32"),
        ("undecodable.mt940", "could not be decoded as UTF-8 or Windows-1252"),
        (
            "invalid_statement_line.mt940",
            "Field :61: in statement 1 of the MT940 file could not be interpreted",
        ),
        (
            "invalid_date.mt940",
            "Statement 1 of the MT940 file contains an invalid date",
        ),
        (
            "invalid_amount.mt940",
            "Statement 1 of the MT940 file contains an invalid amount",
        ),
        ("missing_type_code.mt940", "without a transaction type code"),
        ("missing_currency.mt940", "Statement 1 of the MT940 file has no balance"),
        (
            "missing_account.mt940",
            "Statement 1 of the MT940 file has no account identification",
        ),
        (
            "zero_amount.mt940",
            "Transaction 1 in statement 1 of the MT940 file has an amount of zero",
        ),
        ("multiple_accounts.mt940", "more than one bank account"),
    ],
)
def test_invalid_files_raise_invalid_import_file(importer, name, fragment):
    with pytest.raises(InvalidImportFile) as excinfo:
        list(importer.parse(source_for(name)))
    message = str(excinfo.value)
    assert fragment in message
    for sensitive in SENSITIVE:
        assert sensitive not in message
    assert isinstance(excinfo.value.__cause__, parser.Mt940Error)


def test_translate_error_fallbacks():
    assert translate_error(parser.InvalidEntry(2, 7, "something new")) == (
        "Transaction 7 in statement 2 of the MT940 file could not be interpreted."
    )
    assert translate_error(parser.InvalidStatement(3, "something new")) == (
        "Statement 3 of the MT940 file could not be interpreted."
    )
    assert translate_error(parser.InvalidEntry(1, 1, "invalid credit/debit mark")) == (
        "Transaction 1 in statement 1 of the MT940 file has no valid debit/credit mark."
    )
    assert "could not be processed" in translate_error(parser.Mt940Error("x"))


def test_logs_contain_counts_but_no_bank_data(importer, caplog):
    with caplog.at_level(logging.DEBUG):
        list(importer.parse(source_for("legacy_encoding.mt940")))
        list(importer.parse(source_for("multiple_statements.mt940")))
        with pytest.raises(InvalidImportFile):
            list(importer.parse(source_for("invalid_statement_line.mt940")))
    text = caplog.text
    assert "MT940 import: encoding=cp1252 statements=1 transactions=1" in text
    assert "encoding=utf-8 statements=2 transactions=2" in text
    assert "MT940 import failed: UnparseableField: statement 1: field :61:" in text
    for sensitive in SENSITIVE + ("Jörg", "Spende", "STARTUMSE", "Jahresbeitrag"):
        assert sensitive not in text
