/* The page works with scripts off: the form posts normally. With scripts on,
   the request is sent in place and the answer is shown under the button. */
(function () {
  "use strict";
  var form = document.getElementById("pilot-form");
  if (!form || !window.fetch || !window.FormData) return;
  var ok = document.getElementById("pilot-ok");
  var err = document.getElementById("pilot-err");
  var button = form.querySelector("button[type=submit]");

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    ok.hidden = true;
    err.hidden = true;
    button.disabled = true;
    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { Accept: "application/json" },
    })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        form.reset();
        ok.hidden = false;
        ok.focus();
      })
      .catch(function () {
        err.hidden = false;
        err.focus();
      })
      .then(function () {
        button.disabled = false;
      });
  });
})();
