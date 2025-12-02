# Copyright 2016 Camptocamp SA
# Copyright 2015 Odoo
# @author Pierre Verkest <pierre@verkest.fr>
# License AGPL-3.0 or later (http://www.gnu.org/licenses/lgpl.html)


import ast
import os
import signal
import subprocess
import threading
from contextlib import contextmanager
from unittest import mock
from pathlib import Path
from typing import Optional

import _pytest
import _pytest.python
import pytest

import odoo


def pytest_addoption(parser):
    parser.addoption("--odoo-database",
                     action="store",
                     help="Name of the Odoo database to test")
    parser.addoption("--odoo-config",
                     action="store",
                     help="Path of the Odoo configuration file")
    parser.addoption("--odoo-log-level",
                     action="store",
                     default='critical',
                     help="Log-level used by the Odoo process during tests")
    parser.addoption("--odoo-http",
                     action="store_true",
                     help="If pytest should launch an Odoo http server.")
    parser.addoption("--odoo-http-port",
                     action="store",
                     type=int,
                     default=8069,
                     help="Base HTTP port for Odoo server (default: 8069). "
                          "In parallel mode, workers use base_port+1, base_port+2, etc.")
    parser.addoption("--odoo-dev",
                     action="store")
    parser.addoption("--odoo-addons-path",
                     action="store")
    parser.addoption("--odoo-extra",
                     action="append",
                     default=[],
                     help="Extra options to pass to odoo "
                     "(e.g. --odoo-extra workers=0 --odoo-extra db-filter=odoo_test)")


@pytest.hookimpl(hookwrapper=True)
def pytest_cmdline_main(config):

    if (config.getoption('--odoo-database')
            or config.getoption('--odoo-config')
            or config.getoption('--odoo-dev')
            or os.environ.get('OPENERP_SERVER')
            or os.environ.get('ODOO_RC')):
        options = []
        # Replace --odoo-<something> by --<something> and prepare the argument
        # to propagate to odoo.
        available_options = [
            '--odoo-database',
            '--odoo-log-level',
            '--odoo-config',
            '--odoo-dev',
            '--odoo-addons-path',
        ]
        for option in available_options:
            value = config.getoption(option)
            if value:
                odoo_arg = '--%s' % option[7:]
                options.append('%s=%s' % (odoo_arg, value))

        extra_options = config.getoption("--odoo-extra")
        for extra_option in extra_options:
            options.append('--%s' % extra_option)

        # Check the environment variables supported by the Odoo Docker image
        # ref: https://hub.docker.com/_/odoo
        for arg in ['HOST', 'PORT', 'USER', 'PASSWORD']:
            if os.environ.get(arg):
                options.append('--db_%s=%s' % (arg.lower(), os.environ.get(arg)))

        odoo.tools.config.parse_config(options)

        if not odoo.tools.config['db_name']:
            # if you fall here, it means you have ODOO_RC or OPENERP_SERVER pointing
            # to a configuration file without 'database' configuration
            raise Exception(
                "please provide a database name in the Odoo configuration file"
            )
        disable_odoo_test_retry()
        monkey_patch_resolve_pkg_root_and_module_name()

        # Configure worker-specific HTTP port if running under xdist
        xdist_worker = os.getenv("PYTEST_XDIST_WORKER")
        if xdist_worker:
            try:
                worker_num = _get_worker_number(xdist_worker)
                base_port = config.getoption("--odoo-http-port", default=8069)
                # Use base_port + worker_num + 1 to avoid conflict with main process
                # Main process uses base_port, workers use base_port+1, base_port+2, etc.
                worker_port = base_port + worker_num + 1
                odoo.tools.config["http_port"] = worker_port
            except ValueError:
                # If worker ID parsing fails, continue with default port
                # Port conflict will occur, but better than crashing
                pass

        odoo.service.server.start(preload=[], stop=True)
        # odoo.service.server.start() modifies the SIGINT signal by its own
        # one which in fact prevents us to stop anthem with Ctrl-c.
        # Restore the default one.
        signal.signal(signal.SIGINT, signal.default_int_handler)

        if odoo.release.version_info >= (18,):
            odoo.modules.module.current_test = True

        if odoo.release.version_info < (15,):
            # Refactor in Odoo 15, not needed anymore
            with odoo.api.Environment.manage():
                yield
        else:
            yield
    else:
        yield


