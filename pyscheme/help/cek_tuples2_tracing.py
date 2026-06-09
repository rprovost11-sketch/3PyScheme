# CEK machine in Python, loop-refined variant of cek_tuples.py.
#
# Companion file, not a replacement. cek_tuples.py stays the
# canonical form because its explicit EVAL/APPLY state variable
# matches the way CEK machines are presented in papers and text-
# books - with transition rules tagged by state. This variant is
# a refinement that "reifies the state register as program
# location," one of the standard steps in Danvy's functional-
# correspondence chain from evaluator to abstract machine.
#
# The two machine states are encoded lexically:
#
#   - Inside the top inner `while True:` loop    -> state EVAL
#   - Inside the bottom inner `while True:` loop -> state APPLY
#
# Each inner loop follows the same convention:
#
#   - Stay in this state: do not break (same-state transition)
#   - Switch to the other state: break
#
# The outer `while True:` restarts the EVAL loop after APPLY
# breaks out of its loop.
#
# The EVAL loop is genuine: EXPR_APP and EXPR_IF push a frame
# and keep descending (no break), so the loop iterates. The
# three leaf cases (EXPR_INT, EXPR_VAR, EXPR_LAM) produce a
# value and break out to APPLY.
#
# The APPLY loop, in this particular machine, happens to break
# on every reachable path. Pure lambda calculus + if has no
# frame that computes a value from pre-evaluated inputs - every
# frame either returns (empty K) or transitions to EVAL to
# process a new subexpression. So APPLY runs exactly one
# iteration per outer step here.
#
# That is a property of this *language*, not of the machine
# shape. Add a primitive like `+` and its result-producing
# frame will naturally loop in APPLY, delivering its computed
# value to the next frame without re-entering EVAL. The
# symmetric two-loop form is the right skeleton for grafting
# new features onto this file.


# -------- Tag constants (C enum equivalents) --------

# Expression kinds
EXPR_INT = 0
EXPR_VAR = 1
EXPR_LAM = 2
EXPR_APP = 3
EXPR_IF = 4

# Value kinds
VAL_INT = 0
VAL_CLOSURE = 1

# Frame kinds
FRAME_IF = 0
FRAME_ARG = 1
FRAME_CALL = 2


# -------- Environment --------
#
# Linked list of (name, value, parent) cells. Empty env is None.

def env_lookup(env, name):
    while env is not None:
        if env[0] == name:
            return env[1]
        env = env[2]
    raise NameError("unbound variable: " + name)


def env_extend(env, name, value):
    return (name, value, env)


# -------- Tracing (opt-in via cek_eval(expr, trace=True)) --------
#
# Prints the four registers (C, V, E, K) at the end of every
# transition so you can watch the machine step through a program.

_EXPR_NAMES = {EXPR_INT: "INT", EXPR_VAR: "VAR", EXPR_LAM: "LAM",
               EXPR_APP: "APP", EXPR_IF: "IF"}
_VAL_NAMES = {VAL_INT: "INT", VAL_CLOSURE: "CLO"}
_FRAME_NAMES = {FRAME_IF: "IF", FRAME_ARG: "ARG", FRAME_CALL: "CALL"}


def _fmt_expr(e):
    if e is None:
        return "-"
    tag = e[0]
    name = _EXPR_NAMES.get(tag, "?")
    if tag == EXPR_INT:
        return f"({name} {e[1]})"
    if tag == EXPR_VAR:
        return f"({name} {e[1]})"
    if tag == EXPR_LAM:
        return f"({name} {e[1]} {_fmt_expr(e[2])})"
    if tag == EXPR_APP:
        return f"({name} {_fmt_expr(e[1])} {_fmt_expr(e[2])})"
    if tag == EXPR_IF:
        return f"({name} {_fmt_expr(e[1])} {_fmt_expr(e[2])} {_fmt_expr(e[3])})"
    return "?"


def _fmt_val(v):
    if v is None:
        return "-"
    tag = v[0]
    name = _VAL_NAMES.get(tag, "?")
    if tag == VAL_INT:
        return f"({name} {v[1]})"
    if tag == VAL_CLOSURE:
        return f"({name} {v[1]} {_fmt_expr(v[2])})"
    return "?"


