# CEK machine - multi-argument variant of cek_tuples.py.
#
# Same tagged-tuple shape and same two-state (EVAL/APPLY) loop.
# The only real change is that lambdas now carry a tuple of param
# names instead of a single name, applications carry a tuple of
# argument expressions, and FRAME_CALL becomes a "baton" frame
# that walks through the argument list one slot at a time.
#
# At any instant during a call's argument evaluation, K holds
# exactly one FRAME_CALL for that call - not one per argument.
# Each time a value arrives, the frame is popped and a new one
# is pushed with that value appended and one fewer expression
# remaining. When `remaining` is empty, the frame beta-reduces
# instead of re-pushing itself.


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
# Identical to cek_tuples.py - multi-arg calls just extend the
# env once per (param, value) pair at beta-reduction time.

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
# Expression forms:
#   (EXPR_INT, n)
#   (EXPR_VAR, name)
#   (EXPR_LAM, params_tuple, body)        params is a tuple of names
#   (EXPR_APP, fn_expr, args_tuple)       args is a tuple of expressions
#   (EXPR_IF,  test, then_br, else_br)
#
# Value forms:
#   (VAL_INT,     n)
#   (VAL_CLOSURE, params_tuple, body, captured_env)
#
# Frame forms:
#   (FRAME_IF,   then_br, else_br, env)
#                       waiting on a test value
#   (FRAME_ARG,  args_tuple, env)
#                       waiting on the function value; args
#                       not yet started
#   (FRAME_CALL, fn_value, collected, remaining, env)
#                       baton frame walking through args;
#                       collected holds already-evaluated arg
#                       values, remaining holds arg expressions
#                       still to evaluate

def cek_eval(expr):
    state = EVAL
    C = expr
    V = None
    E = None
    K = []

    while True:
        if state == EVAL:
            tag = C[0]

            if tag == EXPR_INT:
                V = (VAL_INT, C[1])
                state = APPLY

            elif tag == EXPR_VAR:
                V = env_lookup(E, C[1])
                state = APPLY

            elif tag == EXPR_LAM:
                V = (VAL_CLOSURE, C[1], C[2], E)
                state = APPLY

            elif tag == EXPR_APP:
                # Evaluate fn first; remember all arg exprs for later.
                K.append((FRAME_ARG, C[2], E))
                C = C[1]
                # state stays EVAL

            elif tag == EXPR_IF:
                K.append((FRAME_IF, C[2], C[3], E))
                C = C[1]
                # state stays EVAL

            else:
                raise RuntimeError("unknown expression tag: " + str(tag))

        else:  # state == APPLY
            if not K:
                return V

            frame = K.pop()
            tag = frame[0]

            if tag == FRAME_IF:
                # V holds the test value; pick a branch.
                if V[0] == VAL_INT and V[1] == 0:
                    C = frame[2]
                else:
                    C = frame[1]
                E = frame[3]
                state = EVAL

            elif tag == FRAME_ARG:
                # V is the function value. Walk into the arg list.
                args = frame[1]
                saved_env = frame[2]

                if len(args) == 0:
                    # Zero-arg call: no args to evaluate, beta-reduce now.
                    closure = V
                    params = closure[1]
                    body = closure[2]
                    clo_env = closure[3]
                    if len(params) != 0:
                        raise RuntimeError(
                            "arity mismatch: expected "
                            + str(len(params)) + " args, got 0"
                        )
                    E = clo_env
                    C = body
                    state = EVAL
                else:
                    # Push a CALL frame that will step through args.
                    # collected starts empty; remaining is everything
                    # after the first arg (which becomes the new C).
                    K.append((FRAME_CALL, V, (), args[1:], saved_env))
                    C = args[0]
                    E = saved_env
                    state = EVAL

            elif tag == FRAME_CALL:
                # V is the argument value that just arrived.
                fn_value = frame[1]
                collected = frame[2]
                remaining = frame[3]
                saved_env = frame[4]

                new_collected = collected + (V,)

                if len(remaining) == 0:
                    # All args collected: beta-reduce. Extend the
                    # closure's captured env with one binding per
                    # (param, value) pair, then eval the body. No
                    # frame is pushed here, so tail calls inside the
                    # body still reuse the current K depth.
                    closure = fn_value
                    params = closure[1]
                    body = closure[2]
                    clo_env = closure[3]
                    if len(params) != len(new_collected):
                        raise RuntimeError(
                            "arity mismatch: expected "
                            + str(len(params)) + " args, got "
                            + str(len(new_collected))
                        )
                    new_env = clo_env
                    for name, val in zip(params, new_collected):
                        new_env = env_extend(new_env, name, val)
                    E = new_env
                    C = body
                    state = EVAL
                else:
                    # More args to evaluate: advance the baton.
                    # Pop-and-repush with the new value folded in
                    # and the argument list advanced by one slot.
                    K.append((FRAME_CALL, fn_value, new_collected,
                              remaining[1:], saved_env))
                    C = remaining[0]
                    E = saved_env
                    state = EVAL

            else:
                raise RuntimeError("unknown frame tag: " + str(tag))


