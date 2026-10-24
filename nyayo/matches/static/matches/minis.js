// The small boards of the games being played (the match list and a match's page): each is a chessground, view only,
// drawn from the position the page put on it. The pages reload themselves while a match runs, which redraws them.
import { Chessground } from "../vendor/chessground-9.1.1/chessground.min.js";

document.querySelectorAll(".mini-board").forEach(function (el) {
  var last = el.dataset.last ? el.dataset.last.split(" ") : undefined;
  Chessground(el, {fen: el.dataset.fen, lastMove: last, orientation: el.dataset.orientation || "white",
                   viewOnly: true, coordinates: false, animation: {enabled: false}});
});