def _fmt_env(env):
    pairs = []
    while env is not None:
        pairs.append(f"{env[0]}={_fmt_val(env[1])}")
        env = env[2]
    return "[" + ", ".join(pairs) + "]"


def _fmt_frame(f):
    tag = f[0]
    name = _FRAME_NAMES.get(tag, "?")
    if tag == FRAME_IF:
        return f"{name}(then={_fmt_expr(f[1])}, else={_fmt_expr(f[2])})"
    if tag == FRAME_ARG:
        return f"{name}(arg={_fmt_expr(f[1])})"
    if tag == FRAME_CALL:
        return f"{name}(fn={_fmt_val(f[1])})"
    return "?"


def _fmt_k(K):
    if not K:
        return "[]"
    return "[" + ", ".join(_fmt_frame(f) for f in K) + "]"


def _trace(label, C, V, E, K):
    print(f"  [{label}]")
    print(f"     C = {_fmt_expr(C)}")
    print(f"     V = {_fmt_val(V)}")
    print(f"     E = {_fmt_env(E)}")
    print(f"     K = {_fmt_k(K)}")


# -------- The CEK machine --------
#
# Registers:
#   C : current expression (used while in the EVAL inner loop)
#   V : current value      (used while in the APPLY block)
#   E : current environment (linked list, or None)
#   K : continuation stack  (Python list used as a stack)
#
# No state register - lexical position encodes EVAL vs APPLY.
#
# Expression forms:
#   (EXPR_INT, n)
#   (EXPR_VAR, name)
#   (EXPR_LAM, param, body)
#   (EXPR_APP, fn_expr, arg_expr)
#   (EXPR_IF,  test, then_br, else_br)
#
# Value forms:
#   (VAL_INT,     n)
#   (VAL_CLOSURE, param, body, captured_env)
#
# Frame forms:
#   (FRAME_IF,   then_br, else_br, env)   waiting on a test value
#   (FRAME_ARG,  arg_expr, env)           waiting on a function value
#   (FRAME_CALL, fn_value)                waiting on an argument value

def cek_eval(expr, trace=False):
    C = expr
    V = None
    E = None
    K = []

    while True:

        # ----- state == EVAL: descend until we produce a value -----
        while True:
            tag = C[0]

            if tag == EXPR_INT:
                # C = (EXPR_INT, n)
                V = (VAL_INT, C[1])
                if trace:
                    _trace("EVAL/EXPR_INT", C, V, E, K)
                break

            elif tag == EXPR_VAR:
                # C = (EXPR_VAR, name)
                V = env_lookup(E, C[1])
                if trace:
                    _trace("EVAL/EXPR_VAR", C, V, E, K)
                break

            elif tag == EXPR_LAM:
                # C = (EXPR_LAM, param, body)
                V = (VAL_CLOSURE, C[1], C[2], E)
                if trace:
                    _trace("EVAL/EXPR_LAM", C, V, E, K)
                break

            elif tag == EXPR_APP:
                # C = (EXPR_APP, fn_expr, arg_expr)
                # Evaluate fn first; remember arg and env for later.
                K.append((FRAME_ARG, C[2], E))
                C = C[1]
                if trace:
                    _trace("EVAL/EXPR_APP", C, V, E, K)
                # no break - keep descending, still in EVAL

            elif tag == EXPR_IF:
                # C = (EXPR_IF, test, then_br, else_br)
                # Evaluate test first; remember both branches and env.
                K.append((FRAME_IF, C[2], C[3], E))
                C = C[1]
                if trace:
                    _trace("EVAL/EXPR_IF", C, V, E, K)
                # no break - keep descending, still in EVAL

            else:
                raise RuntimeError("unknown expression tag: " + str(tag))

        # ----- state == APPLY: consume V against the top frame -----
        while True:
            if not K:
                if trace:
                    _trace("APPLY/RETURN", C, V, E, K)
                return V

            frame = K.pop()
            ftag = frame[0]

            if ftag == FRAME_IF:
                # frame = (FRAME_IF, then_br, else_br, env)
                # V holds the test value; pick a branch.
                # Treat (VAL_INT, 0) as false, everything else as true.
                if V[0] == VAL_INT and V[1] == 0:
                    C = frame[2]
                else:
                    C = frame[1]
                E = frame[3]
                if trace:
                    _trace("APPLY/FRAME_IF", C, V, E, K)
                break

            elif ftag == FRAME_ARG:
                # frame = (FRAME_ARG, arg_expr, env)
                # V is the function value (VAL_CLOSURE, param, bodyExpr, orig_E); now go evaluate the argument.
                # Push a CALL frame carrying the function for later.
                # V is the FRAME_ARG tuple constructed in the EXPR_APP case above
                K.append((FRAME_CALL, V))
                C = frame[1]
                E = frame[2]
                if trace:
                    _trace("APPLY/FRAME_ARG", C, V, E, K)
                break
                # C (arg_expr) gets evaluated in E (env) and its result stored in V.  Then the FRAME_CALL gets popped

            elif ftag == FRAME_CALL:
                # frame = (FRAME_CALL, (VAL_CLOSURE, param, bodyExpr, env))
                # V is the argument value; frame[1] is the closure.
                # Bind the parameter in the closure's captured env and
                # evaluate the body. No frame is pushed here, so a tail
                # call inside the body reuses this K depth - that is
                # where structural TCO comes from.
                closure = frame[1]
                param = closure[1]
                body = closure[2]
                clo_env = closure[3]
                E = env_extend(clo_env, param, V)
                C = body
                if trace:
                    _trace("APPLY/FRAME_CALL", C, V, E, K)
                break

            else:
                raise RuntimeError("unknown frame tag: " + str(ftag))

        # fall through to the outer `while True` - restarts EVAL loop


