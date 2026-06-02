// Tiny enhancements layered on top of HTMX. Keep this file small.
// Larger JS frameworks are explicitly out of scope (see UI_CONTEXT.md §4).

(function () {
  // Render the /health JSON response as a colored pill instead of raw JSON.
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    const el = evt.target;
    if (!el || !el.matches("[hx-get='/health']")) return;
    try {
      const data = JSON.parse(el.textContent.trim());
      const ok = data && data.status === "ok";
      el.innerHTML = ok
        ? '<span class="h-1.5 w-1.5 rounded-full bg-green-500"></span> healthy'
        : '<span class="h-1.5 w-1.5 rounded-full bg-red-500"></span> unhealthy';
      el.className = ok
        ? "inline-flex items-center gap-1 rounded-full bg-green-100 text-green-800 text-xs font-medium px-2.5 py-1"
        : "inline-flex items-center gap-1 rounded-full bg-red-100 text-red-800 text-xs font-medium px-2.5 py-1";
    } catch (_) {
      /* leave as-is */
    }
  });
})();
