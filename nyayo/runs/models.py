import hashlib
import secrets

from django.db import models
from django.utils import timezone

from . import chessboard

STATES = ["wip", "committed", "published"]


class Machine(models.Model):
    """A box that pushes runs. Each has its own token, so a run can be traced to who sent it and one machine can be
    revoked without touching the others. Only the token's hash is stored."""

    name = models.CharField(max_length=100, unique=True)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    active = models.BooleanField(default=True, help_text="Untick to revoke the token.")
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    def __str__(self):
        return self.name

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def new_token(self):
        """Sets a fresh token and returns it; it's shown once and can't be recovered."""
        token = secrets.token_urlsafe(32)
        self.token_hash = self.hash_token(token)
        return token

    @classmethod
    def for_token(cls, token):
        if not token:
            return None
        return cls.objects.filter(token_hash=cls.hash_token(token), active=True).first()


class Run(models.Model):
    id = models.UUIDField(primary_key=True)
    machine = models.ForeignKey(Machine, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    kind = models.CharField(max_length=20)
    source = models.CharField(max_length=10)
    trigger = models.CharField(max_length=20, blank=True)
    request_id = models.CharField(max_length=100, null=True, blank=True)
    state = models.CharField(max_length=10, choices=[(s, s) for s in STATES])
    commit = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    base_commit = models.CharField(max_length=40, null=True, blank=True)
    tree = models.CharField(max_length=40, blank=True)
    branch = models.CharField(max_length=200, blank=True, db_index=True)
    ancestors = models.JSONField(default=list)
    version = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    recorded_at = models.DateTimeField(db_index=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    host_name = models.CharField(max_length=200, blank=True)
    host = models.JSONField(default=dict)
    build = models.JSONField(default=dict)
    ci = models.JSONField(null=True, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self):
        return self.short_name

    @property
    def short_name(self):
        head = self.commit[:10] if self.commit else "wip"
        return f"{head}-{self.kind}-{self.recorded_at.strftime('%Y%m%dT%H%M%S')}"

    @property
    def revision(self):
        """The commit, or the tree a wip run would commit as."""
        return self.commit or self.tree

    @property
    def lineage(self):
        """Commits this run descends from, nearest first: its commit (or base commit while wip) then its ancestors."""
        return self.ancestors or [c for c in (self.commit or self.base_commit,) if c]


class Check(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="checks")
    ordinal = models.PositiveIntegerField()
    check_id = models.CharField(max_length=100, db_index=True)
    version = models.IntegerField(null=True)
    name = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=10)
    deterministic = models.BooleanField(default=True)
    # what makes two checks comparable, the readable parts and their canonical form in `key`
    limit_type = models.CharField(max_length=20, blank=True)
    limit_value = models.BigIntegerField(null=True)
    threads = models.IntegerField(null=True)
    hash_mb = models.IntegerField(null=True)
    experiment = models.BooleanField(default=False)
    compiler = models.CharField(max_length=100, blank=True)
    config = models.CharField(max_length=50, blank=True)
    assertions = models.BooleanField(null=True)
    exceptions = models.BooleanField(null=True)
    bits = models.IntegerField(null=True)
    key = models.CharField(max_length=64, db_index=True)
    per_file = models.IntegerField(null=True)
    params = models.JSONField(default=dict)
    build = models.JSONField(default=dict)
    engine_verdict = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["run", "ordinal"]

    def __str__(self):
        return f"{self.check_id} ({self.name})"

    def metric(self, name):
        for metric in self.metrics.all():
            if metric.name == name:
                return metric.value
        return None

    @property
    def totals(self):
        """solved/total, plus points/max for scored suites, as text."""
        solved, total = self.metric("solved"), self.metric("total")
        text = f"{solved:g}/{total:g}" if solved is not None and total is not None else ""
        points, max_points = self.metric("points"), self.metric("max_points")
        if points is not None and max_points is not None:
            text += f", {points:g}/{max_points:g} pts"
        return text

    @property
    def settings(self):
        limit = f"{self.limit_type} {self.limit_value}" if self.limit_type else "?"
        text = f"{limit}, {self.threads} thread{'s' if self.threads != 1 else ''}, {self.hash_mb} MB"
        if self.per_file:
            text += f", {self.per_file} per file"
        if self.experiment:
            text += ", experiment"
        return text


class Metric(models.Model):
    check_result = models.ForeignKey(Check, on_delete=models.CASCADE, related_name="metrics")
    name = models.CharField(max_length=100)
    value = models.FloatField()
    unit = models.CharField(max_length=20, blank=True)


class Case(models.Model):
    check_result = models.ForeignKey(Check, on_delete=models.CASCADE, related_name="cases")
    ordinal = models.PositiveIntegerField()
    case_id = models.CharField(max_length=100, db_index=True)
    file = models.CharField(max_length=200, blank=True)
    file_sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10)
    got = models.CharField(max_length=20, blank=True)
    points = models.IntegerField(null=True)
    max_points = models.IntegerField(null=True)
    score = models.IntegerField(null=True)
    mate = models.BooleanField(null=True)
    nodes = models.BigIntegerField(null=True)
    time_ms = models.IntegerField(null=True)
    expected = models.JSONField(default=list)
    avoid = models.JSONField(default=list)
    pv = models.TextField(blank=True)
    fen = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["check_result", "ordinal"]

    @property
    def identity(self):
        """A position is the same position when its id and its EPD file's content match."""
        return (self.case_id, self.file_sha256)

    @property
    def played(self):
        """The move the engine chose, in SAN."""
        return chessboard.san(self.fen, self.got)
