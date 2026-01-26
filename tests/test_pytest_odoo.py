import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import TestCase, mock

from _pytest import pathlib as pytest_pathlib
from pytest_odoo import (
    _find_manifest_path,
    _get_worker_number,
    _shared_filestore,
    pytest_ignore_collect,
    monkey_patch_resolve_pkg_root_and_module_name,
    disable_odoo_test_retry,
)


class TestPytestOdoo(TestCase):

    @contextmanager
    def fake_module(self, with_manifest=True,  using_addons_namespace=False):
        directory = tempfile.TemporaryDirectory()
        try:
            module_path = Path(directory.name)
            files = []
            if using_addons_namespace:
                files.append(module_path / "odoo" / "__init__.py")
                files.append(module_path / "odoo" / "addons" / "__init__.py")
                module_path = module_path / "odoo" / "addons" / "my_module"
                module_path.mkdir(parents=True, exist_ok=True)
            manifest_path = None
            if with_manifest:
                manifest_path = module_path / "__manifest__.py"
                files.append(manifest_path)
            test_path = module_path / "tests" / "test_module.py"
            test_path.parent.mkdir(parents=True, exist_ok=True)
            files.append(test_path)
            files.append(module_path / "__init__.py")
            files.append(module_path / "tests" / "__init__.py")
            for file_path in files:
                file_path.touch()
            yield (module_path, manifest_path, test_path,)
        finally:
            directory.cleanup()


    def test_find_manifest_path_less_than_5_directories(self):
        self.assertIsNone(_find_manifest_path(Path("/some/path")))

    def test_find_manifest_path_from_test_module(self):
        with self.fake_module() as (_, manifest_path, test_path):
            self.assertEqual(_find_manifest_path(test_path), manifest_path)

    def test_find_manifest_path_from_itself(self):
        with self.fake_module() as (_, manifest_path, _):
            self.assertEqual(_find_manifest_path(manifest_path), manifest_path)

    def test_find_manifest_path_from_brother(self):
        with self.fake_module() as (module_path, manifest_path, _):
            test = module_path / "test_something.py"
            test.touch()
            self.assertEqual(_find_manifest_path(test), manifest_path)

    def test_resolve_pkg_root_and_module_name(self):
        monkey_patch_resolve_pkg_root_and_module_name()
        with self.fake_module() as (module_path, _, test_path):
            pkg_root, module_name = pytest_pathlib.resolve_pkg_root_and_module_name(test_path)
            self.assertEqual(
                module_name,
                f"odoo.addons.{module_path.name}.tests.test_module"
            )

    def test_resolve_pkg_root_and_module_name_not_odoo_module(self):
        monkey_patch_resolve_pkg_root_and_module_name()

        with self.fake_module(with_manifest=False) as (module_path, _, test_path):
            pkg_root, module_name = pytest_pathlib.resolve_pkg_root_and_module_name(test_path)
            self.assertEqual(
                module_name,
                f"{module_path.name}.tests.test_module"
            )

    def test_resolve_pkg_root_and_module_name_namespace_ok(self):
        monkey_patch_resolve_pkg_root_and_module_name()

        with self.fake_module(with_manifest=True, using_addons_namespace=True) as (module_path, _, test_path):
            pkg_root, module_name = pytest_pathlib.resolve_pkg_root_and_module_name(test_path)
            self.assertEqual(
                module_name,
                "odoo.addons.my_module.tests.test_module"
            )

    def test_disable_odoo_test_retry(self):
        from odoo.tests import BaseCase

        original_basecase_run = BaseCase.__dict__.get("run")

        def restore_basecase_run():
            if original_basecase_run is not None:
                BaseCase.run = original_basecase_run

        self.addCleanup(restore_basecase_run)

        disable_odoo_test_retry()
        # Check that 'run' is not defined directly on BaseCase (inherited is OK)
        self.assertNotIn("run", BaseCase.__dict__)


    def test_disable_odoo_test_retry_ignore_run_doesnt_exists(self):
        from odoo.tests import BaseCase

        original_basecase_run = BaseCase.__dict__.get("run")

        def restore_basecase_run():
            if original_basecase_run is not None:
                BaseCase.run = original_basecase_run

        self.addCleanup(restore_basecase_run)

        # Remove 'run' if it exists directly on BaseCase
        if "run" in BaseCase.__dict__:
            del BaseCase.run

        disable_odoo_test_retry()
        # Check that 'run' is not defined directly on BaseCase (inherited is OK)
        self.assertNotIn("run", BaseCase.__dict__)



    def test_import_error(self):
        from odoo import tests

        original_BaseCase = tests.BaseCase

        def restore_basecase():
            tests.BaseCase = original_BaseCase

        self.addCleanup(restore_basecase)

        disable_odoo_test_retry()


