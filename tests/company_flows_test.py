"""
company: browser smoke test of the company UI layer, run by the test-company-ui job in company CI.

Starts as a smoke test and grows with each phase of the easier-UI plan. Runs as admin, like the
upstream suite; BaseTestCase.tearDown fails the test on any SEVERE browser console error.
"""
import os
import sys
import unittest
from pathlib import Path

from base_test_class import BaseTestCase, on_exception_html_source_logger
from product_test import ProductTest
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait


class CompanyFlowsTest(BaseTestCase):

    @on_exception_html_source_logger
    def test_my_work_shortcut(self):
        driver = self.driver
        driver.get(self.base_url + "finding/open")  # start elsewhere, so the checks below prove the click navigated
        driver.find_element(By.CSS_SELECTOR, '#dd-sidebar a[data-testid="company-nav-my-work"]').click()
        self.assertTrue(driver.current_url.rstrip("/").endswith("/dashboard"))
        self.assertTrue(self.is_text_present_on_page(text="Active Engagements"))

    @on_exception_html_source_logger
    def test_quick_upload(self):
        # Phase 2: "Upload scan" in the sidebar, then "Upload new report" on the test page re-imports into the same test
        driver = self.driver
        report = str(Path(os.path.realpath(__file__)).parent / "zap_sample.xml")
        driver.get(self.base_url + "dashboard")
        driver.find_element(By.CSS_SELECTOR, '#dd-sidebar a[data-testid="company-nav-upload"]').click()
        # choosing the product, then the scanner, re-renders the page with them in the query string. The
        # product is pre-selected when it is admin's only one (CI); Select would not click it again, so no reload.
        product = Select(driver.find_element(By.ID, "id_product"))
        if product.first_selected_option.text != "QA Test":
            product.select_by_visible_text("QA Test")
            WebDriverWait(driver, 10).until(lambda d: "product=" in d.current_url)
        Select(driver.find_element(By.ID, "id_scan_type")).select_by_value("ZAP Scan")
        WebDriverWait(driver, 10).until(lambda d: "scan_type=" in d.current_url)
        driver.find_element(By.ID, "id_file").send_keys(report)
        driver.find_element(By.CSS_SELECTOR, 'button[data-testid="company-upload-submit"]').click()
        self.assertTrue(self.is_success_message_present(text="Upload complete: 4 new"))
        test_url = driver.current_url
        self.assertIn("/test/", test_url)

        driver.find_element(By.CSS_SELECTOR, 'a[data-testid="company-upload-new-report"]').click()
        driver.find_element(By.ID, "id_file").send_keys(report)
        driver.find_element(By.CSS_SELECTOR, 'button[data-testid="company-upload-submit"]').click()
        self.assertTrue(self.is_success_message_present(text="0 new, 0 closed, 0 reactivated, 4 unchanged"))
        self.assertEqual(driver.current_url, test_url)

    def listed_finding_ids(self):
        return {box.get_attribute("id") for box in self.driver.find_elements(By.CSS_SELECTOR, "input[type=checkbox].select_one")}

    @on_exception_html_source_logger
    def test_bulk_assign(self):
        # Phase 3: tick two findings, "Assign…", pick the team and admin, Save; "My findings" and "My team's findings"
        # list them. Needs the team "qa" with admin in it: no page creates teams (they come from the directory or
        # manage.py company_teams), so the test-company-ui job runs the command before this suite, and so must a local run.
        driver = self.driver
        driver.get(self.base_url + "finding/open")
        boxes = driver.find_elements(By.CSS_SELECTOR, "input[type=checkbox].select_one")[:2]
        self.assertEqual(len(boxes), 2, "the quick upload test leaves four open findings")
        chosen = {box.get_attribute("id") for box in boxes}
        for box in boxes:
            box.click()
        assign = driver.find_element(By.CSS_SELECTOR, '#bulk_edit_menu button[data-testid="company-bulk-assign"]')
        WebDriverWait(driver, 10).until(lambda _driver: assign.is_displayed())
        assign.click()
        Select(driver.find_element(By.ID, "company-assign-team")).select_by_value("qa")
        driver.find_element(By.XPATH, "//select[@id='company-assign-person']/optgroup[@label='qa']/option[contains(., '(admin)')]").click()
        driver.find_element(By.CSS_SELECTOR, 'button[data-testid="company-assign-save"]').click()
        self.assertTrue(self.is_success_message_present(text="Assigned 2 findings to qa / "))

        driver.find_element(By.CSS_SELECTOR, '#dd-sidebar a[data-testid="company-nav-my-findings"]').click()
        self.assertEqual(self.listed_finding_ids(), chosen)
        driver.find_element(By.CSS_SELECTOR, '#dd-sidebar a[data-testid="company-nav-team-findings"]').click()
        self.assertEqual(self.listed_finding_ids(), chosen)


def suite():
    suite = unittest.TestSuite()
    suite.addTest(BaseTestCase("test_login"))
    suite.addTest(CompanyFlowsTest("test_my_work_shortcut"))
    suite.addTest(ProductTest("test_create_product"))
    suite.addTest(CompanyFlowsTest("test_quick_upload"))
    suite.addTest(CompanyFlowsTest("test_bulk_assign"))
    suite.addTest(ProductTest("test_delete_product"))
    return suite


if __name__ == "__main__":
    runner = unittest.TextTestRunner(descriptions=True, failfast=True, verbosity=2)
    ret = not runner.run(suite()).wasSuccessful()
    BaseTestCase.tearDownDriver()
    sys.exit(ret)
