"""Our own ratings, fitted from every gauntlet game (`Engine.ours`).

One logistic model with draws over the counts of every non-hidden gauntlet match, BayesElo's shape: a win for A over B
has probability L((a - b) - d), a loss L(-(a - b) - d) and a draw the rest (Rao and Kupper), with L the logistic curve
at 400 Elo to the decade and `d` one draw width fitted with the ratings. Draws are modelled, not counted as half a
point, because engine games draw about half the time and half points would count each of them as a full game of
evidence, which makes every error too small.

Each engine with a list rating (`Engine.rating.value`) has a Gaussian prior centred there, `NYAYO_RATING_PRIOR` Elo wide
(default 20). An Elephant version, or an opponent without a list rating, has a flat prior. The fit is the MAP,
by L-BFGS on an analytic gradient; the errors are the square roots of the diagonal of the inverse Hessian there (the
Hessian by central differences of the gradient: about 30 engines). An engine whose games never lead to an engine with
a prior is left out, since its place on the scale would be arbitrary.

`ours` is `{ value, error, games, as_of, flagged, z }`: the rating, its one-sigma error, the games it rests on, the
date of the newest match among them, and whether the games disagree with the list. It is the rating *on the list's
scale, under our conditions* (hardware, book, hash); no shift or stretch between the two scales is fitted.

**Flagged.** The posterior already sits near the prior, so comparing it with the list's number would almost never fire.
The games' own evidence is taken out of it instead: with `p` the posterior's precision and `s` the prior's, the games
alone say `(p * ours - s * list) / (p - s)` with error `1 / sqrt(p - s)`. It is flagged when that differs from the list
by more than twice its error combined with the prior's (so a list number and a games-only number that can't both be
right). `z` is that difference in units of the combined error.
"""

import logging
import math
from dataclasses import dataclass, field

import numpy as np
from django.conf import settings
from django.db import transaction
from scipy.optimize import minimize

from .models import Engine, MatchResult

log = logging.getLogger("nyayo.ratings")

K = math.log(10) / 400
FLAG_SIGMAS = 2.0


def prior_sigma():
    return float(getattr(settings, "NYAYO_RATING_PRIOR", 20))


@dataclass
class Fitted:
    engine: Engine
    value: float
    error: float
    games: int
    as_of: str
    flagged: bool
    z: float | None


@dataclass
class Fit:
    engines: list = field(default_factory=list)
    draw: float | None = None  # the draw width, in Elo
    games: int = 0
    skipped: list = field(default_factory=list)  # engines no list rating anchors


def counts():
    """{ (subject id, opponent id): [wins, draws, losses, newest match's date] } over every non-hidden gauntlet
    match, from the subject's side, summed over matches."""
    pairs = {}
    rows = MatchResult.objects.filter(match__kind="gauntlet", match__hidden=False).select_related("match")
    for r in rows:
        if r.games == 0 or r.engine_id == r.match.subject_id:
            continue
        total = pairs.setdefault((r.match.subject_id, r.engine_id), [0, 0, 0, ""])
        total[0] += r.wins
        total[1] += r.draws
        total[2] += r.losses
        total[3] = max(total[3], r.match.updated_at.date().isoformat())
    return pairs


def anchored(ids, pairs, prior):
    """The engines in `ids` connected, through games, to one with a prior."""
    neighbours = {i: set() for i in ids}
    for a, b in pairs:
        neighbours[a].add(b)
        neighbours[b].add(a)
    seen, stack = set(), [i for i in ids if i in prior]
    while stack:
        i = stack.pop()
        if i not in seen:
            seen.add(i)
            stack.extend(neighbours[i] - seen)
    return seen


def logistic(u):
    return 0.5 * (1 + np.tanh(0.5 * u))


class Model:
    """The negative log posterior of ratings `x` (one per engine) and the draw width `exp(t)`, as parameters `[x.., t]`."""

    def __init__(self, index, pairs, centres, sigma):
        self.n = len(index)
        self.a = np.array([index[a] for a, _ in pairs])
        self.b = np.array([index[b] for _, b in pairs])
        self.w, self.d, self.l = (np.array([pairs[p][i] for p in pairs], dtype=float) for i in range(3))
        self.mu = np.array([centres.get(i, 0.0) for i in index])
        self.tight = np.array([1.0 / sigma ** 2 if i in centres else 0.0 for i in index])

    def probabilities(self, params):
        x, delta = params[:self.n], math.exp(params[self.n])
        diff = x[self.a] - x[self.b]
        win = logistic(K * (diff - delta))
        lose_not = logistic(K * (diff + delta))  # 1 - the loss probability
        return diff, delta, win, lose_not

    def value(self, params):
        _, _, win, lose_not = self.probabilities(params)
        p_win, p_loss, p_draw = win, 1 - lose_not, np.maximum(lose_not - win, 1e-300)
        nll = -(self.w * np.log(np.maximum(p_win, 1e-300)) + self.l * np.log(np.maximum(p_loss, 1e-300))
                + self.d * np.log(p_draw)).sum()
        return nll + 0.5 * (self.tight * (params[:self.n] - self.mu) ** 2).sum()

    def gradient(self, params):
        _, delta, win, lose_not = self.probabilities(params)
        sw, sl = win * (1 - win), lose_not * (1 - lose_not)
        p_draw = np.maximum(lose_not - win, 1e-300)
        # derivatives of the three probabilities by the difference, and by the draw width
        d_win, d_loss, d_draw = K * sw, -K * sl, K * (sl - sw)
        e_win, e_loss, e_draw = -K * sw, -K * sl, K * (sl + sw)
        by_diff = -(self.w / np.maximum(win, 1e-300) * d_win + self.l / np.maximum(1 - lose_not, 1e-300) * d_loss
                    + self.d / p_draw * d_draw)
        by_delta = -(self.w / np.maximum(win, 1e-300) * e_win + self.l / np.maximum(1 - lose_not, 1e-300) * e_loss
                     + self.d / p_draw * e_draw)
        grad = np.zeros(self.n + 1)
        np.add.at(grad, self.a, by_diff)
        np.add.at(grad, self.b, -by_diff)
        grad[:self.n] += self.tight * (params[:self.n] - self.mu)
        grad[self.n] = by_delta.sum() * delta
        return grad

    def hessian(self, params, step=1e-3):
        size = len(params)
        out = np.zeros((size, size))
        for i in range(size):
            up, down = params.copy(), params.copy()
            up[i] += step
            down[i] -= step
            out[i] = (self.gradient(up) - self.gradient(down)) / (2 * step)
        return (out + out.T) / 2