@pytest.fixture(scope="module", autouse=True)
def load_http(request):
    if request.config.getoption("--odoo-http"):
        # Configure worker-specific HTTP port if running under xdist
        xdist_worker = os.getenv("PYTEST_XDIST_WORKER")
        if xdist_worker:
            try:
                worker_num = _get_worker_number(xdist_worker)
                base_port = request.config.getoption("--odoo-http-port", default=8069)
                # Use base_port + worker_num + 1 to avoid conflict with main process
                worker_port = base_port + worker_num + 1
                odoo.tools.config["http_port"] = worker_port
            except ValueError:
                pass

        odoo.service.server.start(stop=True)
        signal.signal(signal.SIGINT, signal.default_int_handler)


def _get_worker_number(xdist_worker: str) -> int:
    """Extract worker number from PYTEST_XDIST_WORKER value.

    Args:
        xdist_worker: Worker ID like "gw0", "gw1", "gw2", etc.

    Returns:
        Worker number as integer (0, 1, 2, etc.)

    Raises:
        ValueError: If worker ID format is unexpected
    """
    if not xdist_worker:
        return 0

    # Standard pytest-xdist format: "gw" + number
    if xdist_worker.startswith("gw"):
        try:
            return int(xdist_worker[2:])
        except ValueError:
            raise ValueError(f"Unable to parse worker number from '{xdist_worker}'")

    # Fallback: try to parse as integer directly
    try:
        return int(xdist_worker)
    except ValueError:
        raise ValueError(f"Unexpected worker ID format: '{xdist_worker}'")


@contextmanager
def _shared_filestore(original_db_name, db_name):
    # This method ensure that if tests are ran in a distributed way
    # we share the filestore between the original database and the
    # copy of the database. This is useful to avoid copying the
    # filestore for each worker.
    # This is done by patching the filestore method of the odoo
    # configuration to point to the original filestore.
    if original_db_name == db_name:
        yield
        return
    with mock.patch.object(odoo.tools.config, "filestore") as filestore:
        fs_path = os.path.join(odoo.tools.config['data_dir'], 'filestore', original_db_name)
        filestore.return_value = fs_path
        yield

@contextmanager
def _worker_db_name(config=None):
    """Configure worker-specific database and HTTP port for parallel execution.

    When running under pytest-xdist, each worker receives:
    - A unique database: {original_db_name}-{worker_id}
    - A unique HTTP port: base_port + worker_number + 1

    Args:
        config: pytest Config object to access CLI options (optional)

    Yields:
        str: The database name for this worker
    """
    xdist_worker = os.getenv("PYTEST_XDIST_WORKER")
    original_db_name = db_name = odoo.tests.common.get_db_name()
    original_http_port = odoo.tools.config.get('http_port', 8069)

    try:
        if xdist_worker:
            # Configure worker-specific database
            db_name = f"{original_db_name}-{xdist_worker}"
            subprocess.run(["dropdb", db_name, "--if-exists"], check=True)
            subprocess.run(["createdb", "-T", original_db_name, db_name], check=True)
            odoo.tools.config["db_name"] = db_name
            odoo.tools.config["dbfilter"] = f"^{db_name}$"

            # Configure worker-specific HTTP port
            try:
                worker_num = _get_worker_number(xdist_worker)
                base_port = original_http_port
                if config:
                    # Use CLI option if provided
                    base_port = config.getoption("--odoo-http-port", default=8069)
                # Use base_port + worker_num + 1 to avoid conflict with main process
                # Main process uses base_port, workers use base_port+1, base_port+2, etc.
                worker_port = base_port + worker_num + 1
                odoo.tools.config["http_port"] = worker_port
            except ValueError:
                # If worker ID parsing fails, continue with original port
                pass

        with _shared_filestore(original_db_name, db_name):
            yield db_name
    finally:
        if db_name != original_db_name:
            odoo.sql_db.close_db(db_name)
            subprocess.run(["dropdb", db_name, "--if-exists"], check=True)
            odoo.tools.config["db_name"] = original_db_name
            odoo.tools.config["dbfilter"] = f"^{original_db_name}$"
            # Restore original HTTP port
            odoo.tools.config["http_port"] = original_http_port

    
