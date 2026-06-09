# CEK machine compiled to a traditional bytecode VM.
#
# Companion to cek_tuples.py. Same source language, same CEK
# transitions, same value domain - but the AST walk happens at
# compile time and the runtime is a flat-instruction loop with a big
# switch over the opcode. No EVAL/APPLY state flag, no AST tag
# dispatch: every "which transition runs next" choice is baked into
# the emitted instruction stream.
#
# Opcodes and frame kinds are plain ints, so the shape maps straight
# onto a C port: enum + struct-with-tag for instructions and frames,
# switch inside a single main loop.


# -------- Expression kinds (input AST - same as cek_tuples) --------

EXPR_INT = 0
EXPR_VAR = 1
EXPR_LAM = 2
EXPR_APP = 3
EXPR_IF = 4


# -------- Value kinds --------

VAL_INT = 0
VAL_CLOSURE = 1


# -------- Frame kinds --------
#
# FRAME_IF, FRAME_ARG, FRAME_CALL are the three CEK frames. FRAME_RET
# is a bytecode-VM addition: it records the pc the VM should return
# to when a non-tail call's body finishes. In cek_tuples that role is
# played implicitly by the AST shape surrounding the call - the
# parent expression is right there to pick up with. A flat
# instruction stream has no parent tree to walk back to, so the
# return address has to live somewhere explicit, and K is the
# natural place. OP_TCALL does not push a FRAME_RET, so K depth
# stays flat across tail calls and TCO remains structural.

FRAME_IF = 0
FRAME_ARG = 1
FRAME_CALL = 2
FRAME_RET = 3


# -------- Opcodes --------
#
# Each opcode corresponds to one CEK transition (or a bytecode
# bookkeeping operation like OP_JUMP).

OP_INT = 0   # V = (VAL_INT, n)
OP_VAR = 1   # V = env_lookup(E, name)
OP_LAM = 2   # V = (VAL_CLOSURE, param, body_pc, E)
OP_JUMP = 3   # pc = target
OP_APP_START = 4   # K.push((FRAME_ARG, E))
OP_APPLY_ARG = 5   # pop FRAME_ARG, restore E, K.push((FRAME_CALL, V))
# non-tail call: pop FRAME_CALL, push FRAME_RET(pc+1), jump to body_pc
OP_CALL = 6
OP_TCALL = 7   # tail call:     pop FRAME_CALL,                       jump to body_pc
OP_IF_START = 8   # K.push((FRAME_IF, then_pc, else_pc, E))
OP_APPLY_IF = 9   # pop FRAME_IF, restore E, pc = then_pc or else_pc
OP_RET = 10  # pop FRAME_RET (or halt the VM if K is empty)


# -------- Environment --------
#
# Linked list of (name, value, parent) cells. Empty env is None.
# Identical to cek_tuples.

def env_lookup(env, name):
    while env is not None:
        if env[0] == name:
            return env[1]
        env = env[2]
    raise NameError("unbound variable: " + name)


def env_extend(env, name, value):
    return (name, value, env)


# -------- Compiler --------
#
# Walks an (EXPR_*) AST and emits a flat list of instructions. Each
# AST node is translated into a fixed opcode sequence.
#
# `tail` tracks whether the expression being compiled is in tail
# position: a leaf/closure emission in tail position is followed by
# OP_RET, and a call in tail position becomes OP_TCALL instead of
# OP_CALL. `tail` propagates through lambda bodies and through both
# IF branches; it stops at APP sub-expressions (fn and arg are
# never in tail position because their values feed the enclosing
# call).
#
# Function bodies are laid out inline right after the OP_LAM that
# creates their closure. An OP_JUMP is emitted just before the body
# so the code path that builds the closure skips over the body's
# instructions; only a later OP_CALL/OP_TCALL jumps into them.
# Backpatching fills in pc-dependent operands (body_pc, past_body,
# then_pc, else_pc, OP_JUMP targets) once the corresponding code
# region's length is known.

