"""The rules uwanja's gauntlet loader (`uwanja/config.py`, as of looak/uwanja#8) holds a file to, copied here as a fixture
so that a file the gauntlet page builds can be checked without uwanja. It raises `ConfigError` where uwanja would."""

import re

TOP_KEYS = {"name", "tc", "book", "book_format", "book_order", "rounds", "concurrency", "adjudication", "recover",
            "pgn_options", "fastchess", "subject", "opponent"}
ENGINE_KEYS = {"id", "name", "version", "url", "ref", "asset", "binary", "build", "cmd", "dir", "options", "args",
               "bench", "rating", "fastchess_args"}
RATING_KEYS = {"value", "list", "as_of", "source"}


class ConfigError(Exception):
    pass


def only(table, known, where):
    if not isinstance(table, dict):
        raise ConfigError(f"{where} must be a table")
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown {', '.join(map(repr, unknown))}")


def slug(text):
    text = re.sub(r"/releases/.*$|\.git$|\.exe$", "", str(text).rstrip("/"))
    piece = re.split(r"[/\\]", text)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", piece).strip("-").lower() or "engine"


def engine(table, role, where, taken):
    only(table, ENGINE_KEYS, where)
    if bool(table.get("url")) == bool(table.get("cmd")):
        raise ConfigError(f"{where}: give exactly one of url or cmd")
    if role == "subject" and "rating" in table:
        raise ConfigError(f"{where}: the engine under test has no rating")
    ident = table.get("id") or slug(table.get("name") or table.get("url") or table.get("cmd"))
    base, number = ident, 2
    while ident in taken:
        ident, number = f"{base}-{number}", number + 1
    taken.add(ident)
    for name in ("url", "ref", "asset", "binary", "cmd", "name", "id"):
        if name in table and not isinstance(table[name], str):
            raise ConfigError(f"{where}: {name} is text")
    if not isinstance(table.get("options", {}), dict):
        raise ConfigError(f"{where}: options is a table")
    rating = table.get("rating")
    if rating is not None and not isinstance(rating, (int, float)):
        only(rating, RATING_KEYS, f"{where} rating")
        if "value" not in rating:
            raise ConfigError(f"{where}: rating needs a value")
        float(rating["value"])
    return ident


def parse(raw):
    """Returns the ids of the subject and of the opponents, in order."""
    only(raw, TOP_KEYS, "the gauntlet file")
    for key in ("tc", "subject"):
        if key not in raw:
            raise ConfigError(f"missing {key!r}")
    if not isinstance(raw["subject"], dict):
        raise ConfigError("[subject] must be a table")
    opponents = raw.get("opponent")
    if not isinstance(opponents, list) or not opponents:
        raise ConfigError("at least one [[opponent]] is needed")
    taken = set()
    subject = engine(raw["subject"], "subject", "subject", taken)
    others = [engine(entry, "opponent", f"opponent {n}", taken) for n, entry in enumerate(opponents, 1)]
    name = str(raw.get("name") or "gauntlet")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ConfigError(f"name {name!r} may only hold letters, digits, '.', '_' and '-'")
    rounds, concurrency = raw.get("rounds", 1), raw.get("concurrency", 1)
    if not (isinstance(rounds, int) and rounds > 0 and isinstance(concurrency, int) and concurrency > 0):
        raise ConfigError("rounds and concurrency are whole numbers above zero")
    return subject, others