class TestGetWorkerNumber(TestCase):
    """Tests for _get_worker_number() helper function."""

    def test_standard_xdist_format_gw0(self):
        """Worker gw0 should return 0."""
        self.assertEqual(_get_worker_number("gw0"), 0)

    def test_standard_xdist_format_gw1(self):
        """Worker gw1 should return 1."""
        self.assertEqual(_get_worker_number("gw1"), 1)

    def test_standard_xdist_format_gw99(self):
        """Worker gw99 should return 99."""
        self.assertEqual(_get_worker_number("gw99"), 99)

    def test_empty_string_returns_zero(self):
        """Empty string should return 0."""
        self.assertEqual(_get_worker_number(""), 0)

    def test_none_like_empty_returns_zero(self):
        """Falsy values should return 0."""
        self.assertEqual(_get_worker_number(None), 0)

    def test_integer_string_fallback(self):
        """Plain integer string should be parsed as fallback."""
        self.assertEqual(_get_worker_number("5"), 5)

    def test_invalid_gw_format_raises_valueerror(self):
        """Invalid gw format (non-numeric suffix) should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            _get_worker_number("gwabc")
        self.assertIn("Unable to parse worker number", str(ctx.exception))

    def test_unexpected_format_raises_valueerror(self):
        """Completely unexpected format should raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            _get_worker_number("worker-1")
        self.assertIn("Unexpected worker ID format", str(ctx.exception))


class TestSharedFilestore(TestCase):
    """Tests for _shared_filestore() context manager."""

    def test_same_db_name_no_patching(self):
        """When original and db_name are the same, no patching should occur."""
        with mock.patch("pytest_odoo.mock.patch.object") as mock_patch:
            with _shared_filestore("test_db", "test_db"):
                pass
            # mock.patch.object should not be called when names are same
            mock_patch.assert_not_called()

    def test_different_db_name_patches_filestore(self):
        """When db names differ, filestore should be patched to original."""
        import odoo.tools.config

        original_filestore = odoo.tools.config.filestore
        patched_path = None

        with _shared_filestore("original_db", "worker_db"):
            # Inside the context, filestore should return the original db path
            patched_path = odoo.tools.config.filestore("any_db")

        # Verify the patched path points to original_db filestore
        self.assertIn("original_db", patched_path)

        # After context, filestore should be restored
        self.assertEqual(odoo.tools.config.filestore, original_filestore)


class TestPytestIgnoreCollect(TestCase):
    """Tests for pytest_ignore_collect() hook."""

    @contextmanager
    def fake_odoo_module(self, manifest_content=None):
        """Create a fake Odoo module with optional manifest content."""
        directory = tempfile.TemporaryDirectory()
        try:
            module_path = Path(directory.name) / "my_module"
            module_path.mkdir(parents=True, exist_ok=True)

            # Create __init__.py
            (module_path / "__init__.py").touch()

            # Create tests directory
            tests_path = module_path / "tests"
            tests_path.mkdir(parents=True, exist_ok=True)
            (tests_path / "__init__.py").touch()
            test_file = tests_path / "test_something.py"
            test_file.touch()

            # Create manifest
            manifest_path = module_path / "__manifest__.py"
            if manifest_content is not None:
                manifest_path.write_text(manifest_content)
            else:
                manifest_path.write_text("{'name': 'My Module'}")

            yield module_path, test_file
        finally:
            directory.cleanup()

    def test_installable_true_returns_none(self):
        """Module with installable=True should not be ignored."""
        with self.fake_odoo_module("{'name': 'Test', 'installable': True}") as (_, test_file):
            result = pytest_ignore_collect(test_file)
            self.assertIsNone(result)

    def test_installable_false_returns_true(self):
        """Module with installable=False should be ignored."""
        with self.fake_odoo_module("{'name': 'Test', 'installable': False}") as (_, test_file):
            result = pytest_ignore_collect(test_file)
            self.assertTrue(result)

    def test_installable_not_specified_defaults_to_true(self):
        """Module without installable key should default to installable=True."""
        with self.fake_odoo_module("{'name': 'Test'}") as (_, test_file):
            result = pytest_ignore_collect(test_file)
            self.assertIsNone(result)

    def test_no_manifest_returns_none(self):
        """Path without manifest should return None (don't ignore)."""
        result = pytest_ignore_collect(Path("/some/random/path"))
        self.assertIsNone(result)
