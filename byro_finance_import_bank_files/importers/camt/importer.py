"""byro integration: the CAMT.053 bank transaction importer.

This module is the only place where the plugin touches byro. It opens the
uploaded file, hands it to :mod:`.parser` and passes the resulting
transactions through as ``ImportedBankTransaction`` objects. Everything else
(validation, duplicate detection, persistence, matching) is done by byro.
"""

import dataclasses
import logging

from django.utils.translation import gettext_lazy as _

from byro.bookkeeping.bank_import import (
    BankTransactionImporter,
    ImportedBankTransaction,
    InvalidImportFile,
)

from . import parser

logger = logging.getLogger(__name__)

_ENTRY_MESSAGES = {
    "invalid amount": _(
        "Entry %(position)s of the CAMT statement has an invalid amount."
    ),
    "missing amount": _(
        "Entry %(position)s of the CAMT statement has an invalid amount."
    ),
    "amount without currency": _(
        "Entry %(position)s of the CAMT statement has an invalid amount."
    ),
    "missing status": _(
        "Entry %(position)s of the CAMT statement has an unsupported entry status."
    ),
    "proprietary status": _(
        "Entry %(position)s of the CAMT statement has an unsupported entry status."
    ),
    "unsupported status": _(
        "Entry %(position)s of the CAMT statement has an unsupported entry status."
    ),
    "missing credit/debit indicator": _(
        "Entry %(position)s of the CAMT statement has no valid credit/debit indicator."
    ),
    "invalid credit/debit indicator": _(
        "Entry %(position)s of the CAMT statement has no valid credit/debit indicator."
    ),
    "invalid BookgDt": _(
        "Entry %(position)s of the CAMT statement has an invalid date."
    ),
    "invalid ValDt": _("Entry %(position)s of the CAMT statement has an invalid date."),
}


def translate_error(error: parser.CamtError) -> str:
    """User presentable, translatable message for a parser error.

    The messages describe the problem without repeating any file content.
    """
    if isinstance(error, parser.UnsafeXml):
        return _(
            "The file contains a DTD or external entities and was rejected for "
            "security reasons."
        )
    if isinstance(error, parser.MalformedXml):
        return _("The uploaded file is not well-formed XML.")
    if isinstance(error, parser.UnsupportedCamtMessage):
        return _(
            "The file is a %(type)s message. Only CAMT.053 bank statements are supported."
        ) % {"type": error.message_type}
    if isinstance(error, parser.NotCamt053):
        if error.reason == "zip":
            return _(
                "The uploaded file is a ZIP archive. Please extract it and upload "
                "the CAMT.053 XML file."
            )
        return _("The uploaded file is not a CAMT.053 bank statement.")
    if isinstance(error, parser.MultipleAccounts):
        return _("The file contains statements for more than one bank account.")
    if isinstance(error, parser.MissingBookingDate):
        return _(
            "Entry %(position)s of the CAMT statement has neither a booking date "
            "nor a value date."
        ) % {"position": error.position}
    if isinstance(error, parser.EntryError):
        message = _ENTRY_MESSAGES.get(
            error.reason,
            _("Entry %(position)s of the CAMT statement could not be interpreted."),
        )
        return message % {"position": error.position}
    return _("The uploaded file could not be processed as a CAMT.053 bank statement.")


class Camt053Importer(BankTransactionImporter):
    """Bank transaction importer for CAMT.053 (BankToCustomerStatement) files."""

    identifier = "byro_finance_import_bank_files.camt053"
    label = _("CAMT.053 bank statement")

    def parse(self, source):
        with source.source_file.open("rb") as f:
            document = self._parse_document(f)
        logger.info(
            "CAMT import: version=%s statements=%s entries=%s skipped=%s "
            "transactions=%s batch_fallbacks=%s",
            document.version,
            len(document.statements),
            document.entry_count,
            document.skipped_entries,
            len(document.transactions),
            document.batch_fallbacks,
        )
        for transaction in document.transactions:
            yield ImportedBankTransaction(**dataclasses.asdict(transaction))

    def _parse_document(self, f):
        try:
            return parser.parse_camt053(f)
        except parser.CamtError as e:
            # str(e) is technical and free of bank data (error class, entry
            # position, XML parser position).
            logger.warning("CAMT import failed: %s: %s", type(e).__name__, e)
            raise InvalidImportFile(translate_error(e)) from e