def solve(pairs, centres, sigma):
    """Fits the pairs ({ (a, b): [w, d, l, ..] }) with priors `centres` ({ id: Elo }). Returns (ratings, errors, draw width)
    as dicts by id (errors) and a number."""
    index = {}
    for a, b in pairs:
        index.setdefault(a, len(index))
        index.setdefault(b, len(index))
    model = Model(index, pairs, centres, sigma)
    start = np.concatenate([model.mu, [math.log(100.0)]])
    centre = np.mean(list(centres.values())) if centres else 0.0
    start[:model.n] = np.where(model.tight > 0, model.mu, centre)
    result = minimize(model.value, start, jac=model.gradient, method="L-BFGS-B", options={"maxiter": 2000, "gtol": 1e-9, "ftol": 1e-15})
    covariance = np.linalg.inv(model.hessian(result.x))
    errors = np.sqrt(np.maximum(np.diag(covariance), 0))
    ratings = {i: float(result.x[k]) for i, k in index.items()}
    return ratings, {i: float(errors[k]) for i, k in index.items()}, math.exp(result.x[model.n])


def flag(value, error, centre, sigma):
    """(flagged, z): whether the games alone disagree with the list's number, as the module's docstring says."""
    posterior = 1.0 / error ** 2
    data = posterior - 1.0 / sigma ** 2
    if data <= 1e-9:
        return False, None
    alone = (posterior * value - centre / sigma ** 2) / data
    combined = math.sqrt(1.0 / data + sigma ** 2)
    z = (alone - centre) / combined
    return abs(z) > FLAG_SIGMAS, z


def fit(sigma=None):
    """Fits every stored gauntlet game and returns the Fit, writing nothing."""
    sigma = sigma or prior_sigma()
    pairs = counts()
    ids = {i for pair in pairs for i in pair}
    engines = {e.pk: e for e in Engine.objects.filter(pk__in=ids)}
    centres = {pk: float(e.rating["value"]) for pk, e in engines.items()
               if isinstance(e.rating, dict) and isinstance(e.rating.get("value"), (int, float))}
    keep = anchored(ids, pairs, centres)
    out = Fit(skipped=[engines[i] for i in sorted(ids - keep)])
    pairs = {p: v for p, v in pairs.items() if p[0] in keep and p[1] in keep}
    if not pairs:
        return out
    ratings, errors, out.draw = solve(pairs, {i: c for i, c in centres.items() if i in keep}, sigma)
    games, newest = {}, {}
    for (a, b), (w, d, l, date) in pairs.items():
        for i in (a, b):
            games[i] = games.get(i, 0) + w + d + l
            newest[i] = max(newest.get(i, ""), date)
    out.games = sum(w + d + l for w, d, l, _ in pairs.values())
    for i in sorted(ratings, key=lambda i: (engines[i].name, engines[i].version)):
        flagged, z = flag(ratings[i], errors[i], centres[i], sigma) if i in centres else (False, None)
        out.engines.append(Fitted(engines[i], round(ratings[i], 1), round(errors[i], 1), games[i], newest[i], flagged,
                                  None if z is None else round(z, 2)))
    return out


def ours(fitted):
    return {"value": fitted.value, "error": fitted.error, "games": fitted.games, "as_of": fitted.as_of,
            "flagged": fitted.flagged, "z": fitted.z}


@transaction.atomic
def refit():
    """Fits and stores `ours` on every engine, clearing it on one that left the fit. Writes only the rows that
    changed, so a refit of the same games changes nothing. Returns the Fit."""
    result = fit()
    wanted = {f.engine.pk: ours(f) for f in result.engines}
    for engine in Engine.objects.filter(ours__isnull=False).exclude(pk__in=wanted):
        engine.ours = None
        engine.save(update_fields=["ours"])
    for f in result.engines:
        if f.engine.ours != wanted[f.engine.pk]:
            f.engine.ours = wanted[f.engine.pk]
            f.engine.save(update_fields=["ours"])
    return result


def refit_quietly():
    """`refit`, for when a report must not fail because of it: what goes wrong is logged and the old ratings stay."""
    try:
        return refit()
    except Exception:
        log.exception("refitting our ratings failed")
        return None