def compile_expr(expr, out, tail):
    tag = expr[0]

    if tag == EXPR_INT:
        out.append((OP_INT, expr[1]))
        if tail:
            out.append((OP_RET,))

    elif tag == EXPR_VAR:
        out.append((OP_VAR, expr[1]))
        if tail:
            out.append((OP_RET,))

    elif tag == EXPR_LAM:
        # Reserve slots for OP_LAM and its skip-over OP_JUMP, compile
        # the body inline, then patch body_pc and past_body.
        lam_idx = len(out)
        out.append(None)
        jump_idx = len(out)
        out.append(None)

        body_pc = len(out)
        # body is always in tail position
        compile_expr(expr[2], out, tail=True)
        past_body = len(out)

        out[lam_idx] = (OP_LAM, expr[1], body_pc)
        out[jump_idx] = (OP_JUMP, past_body)

        if tail:
            out.append((OP_RET,))

    elif tag == EXPR_APP:
        out.append((OP_APP_START,))
        compile_expr(expr[1], out, tail=False)  # fn - never in tail position
        out.append((OP_APPLY_ARG,))
        compile_expr(expr[2], out, tail=False)  # arg - never in tail position
        if tail:
            out.append((OP_TCALL,))
        else:
            out.append((OP_CALL,))

    elif tag == EXPR_IF:
        # Reserve slot for OP_IF_START; fill in then_pc/else_pc below.
        if_idx = len(out)
        out.append(None)

        compile_expr(expr[1], out, tail=False)  # cond
        out.append((OP_APPLY_IF,))

        then_pc = len(out)
        compile_expr(expr[2], out, tail=tail)   # then inherits tail context
        if not tail:
            # Non-tail then must jump past the else branch. In the
            # tail case the then branch ended with OP_RET and control
            # will not fall through, so no skip is needed.
            then_jump_idx = len(out)
            out.append(None)

        else_pc = len(out)
        compile_expr(expr[3], out, tail=tail)   # else inherits tail context

        if not tail:
            end_pc = len(out)
            out[then_jump_idx] = (OP_JUMP, end_pc)

        out[if_idx] = (OP_IF_START, then_pc, else_pc)

    else:
        raise RuntimeError("unknown expression tag: " + str(tag))


def compile_program(expr):
    out = []
    compile_expr(expr, out, tail=True)
    return out


# -------- The VM loop --------
#
# Registers:
#   pc : program counter (index into prog)
#   V  : current value
#   E  : current environment (linked list, or None)
#   K  : continuation stack (Python list used as a stack)
#
# One big if/elif over the opcode tag. Each branch inlines the CEK
# transition for that opcode. The only runtime dispatch is on the
# integer opcode - no AST tag check, no state flag.

def run_vm(prog):
    pc = 0
    V = None
    E = None
    K = []

    while True:
        instr = prog[pc]
        op = instr[0]

        if op == OP_INT:
            # EVAL_INT
            V = (VAL_INT, instr[1])
            pc += 1

        elif op == OP_VAR:
            # EVAL_VAR
            V = env_lookup(E, instr[1])
            pc += 1

        elif op == OP_LAM:
            # EVAL_LAM - capture current E inside the closure value.
            V = (VAL_CLOSURE, instr[1], instr[2], E)
            pc += 1

        elif op == OP_JUMP:
            pc = instr[1]

        elif op == OP_APP_START:
            # EVAL_APP preamble: save env so the arg sub-expression
            # can be evaluated in the caller's env once fn is done.
            K.append((FRAME_ARG, E))
            pc += 1

        elif op == OP_APPLY_ARG:
            # APPLY_ARG: pop FRAME_ARG (restoring env), push
            # FRAME_CALL carrying the function value we just produced.
            frame = K.pop()
            E = frame[1]
            K.append((FRAME_CALL, V))
            pc += 1

        elif op == OP_CALL:
            # APPLY_CALL in non-tail position: bind parameter, record
            # where to resume in the caller, jump to body.
            frame = K.pop()          # (FRAME_CALL, closure)
            closure = frame[1]
            K.append((FRAME_RET, pc + 1))
            E = env_extend(closure[3], closure[1], V)
            pc = closure[2]          # body_pc

        elif op == OP_TCALL:
            # APPLY_CALL in tail position: bind parameter, jump to
            # body. No FRAME_RET pushed, so K stays flat across tail
            # calls - structural TCO, same as cek_tuples.
            frame = K.pop()          # (FRAME_CALL, closure)
            closure = frame[1]
            E = env_extend(closure[3], closure[1], V)
            pc = closure[2]          # body_pc

        elif op == OP_IF_START:
            # EVAL_IF preamble: remember both branch pcs and env.
            K.append((FRAME_IF, instr[1], instr[2], E))
            pc += 1

        elif op == OP_APPLY_IF:
            # APPLY_IF: V holds the test value; pick a branch.
            # Treat (VAL_INT, 0) as false, everything else as true.
            frame = K.pop()
            E = frame[3]
            if V[0] == VAL_INT and V[1] == 0:
                pc = frame[2]
            else:
                pc = frame[1]

        elif op == OP_RET:
            # End of a body. If K is empty we are at the top level -
            # return V as the program's final value. Otherwise pop
            # the FRAME_RET that the matching OP_CALL pushed and
            # resume the caller.
            if not K:
                return V
            frame = K.pop()          # (FRAME_RET, return_pc)
            pc = frame[1]

        else:
            raise RuntimeError("unknown opcode: " + str(op))


def cek_eval(expr):
    return run_vm(compile_program(expr))


# -------- Test programs --------
#
# Same six programs as cek_tuples.py; outputs should match exactly.

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
