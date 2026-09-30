"""
company: browser smoke test of the company UI layer, run by the test-company-ui job in company CI.

Starts as a smoke test and grows with each phase of the easier-UI plan. Runs as admin, like the
upstream suite; BaseTestCase.tearDown fails the test on any SEVERE browser console error.
"""
import sys
import unittest

from base_test_class import BaseTestCase, on_exception_html_source_logger
from selenium.webdriver.common.by import By


class CompanyFlowsTest(BaseTestCase):

    @on_exception_html_source_logger
    def test_my_work_shortcut(self):
        driver = self.driver
        driver.get(self.base_url + "finding/open")  # start elsewhere, so the checks below prove the click navigated
        driver.find_element(By.CSS_SELECTOR, '#dd-sidebar a[data-testid="company-nav-my-work"]').click()
        self.assertTrue(driver.current_url.rstrip("/").endswith("/dashboard"))
        self.assertTrue(self.is_text_present_on_page(text="Active Engagements"))


def suite():
    suite = unittest.TestSuite()
    suite.addTest(BaseTestCase("test_login"))
    suite.addTest(CompanyFlowsTest("test_my_work_shortcut"))
    return suite


if __name__ == "__main__":
    runner = unittest.TextTestRunner(descriptions=True, failfast=True, verbosity=2)
    ret = not runner.run(suite()).wasSuccessful()
    BaseTestCase.tearDownDriver()
    sys.exit(ret)
