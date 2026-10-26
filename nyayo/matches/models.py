from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone

# A producer's match is `running`, ends as one of the others, and may go back to running (OpenBench restarts tests).
STATES = ["running", "finished", "stopped", "error"]
KINDS = ["sprt", "gauntlet", "selfplay", "games"]
RESULTS = ["1-0", "0-1", "1/2-1/2", "*"]

# Ids end up in urls and file names (games/<match id>/<game>.pgn), so they're held to a safe alphabet.
MATCH_ID_MAX = 128
GAME_ID_MAX = 64
# starting with a letter or digit keeps "." and ".." out, which would climb out of the games directory
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
id_validator = RegexValidator(ID_PATTERN, "Letters, digits, . _ -, starting with a letter or digit.")


class Engine(models.Model):
    """An engine build as it identifies itself, shared by every match that used it as subject or opponent. What a
    match did with it (options, bench, rating) belongs to the match."""

    name = models.CharField(max_length=200)
    version = models.CharField(max_length=100, blank=True)
    commit = models.CharField(max_length=64, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["name", "version", "commit"], name="engine_identity")]
        ordering = ["name", "version"]

    def __str__(self):
        return f"{self.name} {self.version}".strip()


class Match(models.Model):
    id = models.CharField(max_length=MATCH_ID_MAX, primary_key=True, validators=[id_validator])
    machine = models.ForeignKey("runs.Machine", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="matches")
    kind = models.CharField(max_length=20, choices=[(k, k) for k in KINDS], db_index=True)
    state = models.CharField(max_length=10, choices=[(s, s) for s in STATES], db_index=True)
    producer = models.JSONField(default=dict)
    producer_name = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=200, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    host_name = models.CharField(max_length=200, blank=True)
    host = models.JSONField(default=dict)
    settings = models.JSONField(default=dict)
    subject = models.ForeignKey(Engine, on_delete=models.PROTECT, related_name="matches_as_subject")
    # the subject's branch, bench and options as played; the engine's identity is on `subject`
    subject_info = models.JSONField(default=dict)
    # SPRT bounds and the producer's own LLR and result, kind sprt only
    sprt = models.JSONField(null=True, blank=True)
    # the producer's own Elo and error { elo, error, model }, which may change as the match goes on
    stats = models.JSONField(null=True, blank=True)
    machines = models.JSONField(default=list)
    # { total, played, playing }; the schedule itself is rows (ScheduleEntry)
    progress = models.JSONField(null=True, blank=True)
    # the seq of the last report that carried one, which a report by changes must name as its base, and the digest of
    # the schedule as it now stands; both null/empty for a match whose producer sends whole documents
    report_seq = models.PositiveBigIntegerField(null=True, blank=True)
    schedule_digest = models.CharField(max_length=64, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    # when the producer last said anything, from the document; received_at is when nyayo heard it
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name_plural = "matches"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name or self.pk

    @property
    def schedule(self):
        """The schedule as the eight-field dicts of the match document, by game number. Cached on the instance;
        schedule.apply_* clear it."""
        if getattr(self, "_schedule", None) is None:
            self._schedule = [entry.as_dict() for entry in self.scheduled.order_by("number")]
        return self._schedule


class ScheduleEntry(models.Model):
    """One game of a gauntlet's schedule, the eight fields of an entry of the match document, with the hash the
    schedule's digest is made of. Kept apart from Game, the index of a PGN file that purge_games removes."""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="scheduled")
    number = models.PositiveIntegerField()
    round = models.IntegerField(null=True, blank=True)
    opponent = models.CharField(max_length=GAME_ID_MAX)
    subject_colour = models.CharField(max_length=5)
    state = models.CharField(max_length=10)
    result = models.CharField(max_length=10, null=True, blank=True)
    termination = models.CharField(max_length=200, null=True, blank=True)
    fault = models.JSONField(null=True, blank=True)
    hash = models.CharField(max_length=64)

    class Meta:
        ordering = ["match", "number"]
        verbose_name_plural = "schedule entries"
        constraints = [models.UniqueConstraint(fields=["match", "number"], name="one_entry_per_game_number")]

    def __str__(self):
        return f"{self.match_id}/{self.number}"

    def as_dict(self):
        return {name: getattr(self, name) for name in ("number", "round", "opponent", "subject_colour", "state",
                                                       "result", "termination", "fault")}


