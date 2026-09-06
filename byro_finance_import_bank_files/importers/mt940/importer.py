"""byro integration: the MT940 bank transaction importer.

This module is the only place where the MT940 code touches byro. It opens the
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

_STATEMENT_MESSAGES = {
    "invalid date": _(
        "Statement %(statement)s of the MT940 file contains an invalid date."
    ),
    "invalid amount": _(
        "Statement %(statement)s of the MT940 file contains an invalid amount."
    ),
    "statement lines could not be separated": _(
        "Statement %(statement)s of the MT940 file contains a statement line (:61:) "
        "without a transaction type code, so its transactions could not be separated."
    ),
}

_ENTRY_MESSAGES = {
    "invalid credit/debit mark": _(
        "Transaction %(position)s in statement %(statement)s of the MT940 file has "
        "no valid debit/credit mark."
    ),
    "invalid amount": _(
        "Transaction %(position)s in statement %(statement)s of the MT940 file has "
        "an invalid amount."
    ),
    "zero amount": _(
        "Transaction %(position)s in statement %(statement)s of the MT940 file has "
        "an amount of zero."
    ),
}


def translate_error(error: parser.Mt940Error) -> str:
    """User presentable, translatable message for a parser error.

    The messages describe the problem without repeating any file content;
    statements and transactions are identified by their position only.
    """
    if isinstance(error, parser.UnsupportedEncoding):
        return _(
            "The file is encoded as UTF-16 or UTF-32, which is not supported. "
            "Please export the statement as UTF-8 or Windows-1252 text."
        )
    if isinstance(error, parser.UndecodableFile):
        return _("The file could not be decoded as UTF-8 or Windows-1252 text.")
    if isinstance(error, parser.NotMt940):
        return _("The uploaded file is not an MT940 bank statement.")
    if isinstance(error, parser.MultipleAccounts):
        return _("The file contains statements for more than one bank account.")
    if isinstance(error, parser.UnparseableField):
        return _(
            "Field :%(tag)s: in statement %(statement)s of the MT940 file could not "
            "be interpreted."
        ) % {"tag": error.tag, "statement": error.statement}
    if isinstance(error, parser.MissingAccountIdentification):
        return _(
            "Statement %(statement)s of the MT940 file has no account "
            "identification (:25:)."
        ) % {"statement": error.statement}
    if isinstance(error, parser.MissingCurrency):
        return _(
            "Statement %(statement)s of the MT940 file has no balance and therefore "
            "no currency."
        ) % {"statement": error.statement}
    if isinstance(error, parser.InvalidEntry):
        message = _ENTRY_MESSAGES.get(
            error.reason,
            _(
                "Transaction %(position)s in statement %(statement)s of the MT940 "
                "file could not be interpreted."
            ),
        )
        return message % {"position": error.position, "statement": error.statement}
    if isinstance(error, parser.StatementError):
        message = _STATEMENT_MESSAGES.get(
            error.reason,
            _("Statement %(statement)s of the MT940 file could not be interpreted."),
        )
        return message % {"statement": error.statement}
    return _("The uploaded file could not be processed as an MT940 bank statement.")


class Mt940Importer(BankTransactionImporter):
    """Bank transaction importer for SWIFT MT940 customer statement files."""

    identifier = "byro_finance_import_bank_files.mt940"
    label = _("MT940 bank statement")

    def parse(self, source):
        with source.source_file.open("rb") as f:
            document = self._parse_document(f)
        logger.info(
            "MT940 import: encoding=%s statements=%s transactions=%s "
            "balance_mismatches=%s",
            document.encoding,
            len(document.statements),
            len(document.transactions),
            document.balance_mismatches,
        )
        for transaction in document.transactions:
            yield ImportedBankTransaction(**dataclasses.asdict(transaction))

    def _parse_document(self, f):
        try:
            return parser.parse_mt940(f)
        except parser.Mt940Error as e:
            # str(e) is technical and free of bank data (error class, statement
            # and line positions, field tag).
            logger.warning("MT940 import failed: %s: %s", type(e).__name__, e)
            raise InvalidImportFile(translate_error(e)) from e