# -------- Test programs --------
#
# Expressions are built as raw tagged tuples so the test code
# stays close to the underlying representation. Expected results
# appear in comments next to each call. Output pairs are
# (tag, payload); VAL_INT has tag 0.

if __name__ == "__main__":

    # 1. A literal - sanity check, unchanged from cek_tuples.py.
    #    42  ->  (VAL_INT, 42)
    print(cek_eval(
        (EXPR_INT, 42)
    ))

    # 2. Zero-argument lambda applied with no args.
    #    ((lambda () 5))  ->  (VAL_INT, 5)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, (), (EXPR_INT, 5)),
            ())
    ))

    # 3. Single-arg identity - parity with cek_tuples.py.
    #    ((lambda (x) x) 7)  ->  (VAL_INT, 7)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, ("x",), (EXPR_VAR, "x")),
            ((EXPR_INT, 7),))
    ))

    # 4. Two-arg selecting the first.
    #    ((lambda (x y) x) 11 22)  ->  (VAL_INT, 11)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, ("x", "y"), (EXPR_VAR, "x")),
            ((EXPR_INT, 11), (EXPR_INT, 22)))
    ))

    # 5. Two-arg selecting the second.
    #    ((lambda (x y) y) 11 22)  ->  (VAL_INT, 22)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, ("x", "y"), (EXPR_VAR, "y")),
            ((EXPR_INT, 11), (EXPR_INT, 22)))
    ))

    # 6. Three-arg selecting the middle.
    #    ((lambda (a b c) b) 10 20 30)  ->  (VAL_INT, 20)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, ("a", "b", "c"), (EXPR_VAR, "b")),
            ((EXPR_INT, 10), (EXPR_INT, 20), (EXPR_INT, 30)))
    ))

    # 7. Three-arg where the arguments are themselves computed
    #    by nested IFs. Exercises the baton frame across
    #    subexpressions that each push and pop their own frames.
    #    ((lambda (a b c) a)
    #        (if 1 100 200) (if 0 100 200) 42)  ->  (VAL_INT, 100)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, ("a", "b", "c"), (EXPR_VAR, "a")),
            ((EXPR_IF, (EXPR_INT, 1), (EXPR_INT, 100), (EXPR_INT, 200)),
             (EXPR_IF, (EXPR_INT, 0), (EXPR_INT, 100), (EXPR_INT, 200)),
             (EXPR_INT, 42)))
    ))

    # 8. Higher-order with two args: the outer lambda receives a
    #    function and a value, then applies the function to the
    #    value in its body.
    #    ((lambda (f x) (f x)) (lambda (y) y) 99)  ->  (VAL_INT, 99)
    print(cek_eval(
        (EXPR_APP,
            (EXPR_LAM, ("f", "x"),
                (EXPR_APP, (EXPR_VAR, "f"), ((EXPR_VAR, "x"),))),
            ((EXPR_LAM, ("y",), (EXPR_VAR, "y")),
             (EXPR_INT, 99)))
    ))
