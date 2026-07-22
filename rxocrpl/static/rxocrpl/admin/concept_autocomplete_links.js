(function ($) {
      "use strict";

      function conceptChangeUrl(select, value) {
            const template = select.dataset.changeUrlTemplate;
            if (!template || !value) {
                  return null;
            }
            return template.replace("__value__", encodeURIComponent(value));
      }

      function selectedOptions(select) {
            return Array.from(select.options).filter((option) => option.selected);
      }

      function wireConceptChips(select) {
            const container = $(select).next(".select2-container").get(0);
            if (!container) {
                  return;
            }

            const choices = container.querySelectorAll(".select2-selection__choice");
            const options = selectedOptions(select);

            choices.forEach((choice, index) => {
                  const option = options[index];
                  const url = option ? conceptChangeUrl(select, option.value) : null;
                  if (!url) {
                        return;
                  }

                  choice.dataset.changeUrl = url;
                  choice.title = option.text;
                  choice.style.cursor = "pointer";
            });
      }

      function wireAllConceptChips() {
            document.querySelectorAll("select[data-change-url-template]").forEach(wireConceptChips);
      }

      $(function () {
            const selector = "select[data-change-url-template]";

            wireAllConceptChips();

            $(document).on("select2:select select2:unselect change", selector, function () {
                  window.setTimeout(wireAllConceptChips, 0);
            });

            $(document).on("click", ".select2-selection__choice", function (event) {
                  if (event.target.classList.contains("select2-selection__choice__remove")) {
                        return;
                  }

                  const url = this.dataset.changeUrl;
                  if (url) {
                        window.location.href = url;
                  }
            });
      });
})(django.jQuery);
