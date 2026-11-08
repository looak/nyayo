// The tune's page: a Copy button for the C++ (hidden without script, as the text is there to select).
(function () {
  "use strict";
  document.querySelectorAll("button[data-copy]").forEach(function (button) {
    var source = document.getElementById(button.dataset.copy);
    if (!source || !navigator.clipboard) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(source.textContent).then(function () {
        button.textContent = "Copied";
        setTimeout(function () { button.textContent = "Copy the C++"; }, 1500);
      });
    });
  });
})();
