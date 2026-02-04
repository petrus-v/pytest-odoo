import unittest
import odoo
from odoo.tests.common import HttpCase


@unittest.skipIf(odoo.release.version_info >= (18,), "Broken on Odoo >=18")
class TestModuleCommon(HttpCase):
    def test_pytest_endpoints(self):
        self.authenticate("demo", "demo")
        result = self.url_open("/pytest-odoo/test", allow_redirects=False)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.text, "Hello World")
