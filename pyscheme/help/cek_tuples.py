# CEK machine in Python, structured as a C port would be.
#
# Uses tagged tuples for expressions, values, and continuation frames.
# Two registers (C, V) with an explicit EVAL/APPLY state flag. This
# mirrors the theoretical CEK presentation where EVAL and APPLY are
# distinct machine states, and maps directly onto the shape the C
# version will take: an enum for tags, a struct with a tag + union
# for each variant, and one main loop that switches on state.


# -------- Tag constants (C enum equivalents) --------

# Machine state
EVAL = 0
APPLY = 1

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
# In C:
#   struct EnvCell { const char* name; Value* value; EnvCell* parent; };

def env_lookup(env, name):
    while env is not None:
        if env[0] == name:
            return env[1]
        env = env[2]
    raise NameError("unbound variable: " + name)


def env_extend(env, name, value):
    return (name, value, env)


# -------- The CEK machine --------
#
# Registers:
#   state : EVAL or APPLY - which half of the loop is active
#   C     : current expression (used when state == EVAL)
#   V     : current value      (used when state == APPLY)
#   E     : current environment (linked list, or None)
#   K     : continuation stack  (Python list used as a stack)
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

def cek_eval(expr):
    state = EVAL
    C = expr
    V = None
    E = None
    K = []

    while True:
        if state == EVAL:
            # state == EVAL means we are descending into a subexpression

            tag = C[0]

            if tag == EXPR_INT:
                intVal = C[1]
                V = (VAL_INT, intVal)
                state = APPLY

            elif tag == EXPR_VAR:
                varName = C[1]
                V = env_lookup(E, varName)
                state = APPLY

            elif tag == EXPR_LAM:
                # Capture current E inside the closure value.
                param, bodyExpr = C[1:]
                V = (VAL_CLOSURE, param, bodyExpr, E)
                state = APPLY

            elif tag == EXPR_APP:
                # Evaluate fn first; remember arg and env for later.
                fnExpr, argExpr = C[1:]
                K.append((FRAME_ARG, argExpr, E))
                C = fnExpr
                # state stays EVAL - stay in EVAL until we can't descend any more.

            elif tag == EXPR_IF:
                # Evaluate test first; remember both branches and env.
                cond, thenPart, elsePart = C[1:]
                K.append((FRAME_IF, thenPart, elsePart, E))
                C = cond
                # state stays EVAL - stay in EVAL until we can't descend any more.

            else:
                raise RuntimeError("unknown expression tag: " + str(tag))

        else:  # state == APPLY
            # state == APPLY means we are ascending out of a subexpression.

            if not K:
                return V

            frame = K.pop()
            tag = frame[0]

            if tag == FRAME_IF:
                # V holds the test value; pick a branch.
                # Treat (VAL_INT, 0) as false, everything else as true.
                if V[0] == VAL_INT and V[1] == 0:
                    C = frame[2]
                else:
                    C = frame[1]
                E = frame[3]
                state = EVAL

            elif tag == FRAME_ARG:
                # V is the function value; now go evaluate the argument.
                # Push a CALL frame carrying the function for later.
                K.append((FRAME_CALL, V))
                C = frame[1]
                E = frame[2]
                state = EVAL

            elif tag == FRAME_CALL:
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
                state = EVAL

            else:
                raise RuntimeError("unknown frame tag: " + str(tag))


# -------- Test programs --------
#
# Expressions are built as raw tagged tuples (no constructor helpers)
# to keep the test code as close to the underlying representation as
# possible. Expected results appear in comments next to each call.
# Output pairs are (tag, payload); VAL_INT has tag 0.

if __name__ == "__main__":

    # 1. A literal.
    #    42  ->  (VAL_INT, 42)
    print(cek_eval(
        (EXPR_INT, 42)
    ))

    # 2. Identity applied to 7.
    #    ((lambda (x) x) 7)  ->  (VAL_INT, 7)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, "x", (EXPR_VAR, "x")),
            (EXPR_INT, 7))
    ))

    # 3. Curried constant function.
    #    (((lambda (x) (lambda (y) x)) 3) 9)  ->  (VAL_INT, 3)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_APP,
                (EXPR_LAM, "x",
                    (EXPR_LAM, "y", (EXPR_VAR, "x"))),
                (EXPR_INT, 3)),
            (EXPR_INT, 9))
    ))

    # 4. If selects the then branch when test is nonzero.
    #    (if 1 100 200)  ->  (VAL_INT, 100)
    print(cek_eval(
        (EXPR_IF,
            (EXPR_INT, 1),
            (EXPR_INT, 100),
            (EXPR_INT, 200))
    ))

    # 5. If selects the else branch when test is zero.
    #    (if 0 100 200)  ->  (VAL_INT, 200)
    print(cek_eval(
        (EXPR_IF,
            (EXPR_INT, 0),
            (EXPR_INT, 100),
            (EXPR_INT, 200))
    ))

    # 6. Higher-order: apply an identity function passed as argument.
    #    ((lambda (f) (f 3)) (lambda (x) x))  ->  (VAL_INT, 3)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, "f",
                (EXPR_APP, (EXPR_VAR, "f"), (EXPR_INT, 3))),
            (EXPR_LAM, "x", (EXPR_VAR, "x")))
    ))
