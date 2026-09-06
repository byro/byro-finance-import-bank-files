"""End-to-end tests: MT940 fixture → plugin importer → byro core → bookings.

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
from byro_finance_import_bank_files.importers.mt940.importer import Mt940Importer

IMPORTER = Mt940Importer.identifier

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("configuration")]


def upload(client, mt940_file, name):
    return client.post(
        reverse("office:finance.uploads.add"),
        {"importer": IMPORTER, "source_file": mt940_file(name)},
        follow=True,
    )


def make_source(mt940_file, name):
    return RealTransactionSource.objects.create(
        source_file=mt940_file(name), importer=IMPORTER
    )


def test_both_importers_are_offered_on_the_import_page(logged_in_client):
    importers = get_bank_transaction_importers()
    assert IMPORTER in importers
    assert Camt053Importer.identifier in importers
    response = logged_in_client.get(reverse("office:finance.uploads.add"))
    content = response.content.decode()
    assert response.status_code == 200
    assert f'value="{IMPORTER}"' in content
    assert f'value="{Camt053Importer.identifier}"' in content
    assert "MT940 bank statement" in content
    assert "CAMT.053 bank statement" in content


def test_upload_imports_three_bank_bookings(logged_in_client, mt940_file):
    response = upload(logged_in_client, mt940_file, "structured_86.mt940")
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
    credit, direct_debit, return_debit = bookings
    for booking in bookings:
        assert booking.importer == IMPORTER
        assert booking.import_identity
        assert booking.data["statement_number"] == "00002"
        assert booking.data["business_transaction_code"] in ("166", "105", "109")
    # Money arriving on the bank account is a debit on the asset account.
    assert credit.debit_account == bank
    assert credit.credit_account is None
    assert credit.amount == Decimal("25.30")
    assert credit.memo == "Mitglied NLL123 Jahresbeitrag 2026"
    assert credit.data["counterparty_name"] == "Max Mustermann"
    assert credit.data["counterparty_iban"] == "DE89370400440532013000"
    assert credit.data["external_id"] == "2026011500001"
    assert credit.data["end_to_end_id"] == "NLL123-2026"
    assert credit.data["transaction_code"] == "NTRF"
    assert credit.data["customer_reference"] == "KUNDENREF-1"
    assert direct_debit.debit_account == bank
    assert direct_debit.amount == Decimal("12.99")
    assert direct_debit.data["mandate_id"] == "MANDAT-987654"
    assert direct_debit.data["creditor_id"] == "DE98ZZZ09999999999"
    # Money leaving the bank account is a credit on the asset account.
    assert return_debit.credit_account == bank
    assert return_debit.debit_account is None
    assert return_debit.amount == Decimal("12.99")
    assert return_debit.data["external_id"] == "2026011500003"
    assert return_debit.transaction.booking_datetime.date().isoformat() == "2026-01-15"


def test_reimport_of_the_same_file_only_finds_duplicates(logged_in_client, mt940_file):
    upload(logged_in_client, mt940_file, "structured_86.mt940")
    response = upload(logged_in_client, mt940_file, "structured_86.mt940")
    content = response.content.decode()
    assert "3 transactions read" in content
    assert "0 newly imported" in content
    assert "3 already known" in content
    assert Booking.objects.count() == 3
    second = RealTransactionSource.objects.order_by("-pk").first()
    assert second.state == SourceState.PROCESSED
    assert second.imported_count == 0
    assert second.duplicate_count == 3


def test_outgoing_transfer_credits_the_bank_account(mt940_file):
    source = make_source(mt940_file, "basic_debit.mt940")
    result = source.process()
    assert result.imported_count == 1
    booking = Booking.objects.get(source=source)
    assert booking.credit_account == SpecialAccounts.bank
    assert booking.debit_account is None
    assert booking.amount == Decimal("80.00")
    assert booking.data["counterparty_name"] == "Stadtwerke Musterstadt GmbH"
    assert booking.data["counterparty_iban"] == "DE75512108001245126199"
    assert booking.data["counterparty_bic"] == "SOLADEST600"
    assert booking.data["transaction_code"] == "NTRF"
    assert booking.data["business_transaction_code"] == "116"
    assert booking.transaction.value_datetime.date().isoformat() == "2026-01-15"


def test_entry_date_becomes_the_booking_date(mt940_file):
    source = make_source(mt940_file, "year_boundary.mt940")
    source.process()
    first, second = Booking.objects.filter(source=source).order_by("pk")
    assert first.transaction.booking_datetime.date().isoformat() == "2025-12-30"
    assert first.transaction.value_datetime.date().isoformat() == "2026-01-02"
    assert second.transaction.booking_datetime.date().isoformat() == "2026-01-02"
    assert second.transaction.value_datetime.date().isoformat() == "2025-12-30"


def test_transactions_without_external_id_are_deduplicated_by_fingerprint(
    mt940_file,
):
    first = make_source(mt940_file, "unstructured_86.mt940")
    assert first.process().imported_count == 1
    second = make_source(mt940_file, "unstructured_86.mt940")
    result = second.process()
    assert result.imported_count == 0
    assert result.duplicate_count == 1
    assert Booking.objects.count() == 1


def test_statement_without_transactions_is_processed(logged_in_client, mt940_file):
    response = upload(logged_in_client, mt940_file, "no_transactions.mt940")
    content = response.content.decode()
    assert "Import successful" in content
    assert "0 transactions read" in content
    assert RealTransactionSource.objects.get().state == SourceState.PROCESSED
    assert Booking.objects.count() == 0


def test_foreign_currency_fails_without_bookings(logged_in_client, mt940_file):
    response = upload(logged_in_client, mt940_file, "non_eur.mt940")
    content = response.content.decode()
    assert "could not be imported" in content
    assert "unsupported currency USD" in content
    assert RealTransactionSource.objects.get().state == SourceState.FAILED
    assert Booking.objects.count() == 0


@pytest.mark.parametrize(
    "name, fragment",
    [
        ("malformed.mt940", "not an MT940 bank statement"),
        ("utf16_bom.mt940", "UTF-16 or UTF-32"),
        ("undecodable.mt940", "could not be decoded"),
        ("invalid_statement_line.mt940", "Field :61: in statement 1"),
        ("zero_amount.mt940", "amount of zero"),
        ("missing_account.mt940", "no account identification"),
        ("multiple_accounts.mt940", "more than one bank account"),
    ],
)
def test_invalid_files_fail_cleanly(logged_in_client, name, fragment, mt940_file):
    response = upload(logged_in_client, mt940_file, name)
    content = response.content.decode()
    assert response.status_code == 200
    assert "could not be imported" in content
    assert fragment in content
    assert "Traceback" not in content
    assert "Mustermann" not in content
    assert RealTransactionSource.objects.get().state == SourceState.FAILED
    assert Booking.objects.count() == 0
    assert Transaction.objects.count() == 0