# -------- Test programs --------
#
# Same six programs as cek_tuples.py; outputs should match exactly.

if __name__ == "__main__":

    # 1. A literal.
    #    42  ->  (VAL_INT, 42)
    print("=== 1. literal 42 ===")
    print(cek_eval(
        (EXPR_INT, 42),
        trace=False
    ))
    print()

    # 2. Identity applied to 7.
    #    ((lambda (x) x) 7)  ->  (VAL_INT, 7)
    print("=== 2. ((lambda (x) x) 7) ===")
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, "x", (EXPR_VAR, "x")),
            (EXPR_INT, 7)),
        trace=False
    ))
    print()

    # 3. Curried constant function.
    #    (((lambda (x) (lambda (y) x)) 3) 9)  ->  (VAL_INT, 3)
    print("=== 3. (((lambda (x) (lambda (y) x)) 3) 9) ===")
    print(cek_eval(
        (EXPR_APP,
            (EXPR_APP,
                (EXPR_LAM, "x",
                    (EXPR_LAM, "y", (EXPR_VAR, "x"))),
                (EXPR_INT, 3)),
            (EXPR_INT, 9)),
        trace=False
    ))
    print()

    # 4. If selects the then branch when test is nonzero.
    #    (if 1 100 200)  ->  (VAL_INT, 100)
    print("=== 4. (if 1 100 200) ===")
    print(cek_eval(
        (EXPR_IF,
            (EXPR_INT, 1),
            (EXPR_INT, 100),
            (EXPR_INT, 200)),
        trace=False
    ))
    print()

    # 5. If selects the else branch when test is zero.
    #    (if 0 100 200)  ->  (VAL_INT, 200)
    print("=== 5. (if 0 100 200) ===")
    print(cek_eval(
        (EXPR_IF,
            (EXPR_INT, 0),
            (EXPR_INT, 100),
            (EXPR_INT, 200)),
        trace=False
    ))
    print()

    # 6. Higher-order: apply an identity function passed as argument.
    #    ((lambda (f) (f 3)) (lambda (x) x))  ->  (VAL_INT, 3)
    print("=== 6. ((lambda (f) (f 3)) (lambda (x) x)) ===")
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, "f",
                (EXPR_APP, (EXPR_VAR, "f"), (EXPR_INT, 3))),
            (EXPR_LAM, "x", (EXPR_VAR, "x"))),
        trace=False
    ))
