# pyScheme3

A R7RS-small Scheme interpreter written in Python, implemented as a
[CEK machine](https://en.wikipedia.org/wiki/CEK_Machine).
The Python source is structured as a 1:1 prototype for a C11/C++17 port.

## Features

- R7RS-small compliant numeric tower (exact/inexact, complex, rational)
- Hygienic macros (`syntax-rules`, sets-of-scopes model)
- Proper tail calls (CEK machine guarantees TCO)
- `call/cc` and `dynamic-wind`
- `raise` / `raise-continuable` / `with-exception-handler` (R7RS §6.11)
- Ports: text and binary, file and string ports
- Module system (`.py` extension modules loadable at runtime)
- Interactive REPL with readline support (Windows and Unix)
- Built-in debugger and tracer (`]debug`, `]trace`)
- Interactive help system: `(help)`, `(apropos "...")`

## Requirements

- Python 3.10+
- No third-party dependencies

## Installation

```
git clone https://github.com/rprovost11-sketch/pyScheme3.git
cd pyScheme3
```

## Usage

```
# Interactive REPL
python -m pyscheme

# REPL rooted at a directory
python -m pyscheme <directory>

# Evaluate a file and exit
python -m pyscheme <file.scm>
```

## Architecture

The interpreter is a four-stage pipeline:

| Stage | Module | Responsibility |
|-------|--------|----------------|
| Parser | `Parser.py` | Tokenize and build cons-cell AST |
| Expander | `Expander.py` | Macro expansion (syntax-rules, hygienic) |
| Analyzer | `Analyzer.py` | Validation and static arity checking |
| Evaluator | `Evaluator.py` | CEK machine dispatch |

All stages share a single cons-cell representation (`AST.py`); there is no
typed intermediate representation.

## License

GNU General Public License v3.0 - see [LICENSE](LICENSE).