class MatchResult(models.Model):
    """One row per opponent, counted from the subject's side."""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="results")
    ordinal = models.PositiveIntegerField()
    opponent = models.CharField(max_length=GAME_ID_MAX, validators=[id_validator])
    engine = models.ForeignKey(Engine, on_delete=models.PROTECT, related_name="matches_as_opponent")
    # options, bench and branch as played, and the rating { value, list, as_of } when it has one
    info = models.JSONField(default=dict)
    rating = models.JSONField(null=True, blank=True)
    games = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    draws = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    # LL LD DD DW WW in pairs, null when the games weren't paired
    penta = models.JSONField(null=True, blank=True)
    # each { "subject": n, "opponent": n }
    timelosses = models.JSONField(default=dict)
    crashes = models.JSONField(default=dict)
    illegal = models.JSONField(default=dict)

    class Meta:
        ordering = ["match", "ordinal"]
        constraints = [models.UniqueConstraint(fields=["match", "opponent"], name="one_result_per_opponent")]

    def __str__(self):
        return f"{self.match_id} vs {self.opponent}"

    @property
    def score(self):
        """Points out of games, wins and half the draws, None before any game."""
        return (self.wins + self.draws / 2) / self.games if self.games else None


class MatchSnapshot(models.Model):
    """The counts as they stood when a report changed them. A re-send with the same counts adds none, so the game
    rate and the projection drawn from these aren't skewed by repeats. Kept with the match."""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="snapshots")
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    games = models.PositiveIntegerField()
    wins = models.PositiveIntegerField()
    draws = models.PositiveIntegerField()
    losses = models.PositiveIntegerField()
    # { opponent: [wins, draws, losses] }, to tell a change in one opponent's row from another's
    counts = models.JSONField(default=dict)
    llr = models.FloatField(null=True, blank=True)
    # the producer's { elo, error, model } at the time, when it sent them
    stats = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["match", "received_at"]


UPLOAD_STATES = ["pending", "done", "refused"]
BATCH_STATES = ["open", "sealed"]


class Upload(models.Model):
    """A producer's upload of finished games (PUT .../pgns/{name}), an OpenBench workload's .pgn.bz2 as sent. Taken
    without reading it: the file waits in uploads/<match id>/ as `pending` until the worker has checked every game
    and appended them to the match's batches (`done`), or refused it whole with the reason (`refused`). The file is
    deleted once done; the row stays, so a resend is recognised by its sha256. PROTECT: a match with games kept as data
    isn't deleted from under them; `manage.py purge_batches --match ID` comes first."""

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="uploads")
    name = models.CharField(max_length=GAME_ID_MAX, validators=[id_validator])
    # of the body as sent, and how it was sent
    bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    content_type = models.CharField(max_length=40, default="application/x-bzip2")
    encoding = models.CharField(max_length=20, blank=True)
    machine = models.ForeignKey("runs.Machine", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="uploads")
    received_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=10, choices=[(s, s) for s in UPLOAD_STATES], default="pending",
                             db_index=True)
    reason = models.TextField(blank=True)
    # games read from it, and how many of them are in batches so far: a worker stopped half way carries on from there
    games = models.PositiveIntegerField(default=0)
    appended = models.PositiveIntegerField(default=0)
    # the reader that checked it, and the numbers of the batches its games went to
    reader = models.CharField(max_length=40, blank=True)
    batches = models.JSONField(default=list)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["match", "name"]
        constraints = [models.UniqueConstraint(fields=["match", "name"], name="one_row_per_upload")]

    def __str__(self):
        return f"{self.match_id}/{self.name}"


class Batch(models.Model):
    """nyayo's own batch of a match's finished games, every game it keeps as data: 64 of them, or fewer for a match's
    last. While open it is a plain PGN file the worker appends to, batches/<match id>/<number>.pgn; sealed, it is
    compressed to <number>.pgn.zst and never changes. A game's place in it is on its card in the index database."""

    match = models.ForeignKey(Match, on_delete=models.PROTECT, related_name="batches")
    number = models.PositiveIntegerField()
    state = models.CharField(max_length=10, choices=[(s, s) for s in BATCH_STATES], default="open", db_index=True)
    games = models.PositiveIntegerField(default=0)
    # the plain PGN's length, which is the open file's committed length, and once sealed the stored file's size and hash
    bytes = models.PositiveBigIntegerField(default=0)
    stored_bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    # the reader that read its games, "python-chess 1.11.2" or "phant-indexer 1.3.1"
    reader = models.CharField(max_length=40, blank=True)
    # where its games came from, in order: {"upload": name, "first": n, "count": k} for a run of an upload's games
    # (their ids are <name>.<n> on), {"game": id} for a single game; what the cards are written again from
    sources = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)
    sealed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "batches"
        ordering = ["match", "number"]
        constraints = [models.UniqueConstraint(fields=["match", "number"], name="one_row_per_batch_number")]

    def __str__(self):
        return f"{self.match_id}/{self.number}"


