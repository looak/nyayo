"""What the common CPU flags a worker reports mean, one line each, for the tooltips on the Machines page. The names are as
the worker writes them (OpenBench's `Client`): matched without case, dots, dashes or underscores, so `SSE4.1`, `sse4_1`
and `SSE41` are one. A flag that is not here is shown as a plain pill."""

HELP = {
    'POPCNT': 'population count: counts the set bits of a word in one instruction, the basic tool of bitboards',
    'BMI1': 'bit manipulation, set 1: lowest set bit, and-not and trailing zero count',
    'BMI2': 'bit manipulation, set 2: PEXT and PDEP, which gather and spread bits, as sliding-piece attack tables use',
    'LZCNT': 'counts the leading zero bits of a word in one instruction',
    'SSE': '128-bit single-precision vector instructions',
    'SSE2': '128-bit integer and double-precision vector instructions; every x86-64 CPU has it',
    'SSE3': '128-bit vectors, with horizontal adds and other additions to SSE2',
    'SSSE3': 'supplemental SSE3: byte shuffles and horizontal integer operations',
    'SSE41': '128-bit vectors, with blends, integer min and max and packed multiplies',
    'SSE42': '128-bit vectors, with string compares and a CRC32 instruction',
    'SSE4A': "AMD's extra SSE4 instructions, mostly bit field insert and extract",
    'AVX': '256-bit floating point vector instructions',
    'AVX2': '256-bit integer and float vector instructions; what NNUE evaluation runs on',
    'FMA': 'fused multiply-add: a multiply and an add in one instruction, with one rounding',
    'AVX512F': '512-bit vector instructions, the foundation set',
    'AVX512BW': '512-bit vectors on bytes and words',
    'AVX512DQ': '512-bit vectors on doublewords and quadwords, with conversions',
    'AVX512VL': 'the 512-bit instructions on 128- and 256-bit vectors, with mask registers',
    'AVX512CD': '512-bit conflict detection',
    'AVX512VNNI': '512-bit neural network instructions: 8-bit multiply and accumulate for NNUE',
    'AVXVNNI': '256-bit neural network instructions: 8-bit multiply and accumulate for NNUE',
    'AVX512VBMI': '512-bit byte permutes',
    'AVX512VBMI2': '512-bit byte and word compress, expand and shifts',
    'AVX512BITALG': '512-bit bit counting on bytes and words',
    'AVX512VPOPCNTDQ': '512-bit population count on doublewords and quadwords',
    'AVX512ER': '512-bit exponential and reciprocal (Xeon Phi)',
    'AVX512PF': '512-bit prefetch (Xeon Phi)',
    'AVX512IFMA': '512-bit integer multiply-add of 52 bits',
    'AMX': 'advanced matrix extensions: tile matrix multiplies',
    'NEON': "ARM's 128-bit vector instructions",
    'SVE': "ARM's scalable vector extension",
    'SVE2': "the second version of ARM's scalable vector extension",
    'DOTPROD': "ARM's 8-bit dot product, for NNUE",
    'AES': 'AES encryption instructions',
    'SHA': 'SHA hashing instructions',
    'PCLMULQDQ': 'carry-less multiply',
    'RDRND': 'a hardware random number generator',
    'RDSEED': 'a hardware seed for random number generators',
    'ADX': 'multi-precision add with carry',
    'MOVBE': 'loads and stores with the bytes swapped',
    'F16C': 'half-precision float conversions',
    'XSAVE': 'saving the vector registers on a context switch',
    'CX16': 'a 16-byte compare and exchange',
}


def key(flag):
    return ''.join(c for c in flag.upper() if c.isalnum())


def describe(flag):
    return HELP.get(key(flag), '')


def pills(flags):
    """[(flag, what it means or '')], in the order the worker sent them."""
    return [(flag, describe(flag)) for flag in flags]
