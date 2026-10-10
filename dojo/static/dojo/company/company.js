/*
 * company: small enhancements of upstream pages, loaded with defer from block extra_javascript in the
 * company base.html, so the whole page is parsed when it runs. It must never throw: upstream selenium
 * fails a test on any console error. Plain DOM only; nothing here waits for Alpine or jQuery.
 */
(function () {
  "use strict";

  function samePath(a, b) {
    try {
      return new URL(a, document.baseURI).pathname === new URL(b, document.baseURI).pathname;
    } catch (e) {
      return false;
    }
  }

  // "Assign…" and "Request risk acceptance…" in the findings bulk menu. The forms come from
  // <template id="company-bulk"> (company base.html, block modals). Only where form#bulk_change_form posts to a
  // findings bulk URL: the finding page has an endpoint bulk menu with the same ids, which must not get them.
  function addBulkForms() {
    var template = document.getElementById("company-bulk");
    var bulkForm = document.querySelector("form#bulk_change_form");
    var menu = document.getElementById("bulk_edit_menu");
    if (!template || !template.content || !bulkForm || !menu) {
      return;
    }
    var action = bulkForm.getAttribute("action") || "";
    var targets = [template.getAttribute("data-findings-bulk-url"), template.getAttribute("data-findings-bulk-product-url")];
    var findingsBulk = targets.some(function (url) {
      return url && samePath(action, url);
    });
    if (!findingsBulk) {
      return;
    }
    Array.prototype.forEach.call(template.content.querySelectorAll("form"), function (source) {
      var form = source.cloneNode(true);
      form.addEventListener("submit", function () {
        // the same selection upstream's bulk edit submits (findings_list_snippet.html, view_test.html)
        Array.prototype.forEach.call(form.querySelectorAll("input[name=finding_to_update]"), function (old) {
          old.parentNode.removeChild(old); // left over when the page comes back from the browser cache
        });
        Array.prototype.forEach.call(document.querySelectorAll("input[type=checkbox].select_one:checked"), function (box) {
          if (/^\d+$/.test(box.id)) {
            var input = document.createElement("input");
            input.type = "hidden";
            input.name = "finding_to_update";
            input.value = box.id;
            form.appendChild(input);
          }
        });
      });
      menu.appendChild(form);
    });
  }

  try {
    addBulkForms();
  } catch (e) {
    if (window.console && console.warn) {
      console.warn("company.js:", e);
    }
  }
})();
