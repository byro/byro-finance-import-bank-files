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
from byro_finance_import_bank_files.importers.camt import parser
from byro_finance_import_bank_files.importers.camt.importer import (
    Camt053Importer,
    translate_error,
)

FIXTURES = pathlib.Path(__file__).parents[2] / "fixtures" / "camt"


def load(name):
    return (FIXTURES / name).read_bytes()


def source_for(name):
    """A stand-in for ``RealTransactionSource`` with a ``source_file``."""
    return SimpleNamespace(pk=1, source_file=ContentFile(load(name), name=name))


@pytest.fixture
def importer():
    return Camt053Importer()


def test_identifier_and_label(importer):
    assert importer.identifier == "byro_finance_import_bank_files.camt053"
    assert str(importer.label) == "CAMT.053 bank statement"


def test_parse_yields_imported_bank_transactions(importer):
    transactions = list(importer.parse(source_for("batch_booking.xml")))
    assert len(transactions) == 3
    for tx in transactions:
        assert isinstance(tx, ImportedBankTransaction)
        assert tx.amount == Decimal("25.00")
        assert tx.currency == "EUR"
        assert tx.counterparty_iban.startswith("DE")
    assert [tx.external_id for tx in transactions] == [
        "BATCH-001-1",
        "BATCH-001-2",
        "BATCH-001-3",
    ]


def test_fields_are_passed_through_unchanged(importer):
    expected = parser.parse_camt053(load("direct_debit.xml")).transactions[0]
    (tx,) = importer.parse(source_for("direct_debit.xml"))
    assert dataclasses.asdict(tx) == dataclasses.asdict(expected)
    assert tx.amount == Decimal("-12.99")
    assert tx.mandate_id == "MANDAT-987654"
    assert tx.creditor_id == "DE98ZZZ09999999999"


def test_outgoing_transfer_is_negative(importer):
    (tx,) = importer.parse(source_for("outgoing_transfer.xml"))
    assert tx.amount == Decimal("-80.00")
    assert tx.counterparty_name == "Stadtwerke Musterstadt GmbH"


def test_foreign_currency_is_not_reinterpreted(importer):
    (tx,) = importer.parse(source_for("foreign_currency.xml"))
    assert tx.currency == "USD"
    assert tx.amount == Decimal("100.00")


@pytest.mark.parametrize(
    "name, fragment",
    [
        ("not_camt.xml", "not a CAMT.053 bank statement"),
        ("no_namespace.xml", "not a CAMT.053 bank statement"),
        ("camt052.xml", "camt.052.001.02 message"),
        ("camt054.xml", "camt.054.001.02 message"),
        ("archive.zip", "ZIP archive"),
        ("malformed.xml", "not well-formed XML"),
        ("empty_file.xml", "not well-formed XML"),
        ("xxe.xml", "security reasons"),
        ("dtd.xml", "security reasons"),
        ("invalid_amount.xml", "Entry 1 of the CAMT statement has an invalid amount"),
        (
            "proprietary_status.xml",
            "Entry 1 of the CAMT statement has an unsupported entry status",
        ),
        (
            "missing_dates.xml",
            "Entry 1 of the CAMT statement has neither a booking date",
        ),
        ("multiple_accounts.xml", "more than one bank account"),
    ],
)
def test_invalid_files_raise_invalid_import_file(importer, name, fragment):
    with pytest.raises(InvalidImportFile) as excinfo:
        list(importer.parse(source_for(name)))
    message = str(excinfo.value)
    assert fragment in message
    for sensitive in ("Mustermann", "DE89", "NLL", "passwd", "Traceback"):
        assert sensitive not in message
    assert isinstance(excinfo.value.__cause__, parser.CamtError)


def test_translate_error_for_unknown_entry_reason():
    message = translate_error(parser.InvalidEntry(7, "something new"))
    assert message == "Entry 7 of the CAMT statement could not be interpreted."


def test_translate_error_fallback():
    assert "could not be processed" in translate_error(parser.CamtError("x"))


def test_logs_contain_counts_but_no_bank_data(importer, caplog):
    with caplog.at_level(logging.DEBUG, logger="byro_finance_import_bank_files"):
        list(importer.parse(source_for("batch_booking.xml")))
        with pytest.raises(InvalidImportFile):
            list(importer.parse(source_for("invalid_amount.xml")))
    text = caplog.text
    assert "version=camt.053.001.02" in text
    assert "transactions=3" in text
    assert "CAMT import failed: InvalidEntry" in text
    for sensitive in (
        "Mustermann",
        "Musterfrau",
        "DE89",
        "DE75",
        "DE44",
        "NLL",
        "Jahresbeitrag",
    ):
        assert sensitive not in text
