"""Build migration resources from the single authoritative root migrations directory."""

from pathlib import Path
from shutil import copy2

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        super().run()
        target = Path(self.build_lib) / "papersteward" / "_migrations"
        for source in Path("migrations").rglob("*"):
            if source.is_file() and (source.suffix in {".py", ".mako"} or source.name == "README"):
                destination = target / source.relative_to("migrations")
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy2(source, destination)
                destination.chmod(0o644)
        ini = Path("alembic.ini").read_text(encoding="utf-8")
        ini = ini.replace("%(here)s/migrations", "%(here)s")
        ini = ini.replace("prepend_sys_path = %(here)s/src", "prepend_sys_path =")
        (target / "alembic.ini").write_text(ini, encoding="utf-8")


setup(cmdclass={"build_py": BuildPy})