@pytest.fixture(scope='session', autouse=True)
def load_registry(request):
    # Initialize the registry before running tests.
    # If we don't do that, the modules will be loaded *inside* of the first
    # test we run, which would trigger the launch of the postinstall tests
    # (because we force 'test_enable' to True and the at end of the loading of
    # the registry, the postinstall tests are run when test_enable is enabled).
    # And also give wrong timing indications.
    # Finally we enable `testing` flag on current thread
    # since Odoo sets it when loading test suites.
    threading.current_thread().testing = True
    with _worker_db_name(config=request.config) as db_name:
        odoo.modules.registry.Registry(db_name)
        yield


@pytest.fixture(scope='module', autouse=True)
def enable_odoo_test_flag():
    # When we run tests through Odoo, test_enable is always activated, and some
    # code might rely on this (for instance to selectively disable database
    # commits). When we run the tests through pytest, the flag is not
    # activated, and if it was activated globally, it would make odoo start all
    # tests in addition to the tests we are running through pytest.  If we
    # enable the option only in the scope of the tests modules, we won't
    # interfere with the odoo's loading of modules, thus we are good.
    odoo.tools.config['test_enable'] = True
    yield
    odoo.tools.config['test_enable'] = False

def monkey_patch_resolve_pkg_root_and_module_name():
    original_resolve_pkg_root_and_module_name = _pytest.pathlib.resolve_pkg_root_and_module_name

    def resolve_pkg_root_and_module_name(
        path: Path, *, consider_namespace_packages: bool = False
    ) -> "tuple[Path, str]":
        pkg_root, module_name = original_resolve_pkg_root_and_module_name(
            path, consider_namespace_packages=consider_namespace_packages
        )

        if not module_name.startswith("odoo.addons"):
            manifest = _find_manifest_path(path)
            if manifest and manifest.parent.name == module_name.split(".",1)[0]:
                module_name = "odoo.addons." + module_name
        return pkg_root, module_name


    _pytest.pathlib.resolve_pkg_root_and_module_name= resolve_pkg_root_and_module_name


def disable_odoo_test_retry():
    """Odoo BaseCase.run method overload TestCase.run and manage
    a retry mechanism that breaks using pytest launcher.
    Using `pytest-rerunfailures` we can use `--reruns` parameters
    if needs equivalent feature, so we remove such overload here.
    """
    try:
        from odoo.tests import BaseCase
        del BaseCase.run
    except (ImportError, AttributeError):
        # Odoo <= 15.0
        pass

def _find_manifest_path(collection_path: Path) -> Path:
    """Try to locate an Odoo manifest file in the collection path."""
    # check if collection_path is an addon directory
    path = collection_path
    for _ in range(5):
        if (path.parent / "__manifest__.py").is_file():
            break
        path = path.parent
    else:
        return None
    return path.parent / "__manifest__.py"


def pytest_ignore_collect(collection_path: Path) -> Optional[bool]:
    """Do not collect tests of modules that are marked non installable."""
    manifest_path = _find_manifest_path(collection_path)
    if not manifest_path:
        return None
    manifest = ast.literal_eval(manifest_path.read_text())
    if not manifest.get("installable", True):
        # installable = False, do not collect this
        return True
    return None
