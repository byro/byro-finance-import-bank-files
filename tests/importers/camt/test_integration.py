"""End-to-end tests: CAMT fixture → plugin importer → byro core → bookings.

The plugin must not create any bookkeeping objects itself; everything below
the importer is byro's responsibility and is only checked for its outcome.
"""

from decimal import Decimal

import pytest
from django.urls import reverse

from byro.bookkeeping.bank_import import get_bank_transaction_importers
from byro.bookkeeping.models import Booking, RealTransactionSource, Transaction
from byro.bookkeeping.models.real_transaction import SourceState
from byro.bookkeeping.special_accounts import SpecialAccounts
from byro_finance_import_bank_files.importers.camt.importer import Camt053Importer

IMPORTER = Camt053Importer.identifier

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("configuration")]


def upload(client, camt_file, name):
    return client.post(
        reverse("office:finance.uploads.add"),
        {"importer": IMPORTER, "source_file": camt_file(name)},
        follow=True,
    )


def make_source(camt_file, name):
    return RealTransactionSource.objects.create(
        source_file=camt_file(name), importer=IMPORTER
    )


def test_importer_is_offered_on_the_import_page(logged_in_client):
    assert IMPORTER in get_bank_transaction_importers()
    response = logged_in_client.get(reverse("office:finance.uploads.add"))
    content = response.content.decode()
    assert response.status_code == 200
    assert f'value="{IMPORTER}"' in content
    assert "CAMT.053 bank statement" in content


def test_upload_imports_batch_as_three_bank_bookings(logged_in_client, camt_file):
    response = upload(logged_in_client, camt_file, "batch_booking.xml")
    content = response.content.decode()
    assert response.status_code == 200, content
    assert "Import successful" in content
    assert "3 transactions read" in content
    assert "3 newly imported" in content

    source = RealTransactionSource.objects.get()
    assert source.importer == IMPORTER
    assert source.state == SourceState.PROCESSED
    assert source.imported_count == 3
    assert source.duplicate_count == 0

    bank = SpecialAccounts.bank
    bookings = Booking.objects.filter(source=source).order_by("pk")
    assert bookings.count() == 3
    assert Booking.objects.count() == 3  # nothing but the core's bank bookings
    assert Transaction.objects.count() == 3
    for booking in bookings:
        # Money arriving on the bank account is a debit on the asset account.
        assert booking.debit_account == bank
        assert booking.credit_account is None
        assert booking.amount == Decimal("25.00")
        assert booking.importer == IMPORTER
        assert booking.import_identity
        assert booking.data["counterparty_iban"].startswith("DE")
        assert booking.data["batch_payment_information_id"] == "PMT-INFO-1"
    assert sorted(b.data["external_id"] for b in bookings) == [
        "BATCH-001-1",
        "BATCH-001-2",
        "BATCH-001-3",
    ]
    assert sorted(b.data["end_to_end_id"] for b in bookings) == [
        "NLL123-2026",
        "NLL124-2026",
        "NLL125-2026",
    ]
    assert sorted(b.data["counterparty_name"] for b in bookings) == [
        "Erika Musterfrau",
        "Max Mustermann",
        "Peter Beispiel",
    ]
    assert bookings[0].memo == "Mitglied NLL123 Jahresbeitrag 2026"


def test_reimport_of_the_same_file_only_finds_duplicates(logged_in_client, camt_file):
    upload(logged_in_client, camt_file, "batch_booking.xml")
    response = upload(logged_in_client, camt_file, "batch_booking.xml")
    content = response.content.decode()
    assert "3 transactions read" in content
    assert "0 newly imported" in content
    assert "3 already known" in content
    assert Booking.objects.count() == 3
    second = RealTransactionSource.objects.order_by("-pk").first()
    assert second.state == SourceState.PROCESSED
    assert second.imported_count == 0
    assert second.duplicate_count == 3


def test_outgoing_transfer_credits_the_bank_account(camt_file):
    source = make_source(camt_file, "outgoing_transfer.xml")
    result = source.process()
    assert result.imported_count == 1
    booking = Booking.objects.get(source=source)
    assert booking.credit_account == SpecialAccounts.bank
    assert booking.debit_account is None
    assert booking.amount == Decimal("80.00")
    assert booking.data["counterparty_name"] == "Stadtwerke Musterstadt GmbH"
    assert booking.data["counterparty_iban"] == "DE75512108001245126199"
    assert booking.data["transaction_code"] == "PMNT/ICDT/ESCT"
    assert booking.transaction.value_datetime.date().isoformat() == "2026-01-15"


def test_incoming_transfer_normalizes_iban_and_keeps_references(camt_file):
    source = make_source(camt_file, "camt053_001_02_incoming_transfer.xml")
    source.process()
    booking = Booking.objects.get(source=source)
    assert booking.data["counterparty_iban"] == "DE89370400440532013000"
    assert booking.data["external_id"] == "2026011500001-001"
    assert booking.data["bank_reference"] == "2026011500001-001"
    assert booking.data["end_to_end_id"] == "NLL123-2026"
    assert booking.data["transaction_code"] == "NTRF+166"
    assert booking.data["camt_version"] == "camt.053.001.02"
    assert booking.data["statement_id"] == "2026-01-15-001"
    assert booking.transaction.booking_datetime.date().isoformat() == "2026-01-15"


def test_foreign_currency_fails_without_bookings(logged_in_client, camt_file):
    response = upload(logged_in_client, camt_file, "foreign_currency.xml")
    content = response.content.decode()
    assert "could not be imported" in content
    assert "unsupported currency USD" in content
    assert RealTransactionSource.objects.get().state == SourceState.FAILED
    assert Booking.objects.count() == 0


@pytest.mark.parametrize(
    "name, fragment",
    [
        ("xxe.xml", "security reasons"),
        ("malformed.xml", "not well-formed XML"),
        ("camt052.xml", "Only CAMT.053 bank statements are supported"),
        ("multiple_accounts.xml", "more than one bank account"),
        ("invalid_amount.xml", "Entry 1 of the CAMT statement has an invalid amount"),
    ],
)
def test_invalid_files_fail_cleanly(logged_in_client, name, fragment, camt_file):
    response = upload(logged_in_client, camt_file, name)
    content = response.content.decode()
    assert response.status_code == 200
    assert "could not be imported" in content
    assert fragment in content
    assert "Traceback" not in content
    assert RealTransactionSource.objects.get().state == SourceState.FAILED
    assert Booking.objects.count() == 0
    assert Transaction.objects.count() == 0


def test_entries_without_bookings_do_not_fail(logged_in_client, camt_file):
    response = upload(logged_in_client, camt_file, "entry_status.xml")
    content = response.content.decode()
    assert "1 transactions read" in content
    assert RealTransactionSource.objects.get().state == SourceState.PROCESSED
    assert Booking.objects.count() == 1