class Game(models.Model):
    """The index of a game's PGN file (games/<match id>/<game>.pgn). Kept apart from the match's results so games can
    be purged without touching counts, snapshots or the document. Only touched when something other than a move
    changes."""

    match = models.ForeignKey(Match, on_delete=models.CASCADE, related_name="games")
    game = models.CharField(max_length=GAME_ID_MAX, validators=[id_validator])
    opponent = models.CharField(max_length=GAME_ID_MAX, blank=True)
    white = models.CharField(max_length=200, blank=True)
    black = models.CharField(max_length=200, blank=True)
    # the subject's colour, white or black; empty when the PGN doesn't say which engine is the subject
    colour = models.CharField(max_length=5, blank=True)
    result = models.CharField(max_length=10, choices=[(r, r) for r in RESULTS], default="*")
    termination = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    # A finished game waits for the worker to put it in its match's batches (worker.py), then says which batch it is
    # in; its file stays until that batch is sealed, and purge_games leaves it until then. Games finished before
    # nyayo kept batches wait for `manage.py keep_games`, not for the worker.
    waiting = models.BooleanField(default=False, db_index=True)
    kept_in = models.ForeignKey(Batch, null=True, blank=True, on_delete=models.SET_NULL, related_name="single_games")

    class Meta:
        ordering = ["match", "game"]
        constraints = [models.UniqueConstraint(fields=["match", "game"], name="one_row_per_game")]

    def __str__(self):
        return f"{self.match_id}/{self.game}"

    @property
    def live(self):
        return self.result == "*"


DATASET_STATES = ["requested", "running", "done", "failed"]


class Dataset(models.Model):
    """A position dataset (game database, milestone 10): positions drawn from the games kept, by the worker through
    `phant-indexer --draw`, for elephant's tuner. The record is kept for good, its file deleted or not: what was
    selected, the draw's settings and seed, both versions and the manifest of every batch read, so a rebuild reads
    the same games and is checked against the sha256 recorded (datasets.py)."""

    state = models.CharField(max_length=10, choices=[(s, s) for s in DATASET_STATES], default="requested",
                             db_index=True)
    # what it was used for, free text, and who asked for it: a machine's name, or "page"
    note = models.TextField(blank=True)
    requested_by = models.CharField(max_length=100, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # the filters given (V1: every game that read with a good label) and phant-indexer --draw's settings and seed
    selection = models.JSONField(default=dict)
    draw = models.JSONField(default=dict)
    # progress, exact: batches read of those selected, games read, positions written
    batches_total = models.PositiveIntegerField(default=0)
    batches_done = models.PositiveIntegerField(default=0)
    games = models.PositiveIntegerField(default=0)
    positions = models.PositiveIntegerField(default=0)
    nyayo_version = models.CharField(max_length=100, blank=True)
    reader = models.CharField(max_length=100, blank=True)
    reader_sha256 = models.CharField(max_length=64, blank=True)
    # every batch read, in order: its id, match, number and sha256, and each game taken (place, offset, length)
    manifest = models.JSONField(default=list)
    # the reader's statistics of the draw (--stats), as it wrote them: candidates, quiet, per game, written,
    # duplicates, results, games read and skipped
    stats = models.JSONField(null=True, blank=True)
    # the positions, decompressed: what a rebuild must give again
    sha256 = models.CharField(max_length=64, blank=True)
    file_bytes = models.PositiveBigIntegerField(default=0)
    file_deleted_at = models.DateTimeField(null=True, blank=True)
    # a rebuild's: the sha256 it had to give, and whether it did
    rebuild_of_sha256 = models.CharField(max_length=64, blank=True)
    rebuild_matched = models.BooleanField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at", "-pk"]

    def __str__(self):
        return f"dataset {self.pk}"
