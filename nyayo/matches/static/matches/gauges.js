// A needle that moves rather than jumps. The page is redrawn by reloading, so each needle remembers, for the session,
// where it stood; on the next load it starts there and turns to its new place. Without script it is simply drawn in place.
(function () {
  var needles = document.querySelectorAll(".gneedle[data-gauge]");
  var seen = {};
  try { seen = JSON.parse(sessionStorage.getItem("nyayo-gauges") || "{}"); } catch (e) {}
  needles.forEach(function (el) {
    var was = seen[el.dataset.gauge], now = parseFloat(el.dataset.angle);
    if (typeof was === "number" && was !== now) {
      el.style.transition = "none";
      el.style.transform = "rotate(" + was + "deg)";
      el.getBoundingClientRect();
      el.style.transition = "";
      requestAnimationFrame(function () { el.style.transform = "rotate(" + now + "deg)"; });
    }
    seen[el.dataset.gauge] = now;
  });
  try { sessionStorage.setItem("nyayo-gauges", JSON.stringify(seen)); } catch (e) {}
})();
