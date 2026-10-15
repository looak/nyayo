from django.test import SimpleTestCase

from nyayo.runs import chessboard

WAC_001 = "2rr3k/pp3pp1/1nnqbN1p/3pN3/2pP4/2P3Q1/PPB4P/R4RK1 w - - 0 1"
WAC_002 = "8/7p/5k2/5p2/p1p2P2/Pr1pPK2/1P1R3P/8 b - - 0 1"


class Notation(SimpleTestCase):
    def test_uci_becomes_san(self):
        self.assertEqual(chessboard.san(WAC_001, "g3g6"), "Qg6")
        self.assertEqual(chessboard.san(WAC_002, "b3b2"), "Rxb2")

    def test_unreadable_moves_stay_as_they_were(self):
        self.assertEqual(chessboard.san(WAC_001, "a1a8"), "a1a8")
        self.assertEqual(chessboard.san(WAC_001, "nonsense"), "nonsense")
        self.assertEqual(chessboard.san("not a fen", "g3g6"), "g3g6")
        self.assertEqual(chessboard.san(WAC_001, ""), "")


class Board(SimpleTestCase):
    def test_arrows_for_expected_and_avoided_moves(self):
        svg = chessboard.board_svg(WAC_001, ["Qg6"], ["Nxf7+!?", "Kh2"])
        # an arrow is a line and a head, both in its colour
        self.assertEqual(svg.count(f'stroke="{chessboard.EXPECTED}"'), 1)
        # Nxf7+ is legal once the annotation is dropped; Kh2 isn't legal here and gets no arrow
        self.assertEqual(svg.count(f'stroke="{chessboard.AVOID}"'), 1)

    def test_bad_fen_draws_nothing(self):
        self.assertIsNone(chessboard.board_svg("not a fen", ["Qg6"]))
