"""The tools a worker is asked to look for (the describe job), and whether what it found builds an engine.

A worker says what it has in `toolchains` of its system info: `{ "g++": "13.3.0", "rustc": null, ... }`, a version or
`null` for each tool it was asked about. An engine lists the compilers it can be built with (`build_compilers`: `g`,
`clang`, `g++>=10`, `rustc`...). Only C and C++ are scheduled for now (ramani plans/nyayo-2.md, *Worker registration*):
Rust and C# need a build recipe the engine has no makefile for, and Python builds nothing. A worker without `toolchains`
(from before the describe job) is judged by the `compilers` it scanned for itself, as it was."""

TOOLS = ['gcc', 'g++', 'clang', 'clang++', 'dotnet', 'rustc', 'cargo', 'python3', 'make']

# the tools that build C and C++, which are all that is scheduled
SCHEDULED = {'gcc', 'g++', 'clang', 'clang++'}

# a compiler as an engine's file names it, and the tools that can be it
CANDIDATES = {'g': ['g++', 'gcc'], 'gcc': ['gcc'], 'g++': ['g++'], 'clang': ['clang', 'clang++'], 'clang++': ['clang++']}


def version_tuple(text):
    try:
        return tuple(int(part) for part in str(text).split('.'))
    except ValueError:
        return (0, 0, 0)


def found(toolchains, spec):
    """The tool and its version that satisfies one compiler of an engine's list (`g`, `clang`, `g++>=10`), or None."""
    name, _, wanted = spec.partition('>=')
    minimum = version_tuple(wanted) if wanted else (0, 0, 0)
    for tool in CANDIDATES.get(name, []):
        version = toolchains.get(tool)
        if version and tool in SCHEDULED and version_tuple(version) >= minimum:
            return tool, version
    return None


def can_build(info, build):
    """(True, []) or (False, [why]) for a worker's info and an engine's `build()`: the compilers it needs, and make."""
    info = info if isinstance(info, dict) else {}
    specs = build.get('compilers') or []
    toolchains = info.get('toolchains')
    if not isinstance(toolchains, dict) or not toolchains:
        # a worker that has not described itself (its first registration has an empty toolchains): by what it scanned for, as before
        scanned = info.get('compilers') or {}
        return (True, []) if scanned else (False, ['Missing %s' % specs])
    if not toolchains.get('make'):
        return False, ['Missing make']
    if any(found(toolchains, spec) for spec in specs):
        return True, []
    if specs and not any(spec.partition('>=')[0] in CANDIDATES for spec in specs):
        return False, ['Only C and C++ are scheduled, and the engine builds with %s' % specs]
    return False, ['Missing %s' % specs]


def compiler_of(info, build):
    """'g++ 13.3.0', what builds the engine on this worker, for the page; empty when nothing does."""
    toolchains = (info or {}).get('toolchains')
    if not isinstance(toolchains, dict):
        return ''
    for spec in build.get('compilers') or []:
        hit = found(toolchains, spec)
        if hit:
            return '%s %s' % hit
    return ''
