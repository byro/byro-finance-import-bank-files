import pathlib
import shutil
import subprocess

from setuptools import setup
from setuptools.command.build_py import build_py

PACKAGE = "byro_finance_import_bank_files"
HERE = pathlib.Path(__file__).resolve().parent
LOCALE_DIR = HERE / PACKAGE / "locale"


class CustomBuild(build_py):
    """Compile the gettext catalogues and ship them in the wheel.

    ``.mo`` files are not checked in. They are compiled with ``msgfmt`` in
    every build from source (isolated or not) and copied into ``build_lib``
    explicitly, so the wheel never depends on manifest discovery of generated
    files. A build without gettext fails instead of silently shipping a
    package without translations. Installing a built wheel does not need
    gettext.
    """

    def run(self):
        compiled = self.compile_catalogues()
        super().run()
        for mo in compiled:
            target = pathlib.Path(self.build_lib) / mo.relative_to(HERE)
            self.mkpath(str(target.parent))
            self.copy_file(str(mo), str(target))

    def compile_catalogues(self):
        msgfmt = shutil.which("msgfmt")
        if msgfmt is None:
            raise SystemExit(
                "msgfmt not found: gettext is required to build "
                "byro-finance-import-bank-files from source. Installing a "
                "prebuilt wheel does not need it."
            )
        compiled = []
        for po in sorted(LOCALE_DIR.glob("*/LC_MESSAGES/*.po")):
            mo = po.with_suffix(".mo")
            subprocess.run(
                [msgfmt, "--check-format", "-o", str(mo), str(po)], check=True
            )
            compiled.append(mo)
        return compiled


setup(cmdclass={"build_py": CustomBuild})
