// Converte <time datetime="..."> (UTC) para o fuso horário do navegador.
(() => {
  "use strict";

  function localizeTimes(root) {
    root.querySelectorAll("time[datetime]").forEach((element) => {
      const date = new Date(element.getAttribute("datetime"));
      if (!Number.isNaN(date.getTime())) {
        element.title = element.textContent;
        element.textContent = date.toLocaleString();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => localizeTimes(document));
  document.addEventListener("htmx:afterSwap", (event) => localizeTimes(event.target));
})();
