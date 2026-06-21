"""Meta-operations: apply, eval, force, make-promise, environment.

R7RS library procedures that transcend normal procedure-call semantics:

    (apply <proc> <arg>... <list>)      6.10  apply proc to combined args
    (eval <datum> [<env-spec>])         6.12  evaluate a datum in an environment
    (environment <list>...)             6.12  build a frozen env from libraries
    (interaction-environment)           6.12  return the mutable global env

Record plumbing for define-record-type (R7RS 5.5).  These %-prefixed
primitives are emitted by the Expander; users do not call them directly:

    (%make-record-type name field-names)    build a RecordType descriptor
    (%make-record record-type field-values) build a record from field list
    (%record-of-type? obj record-type)      predicate for one record type
    (%record-ref record record-type index)  type-checked field read
    (%record-set! record record-type idx v) type-checked field mutate
"""

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
    is_cons, is_nil, is_string, is_integer, is_primitive, is_closure, is_case_closure,
    is_multi_values, is_symbol, is_record, is_record_type,
    is_parameter, is_error_object,
    as_string, as_integer, as_primitive_fn, as_symbol,
    as_promise_is_done, as_promise_payload, promise_resolve, promise_become,
    as_multi_values_list, as_record_type, as_record_fields,
    as_record_type_name,
    as_parameter_value, as_parameter_converter, set_parameter_value,
    as_error_object_message, as_error_object_irritants,
    is_file_error_object, is_read_error_object,
    alloc_cons, make_symbol, symbol_name,
    make_record_type, make_record, make_parameter, make_string,
    make_environment, make_record_accessor, make_record_mutator,
    make_boolean, make_real, list_from_items, src_of,
    VOID_VALUE,
)
from pyscheme.Environment import SchemeTypeError, SchemeUserError, SchemeRaised


CATEGORY = 'meta'


def _prim_syntax_expand(ctx, env, args, app_node):
    from pyscheme.Expander import expand
    return expand(args[0])


def _prim_file_error_p(ctx, env, args, app_node):
    from pyscheme.AST import make_boolean, is_error_object
    obj = args[0]
    return make_boolean(is_error_object(obj) and is_file_error_object(obj))


def _prim_read_error_p(ctx, env, args, app_node):
    from pyscheme.AST import make_boolean, is_error_object
    obj = args[0]
    return make_boolean(is_error_object(obj) and is_read_error_object(obj))


def _prim_error_object_message(ctx, env, args, app_node):
    obj = args[0]
    if not is_error_object(obj):
        raise SchemeTypeError(
            'error-object-message: not an error object', app_node)
    return make_string(as_error_object_message(obj))


def _prim_error_object_irritants(ctx, env, args, app_node):
    obj = args[0]
    if not is_error_object(obj):
        raise SchemeTypeError(
            'error-object-irritants: not an error object', app_node)
    return list_from_items(list(as_error_object_irritants(obj)))


def _prim_apply_unreached(ctx, env, args, app_node):
    # The Evaluator intercepts apply at FRAME_CALL dispatch to tail-call
    # the target procedure through the normal CEK path.  This body only
    # fires if the interception was bypassed (e.g., apply substituted
    # via macro hygiene into a position other than operator position,
    # which isn't well-defined).
    raise SchemeTypeError(
        'apply: cannot be called through a re-entering path in this '
        'implementation', app_node)


def _prim_eval_unreached(ctx, env, args, app_node):
    # The Evaluator intercepts eval at FRAME_CALL dispatch so the expanded
    # datum runs in the same cek_eval call (preserving outer TCO).  This
    # body fires only if the interception was bypassed.
    raise SchemeTypeError(
        'eval: cannot be called through a re-entering path in this '
        'implementation', app_node)


def _prim_interaction_environment(ctx, env, args, app_node):
    # Returns the mutable global environment as a first-class env value.
    # eval against this is the same as eval with no env arg: definitions
    # persist on the top-level env.
    return make_environment(env.getGlobalEnv())


def _prim_environment(ctx, env, args, app_node):
    # (environment <library-spec>...) returns a frozen env containing the
    # union of the named libraries' exports.  Each spec is a list of
    # symbols / integers (e.g. '(scheme base)).  R7RS forbids modifying
    # the resulting env; we enforce that via Environment.freeze().
    from pyscheme.Environment import Environment
    from pyscheme.library import library_name_to_key, library_lookup
    from pyscheme.PrettyPrinter import pretty_print
    result = Environment(parent=None)
    i = 0
    while i < len(args):
        spec = args[i]
        if not is_cons(spec):
            raise SchemeTypeError(
                'environment: argument must be a library-name list',
                app_node)
        try:
            key = library_name_to_key(spec)
        except ValueError as e:
            raise SchemeTypeError('environment: ' + str(e), app_node)
        lib_env = library_lookup(key)
        if lib_env is None:
            raise SchemeTypeError(
                'environment: library not found: ' + pretty_print(spec),
                app_node)
        for sid, val in lib_env._bindings.items():
            result.bind(symbol_name(sid), val)
        i = i + 1
    result.freeze()
    return make_environment(result)


def _prim_make_environment(ctx, env, args, app_node):
    # (make-environment <library-spec>...) -- the MUTABLE sibling of R7RS
    # `environment`.  Returns a fresh top-level env that allows defines/mutations
    # and isolates them.  With NO args it is a child of the global (REPL) env, so
    # all default bindings are visible while new top-level defines stay local --
    # the isolation neither `environment` (frozen) nor `interaction-environment`
    # (the single shared global) can give.  With library-specs it holds the union
    # of their exports (like `environment`, but not frozen).  Used with `eval` to
    # run a program in a clean sandbox (e.g. one ecraven benchmark per fresh env).
    from pyscheme.Environment import Environment
    if len(args) == 0:
        return make_environment(Environment(parent=env.getGlobalEnv()))
    from pyscheme.library import library_name_to_key, library_lookup
    from pyscheme.PrettyPrinter import pretty_print
    result = Environment(parent=None)
    i = 0
    while i < len(args):
        spec = args[i]
        if not is_cons(spec):
            raise SchemeTypeError(
                'make-environment: argument must be a library-name list',
                app_node)
        try:
            key = library_name_to_key(spec)
        except ValueError as e:
            raise SchemeTypeError('make-environment: ' + str(e), app_node)
        lib_env = library_lookup(key)
        if lib_env is None:
            raise SchemeTypeError(
                'make-environment: library not found: ' + pretty_print(spec),
                app_node)
        for sid, val in lib_env._bindings.items():
            result.bind(symbol_name(sid), val)
        i = i + 1
    return make_environment(result)  # deliberately NOT frozen -> mutable


def _prim_directory_files(ctx, env, args, app_node):
    # (directory-files path) -> sorted list of the bare entry names in PATH,
    # excluding "." and ".." (os.listdir already does; dotfiles kept).  Models
    # SRFI 170 directory-files; mirrors cppScheme2.
    import os
    if not is_string(args[0]):
        raise SchemeTypeError(
            'directory-files: argument must be a string', app_node)
    try:
        names = sorted(os.listdir(as_string(args[0])))
    except OSError as e:
        raise SchemeTypeError('directory-files: ' + str(e), app_node)
    return list_from_items([make_string(n) for n in names])


def _prim_interpreter_argv(ctx, env, args, app_node):
    # (interpreter-argv) -> the argv list (of strings) that relaunches THIS
    # interpreter, for spawning self / sibling interpreters via run-process.
    # pyScheme launches as `python -m pyscheme`; a LIST keeps parity with the
    # cppScheme2 primitive (whose list is a single exe path).  Mirrors cppScheme2.
    import sys
    return list_from_items([make_string(sys.executable),
                            make_string('-m'), make_string('pyscheme')])


def _prim_run_process(ctx, env, args, app_node):
    # (run-process argv [stdin-string]) -> (values exit-code stdout stderr).
    # argv is a non-empty list of strings (argv[0] = program, searched on PATH;
    # direct exec, NO shell -> arguments verbatim and injection-safe).  Blocks until
    # the child exits.  Optional 2nd arg = a string written to the child's stdin then
    # EOF (#f or omitted = empty stdin).  Mirrors the cppScheme2 primitive (lockstep).
    import subprocess
    from pyscheme.AST import car, cdr, make_integer, make_multi_values, \
        is_boolean, as_boolean
    argv = []
    cur = args[0]
    while is_cons(cur):
        head = car(cur)
        if not is_string(head):
            raise SchemeTypeError(
                'run-process: argv elements must be strings', app_node)
        argv.append(as_string(head))
        cur = cdr(cur)
    if len(argv) == 0:
        raise SchemeTypeError(
            'run-process: first argument must be a non-empty list of strings',
            app_node)
    stdin_data = None
    if len(args) >= 2 and not (is_boolean(args[1]) and as_boolean(args[1]) is False):
        if not is_string(args[1]):
            raise SchemeTypeError(
                'run-process: stdin argument must be a string or #f', app_node)
        stdin_data = as_string(args[1]).encode('utf-8')
    try:
        # shell=False (default) -> direct exec, no /bin/sh or cmd.exe.
        proc = subprocess.run(argv, input=stdin_data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, ValueError) as e:
        raise SchemeTypeError('run-process: ' + str(e), app_node)
    out = proc.stdout.decode('utf-8', errors='replace')
    err = proc.stderr.decode('utf-8', errors='replace')
    # proc.returncode is already a negated signal number on POSIX signal-kill.
    return make_multi_values([make_integer(proc.returncode),
                              make_string(out), make_string(err)])


def _prim_make_record_type(ctx, env, args, app_node):
    # (%make-record-type 'name '(field-name ...))
    name_arg = args[0]
    fields_arg = args[1]
    if not is_symbol(name_arg):
        raise SchemeTypeError(
            '%make-record-type: first argument must be a symbol', app_node)
    field_names = []
    cur = fields_arg
    while is_cons(cur):
        f = cur.car
        if not is_symbol(f):
            raise SchemeTypeError(
                '%make-record-type: field names must be symbols', app_node)
        field_names.append(as_symbol(f))
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            '%make-record-type: field names must be a proper list', app_node)
    return make_record_type(as_symbol(name_arg), field_names)


def _prim_make_record(ctx, env, args, app_node):
    # (%make-record record-type (field-value ...))
    rt = args[0]
    field_list = args[1]
    if not is_record_type(rt):
        raise SchemeTypeError(
            '%make-record: first argument must be a record type', app_node)
    values = []
    cur = field_list
    while is_cons(cur):
        values.append(cur.car)
        cur = cur.cdr
    if not is_nil(cur):
        raise SchemeTypeError(
            '%make-record: field values must be a proper list', app_node)
    return make_record(rt, values)


def _prim_record_of_type_p(ctx, env, args, app_node):
    # (%record-of-type? obj record-type)
    from pyscheme.AST import make_boolean
    obj = args[0]
    rt = args[1]
    if not is_record_type(rt):
        raise SchemeTypeError(
            '%record-of-type?: second argument must be a record type', app_node)
    if not is_record(obj):
        return make_boolean(False)
    return make_boolean(as_record_type(obj) is rt)


def _prim_record_ref(ctx, env, args, app_node):
    # (%record-ref record record-type index)
    from pyscheme.AST import is_integer, as_integer
    rec = args[0]
    rt = args[1]
    idx_val = args[2]
    if not is_record_type(rt):
        raise SchemeTypeError(
            '%record-ref: second argument must be a record type', app_node)
    if not is_record(rec) or as_record_type(rec) is not rt:
        raise SchemeTypeError(
            '%record-ref: record is not of the expected type ' +
            as_record_type_name(rt), app_node)
    if not is_integer(idx_val):
        raise SchemeTypeError(
            '%record-ref: index must be an integer', app_node)
    idx = as_integer(idx_val)
    return as_record_fields(rec)[idx]


def _prim_make_record_accessor(ctx, env, args, app_node):
    # (%make-record-accessor record-type index name)
    from pyscheme.AST import is_integer, as_integer
    rt = args[0]
    idx_val = args[1]
    name_arg = args[2]
    if not is_record_type(rt):
        raise SchemeTypeError(
            '%make-record-accessor: first argument must be a record type',
            app_node)
    if not is_integer(idx_val):
        raise SchemeTypeError(
            '%make-record-accessor: index must be an integer', app_node)
    if not is_symbol(name_arg):
        raise SchemeTypeError(
            '%make-record-accessor: name must be a symbol', app_node)
    return make_record_accessor(rt, as_integer(idx_val), as_symbol(name_arg))


def _prim_make_record_mutator(ctx, env, args, app_node):
    # (%make-record-mutator record-type index name)
    from pyscheme.AST import is_integer, as_integer
    rt = args[0]
    idx_val = args[1]
    name_arg = args[2]
    if not is_record_type(rt):
        raise SchemeTypeError(
            '%make-record-mutator: first argument must be a record type',
            app_node)
    if not is_integer(idx_val):
        raise SchemeTypeError(
            '%make-record-mutator: index must be an integer', app_node)
    if not is_symbol(name_arg):
        raise SchemeTypeError(
            '%make-record-mutator: name must be a symbol', app_node)
    return make_record_mutator(rt, as_integer(idx_val), as_symbol(name_arg))


def _prim_record_set(ctx, env, args, app_node):
    # (%record-set! record record-type index value)
    from pyscheme.AST import is_integer, as_integer
    rec = args[0]
    rt = args[1]
    idx_val = args[2]
    new_val = args[3]
    if not is_record_type(rt):
        raise SchemeTypeError(
            '%record-set!: second argument must be a record type', app_node)
    if not is_record(rec) or as_record_type(rec) is not rt:
        raise SchemeTypeError(
            '%record-set!: record is not of the expected type ' +
            as_record_type_name(rt), app_node)
    if not is_integer(idx_val):
        raise SchemeTypeError(
            '%record-set!: index must be an integer', app_node)
    idx = as_integer(idx_val)
    as_record_fields(rec)[idx] = new_val
    return VOID_VALUE


def _apply_scheme_proc(proc, arg_values, ctx, env, app_node):
    """Invoke any callable Scheme value with a Python arg list, returning
    the value.  Handles primitives, closures, case-closures; also applies
    the body via cek_eval for closure targets.  Shared helper for
    call-with-values, %with-parameters, and make-parameter's converter."""
    from pyscheme.Evaluator import cek_eval, _apply_value
    if is_primitive(proc):
        return as_primitive_fn(proc)(ctx, env, arg_values, app_node)
    if is_closure(proc) or is_case_closure(proc):
        r = _apply_value(proc, arg_values, app_node)
        body = r.body
        if is_cons(body) and is_nil(body.cdr):
            return cek_eval(body.car, r.new_env, ctx)
        begin_sym = make_symbol('begin', None)
        begin_form = alloc_cons(begin_sym, body, None)
        return cek_eval(begin_form, r.new_env, ctx)
    # Record accessors / mutators are first-class procedures too (R7RS 5.5), so
    # map / for-each / apply / call-with-values must apply them, matching the
    # eval loop's application dispatch.
    from pyscheme.AST import (
        is_record_accessor, is_record_mutator, as_record_type,
        as_record_type_name, as_record_fields,
        as_record_accessor_type, as_record_accessor_index, as_record_accessor_name,
        as_record_mutator_type, as_record_mutator_index, as_record_mutator_name,
    )
    from pyscheme.Environment import SchemeArityError, arity_mismatch_msg
    if is_record_accessor(proc):
        if len(arg_values) != 1:
            raise SchemeArityError(
                arity_mismatch_msg(as_record_accessor_name(proc), 1, 1, len(arg_values)), app_node)
        rt = as_record_accessor_type(proc)
        rec = arg_values[0]
        if not is_record(rec) or as_record_type(rec) is not rt:
            raise SchemeTypeError(
                as_record_accessor_name(proc) + ': argument is not a ' + as_record_type_name(rt), app_node)
        return as_record_fields(rec)[as_record_accessor_index(proc)]
    if is_record_mutator(proc):
        if len(arg_values) != 2:
            raise SchemeArityError(
                arity_mismatch_msg(as_record_mutator_name(proc), 2, 2, len(arg_values)), app_node)
        rt = as_record_mutator_type(proc)
        rec = arg_values[0]
        if not is_record(rec) or as_record_type(rec) is not rt:
            raise SchemeTypeError(
                as_record_mutator_name(proc) + ': first argument is not a ' + as_record_type_name(rt), app_node)
        as_record_fields(rec)[as_record_mutator_index(proc)] = arg_values[1]
        return VOID_VALUE
    raise SchemeTypeError('expected a procedure', app_node)


def _prim_make_parameter_unreached(ctx, env, args, app_node):
    # The Evaluator intercepts make-parameter at FRAME_CALL dispatch so
    # the converter is tail-called through the CEK path, driven by
    # FRAME_MAKE_PARAMETER.  This body fires only if interception was
    # bypassed.
    raise SchemeTypeError(
        'make-parameter: cannot be called through a re-entering path '
        'in this implementation', app_node)


def _prim_with_parameters_unreached(ctx, env, args, app_node):
    # The Evaluator intercepts %with-parameters at FRAME_CALL dispatch to
    # run the thunk in tail position and integrate with the wind stack for
    # continuation / exception handling.  This body fires only if the
    # interception was bypassed.
    raise SchemeTypeError(
        '%with-parameters: cannot be called through a re-entering path '
        'in this implementation', app_node)


def _prim_continuation_depth_unreached(ctx, env, args, app_node):
    # The Evaluator intercepts %continuation-depth at application dispatch to
    # read the live continuation-stack (K) length, which a normal primitive
    # body cannot see.  This fires only if the interception was bypassed.
    raise SchemeTypeError(
        '%continuation-depth: cannot be called through a re-entering path '
        'in this implementation', app_node)


def _prim_null_environment(ctx, env, args, app_node):
    from pyscheme.Environment import Environment
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'null-environment: version must be an integer', src_of(app_node))
    # R7RS 6.12: version 5 (R5RS) must be supported; if version is neither 5
    # nor another value supported by the implementation, an error is signaled.
    if as_integer(args[0]) != 5:
        raise SchemeTypeError(
            'null-environment: unsupported version (only 5 is supported)',
            src_of(app_node))
    e = Environment(parent=None)
    e.freeze()
    return make_environment(e)


def _prim_scheme_report_environment(ctx, env, args, app_node):
    if not is_integer(args[0]):
        raise SchemeTypeError(
            'scheme-report-environment: version must be an integer', src_of(app_node))
    # R7RS 6.12: only version 5 (R5RS) is required; signal an error otherwise.
    if as_integer(args[0]) != 5:
        raise SchemeTypeError(
            'scheme-report-environment: unsupported version (only 5 is supported)',
            src_of(app_node))
    return make_environment(env.getGlobalEnv())


def load_setup(args, env, app_node):
    """Validate (load filename [environment]) and read + parse the file,
    returning (forms, eval_env).  The evaluator intercepts `load` and drives the
    forms on the main K stack via FRAME_EVAL_FORMS instead of a re-entrant
    cek_eval per form, so a continuation captured outside the load no longer
    crosses a nested evaluator activation.  Mirrors _prim_load's setup."""
    import os as _os
    from pyscheme.AST import is_environment, as_environment
    from pyscheme.Environment import SchemeArityError, arity_mismatch_msg
    if len(args) < 1 or len(args) > 2:
        raise SchemeArityError(
            arity_mismatch_msg('load', 1, 2, len(args)), src_of(app_node))
    if not is_string(args[0]):
        raise SchemeTypeError(
            'load: filename must be a string', src_of(app_node))
    if len(args) >= 2:
        if not is_environment(args[1]):
            raise SchemeTypeError(
                'load: second argument must be an environment specifier',
                src_of(app_node))
        eval_env = as_environment(args[1])
    else:
        eval_env = env
    path = as_string(args[0])
    abs_path = _os.path.abspath(path)
    try:
        f = open(abs_path, 'r', encoding='utf-8')
        source = f.read()
        f.close()
    except OSError as e:
        raise SchemeTypeError('load: ' + str(e), src_of(app_node))
    from pyscheme.Parser import parse
    return (parse(source, abs_path), eval_env)


def _prim_load(ctx, env, args, app_node):
    import os as _os
    from pyscheme.AST import VOID_VALUE, is_environment, as_environment
    if not is_string(args[0]):
        raise SchemeTypeError(
            'load: filename must be a string', src_of(app_node))
    # R7RS 6.14: (load filename [environment-specifier]).  If the optional
    # environment is supplied, evaluate the file's forms in it; otherwise use
    # the current (interaction) environment.
    if len(args) >= 2:
        if not is_environment(args[1]):
            raise SchemeTypeError(
                'load: second argument must be an environment specifier',
                src_of(app_node))
        eval_env = as_environment(args[1])
    else:
        eval_env = env
    path = as_string(args[0])
    abs_path = _os.path.abspath(path)
    try:
        f = open(abs_path, 'r', encoding='utf-8')
        source = f.read()
        f.close()
    except OSError as e:
        raise SchemeTypeError('load: ' + str(e), src_of(app_node))
    from pyscheme.Parser import parse
    from pyscheme.Expander import expand
    from pyscheme.Analyzer import analyze, extend_static_env_with_define
    from pyscheme.Evaluator import cek_eval
    forms = parse(source, abs_path)
    static_env = {}
    i = 0
    while i < len(forms):
        form = forms[i]
        expanded = expand(form)
        analyze(expanded, static_env)
        extend_static_env_with_define(static_env, expanded)
        cek_eval(expanded, eval_env, ctx)
        i = i + 1
    return VOID_VALUE


def _prim_command_line(ctx, env, args, app_node):
    import sys as _sys
    from pyscheme.AST import alloc_cons, NIL_VALUE
    result = NIL_VALUE
    i = len(_sys.argv) - 1
    while i >= 0:
        result = alloc_cons(make_string(_sys.argv[i]), result)
        i = i - 1
    return result


def _prim_exit(ctx, env, args, app_node):
    import sys as _sys
    from pyscheme.AST import is_boolean, as_boolean, is_integer, as_integer
    from pyscheme.Environment import ReplExit
    if len(args) == 0:
        code = 0
    else:
        obj = args[0]
        if is_boolean(obj):
            code = 0 if as_boolean(obj) is True else 1
        elif is_integer(obj):
            code = as_integer(obj)
        else:
            code = 1
    # In a live REPL session (exit) aborts the current evaluation and returns
    # to the '>>> ' prompt rather than terminating the process; batch file
    # execution still exits the process.  See Context.interactive.
    if getattr(ctx, 'interactive', False):
        raise ReplExit(code)
    _sys.exit(code)


def _prim_emergency_exit(ctx, env, args, app_node):
    import os as _os
    from pyscheme.AST import is_boolean, as_boolean, is_integer, as_integer
    if len(args) == 0:
        _os._exit(0)
    obj = args[0]
    if is_boolean(obj):
        _os._exit(0 if as_boolean(obj) is True else 1)
    if is_integer(obj):
        _os._exit(as_integer(obj))
    _os._exit(1)


def _prim_get_environment_variable(ctx, env, args, app_node):
    import os as _os
    if not is_string(args[0]):
        raise SchemeTypeError(
            'get-environment-variable: argument must be a string',
            src_of(app_node))
    val = _os.environ.get(as_string(args[0]))
    if val is None:
        return make_boolean(False)
    return make_string(val)


def _prim_get_environment_variables(ctx, env, args, app_node):
    import os as _os
    from pyscheme.AST import alloc_cons, NIL_VALUE
    items = list(_os.environ.items())
    result = NIL_VALUE
    i = len(items) - 1
    while i >= 0:
        k = items[i][0]
        v = items[i][1]
        pair = alloc_cons(make_string(k), make_string(v))
        result = alloc_cons(pair, result)
        i = i - 1
    return result


def _prim_unicode_version(ctx, env, args, app_node):
    import unicodedata as _ud
    return make_string(_ud.unidata_version)


def _prim_runtime(ctx, env, args, app_node):
    import time as _time
    return make_real(_time.process_time())


def _prim_current_second(ctx, env, args, app_node):
    import time as _time
    return make_real(_time.time())


def _prim_current_jiffy(ctx, env, args, app_node):
    import time as _time
    from pyscheme.AST import make_integer
    return make_integer(int(_time.monotonic() * 1000))


def _prim_jiffies_per_second(ctx, env, args, app_node):
    from pyscheme.AST import make_integer
    return make_integer(1000)


def register():
    register_primitive('apply', (2, None), _prim_apply_unreached,
                       usage='(apply <proc> <arg>... <list>)',
                       doc=(
        "Call <proc> with the elements of <list> as its arguments, optionally\n"
        "prepended by any leading <arg>s.  The last argument must be a\n"
        "proper list.  Equivalent to (proc arg1 ... arg_m elem1 ... elem_n)\n"
        "when <list> has elements elem1..elem_n."),
        category=CATEGORY)

    register_primitive('eval', (1, 2), _prim_eval_unreached,
                       usage='(eval <datum> [<env-spec>])',
                       doc=(
        "Evaluate <datum> as a Scheme expression.  With one argument, the\n"
        "evaluation environment is the caller's global environment.  With\n"
        "two arguments, <env-spec> must be an environment value produced by\n"
        "(interaction-environment) or (environment ...).  Definitions made\n"
        "during evaluation extend that environment if it is mutable, and\n"
        "are an error if it is frozen.  R7RS 6.12."),
        category=CATEGORY)

    register_primitive('interaction-environment', (0, 0),
                       _prim_interaction_environment,
                       usage='(interaction-environment)',
                       doc=(
        "Return a specifier for the current REPL / top-level environment.\n"
        "The returned environment is mutable: (eval '(define x 1) (interaction-\n"
        "environment)) installs x in the global env so subsequent code can\n"
        "see it.  R7RS 6.12; library (scheme repl)."),
        category=CATEGORY)

    register_primitive('environment', (0, None), _prim_environment,
                       usage='(environment <library-spec>...)',
                       doc=(
        "Return a specifier for an environment built from the named\n"
        "libraries' exports.  Each <library-spec> is a list of symbols /\n"
        "integers identifying a registered library, e.g. '(scheme base).\n"
        "The returned environment is frozen: defining or set!'ing on it is\n"
        "an error.  R7RS 6.12; library (scheme eval)."),
        category=CATEGORY)

    register_primitive('make-environment', (0, None), _prim_make_environment,
                       usage='(make-environment <library-spec>...)',
                       doc=(
        "Return a fresh MUTABLE top-level environment (the mutable sibling of\n"
        "R7RS `environment`).  With no args it is a child of the global REPL\n"
        "environment: all default bindings are visible, but new top-level\n"
        "defines are isolated to it.  With library-specs it holds the union of\n"
        "their exports, not frozen.  Use with `eval` to run a program in\n"
        "isolation.  cppScheme2/pyScheme extension."),
        category=CATEGORY)

    register_primitive('directory-files', (1, 1), _prim_directory_files,
                       usage='(directory-files path)',
                       doc=(
        "Return a sorted list of the bare filenames in directory PATH, excluding\n"
        "\".\" and \"..\" (dotfiles kept).  Names are strings, not full paths --\n"
        "join with \"/\" to build a path.  Errors if PATH cannot be opened.\n"
        "Models SRFI 170 directory-files.  cppScheme2/pyScheme extension."),
        category=CATEGORY)

    register_primitive('interpreter-argv', (0, 0), _prim_interpreter_argv,
                       usage='(interpreter-argv)',
                       doc=(
        "Return the argv list (of strings) that relaunches THIS interpreter, for\n"
        "spawning self / sibling interpreters via run-process.  pyScheme:\n"
        "(python -m pyscheme); cppScheme2: a one-element list (the exe path).\n"
        "cppScheme2/pyScheme extension."),
        category=CATEGORY)

    register_primitive('run-process', (1, 2), _prim_run_process,
                       usage='(run-process argv [stdin-string])',
                       doc=(
        "Run argv (a non-empty list of strings; argv[0] searched on PATH) as a\n"
        "child process with NO shell, blocking until it exits.  Optional 2nd arg\n"
        "= a string written to the child's stdin.  Returns THREE values:\n"
        "exit-code (a negated signal number on POSIX signal-kill), captured\n"
        "stdout string, captured stderr string.  cppScheme2/pyScheme extension."),
        category=CATEGORY)

    # Record plumbing: emitted by the Expander for (define-record-type ...).
    # Not intended for direct user calls - the %-prefix signals internal use.
    register_primitive('%make-record-type', (2, 2), _prim_make_record_type,
                       doc='Build a record-type descriptor.  Internal: used by define-record-type.',
                       category=CATEGORY)

    register_primitive('%make-record', (2, 2), _prim_make_record,
                       doc='Build a record of a given type from a field-values list.  Internal.',
                       category=CATEGORY)

    register_primitive('%record-of-type?', (2, 2), _prim_record_of_type_p,
                       doc='#t if obj is a record of the given record type.  Internal.',
                       category=CATEGORY)

    register_primitive('%record-ref', (3, 3), _prim_record_ref,
                       doc='Read the i-th field of a record; type-checks against the given record-type.  Internal.',
                       category=CATEGORY)

    register_primitive('%record-set!', (4, 4), _prim_record_set,
                       doc='Mutate the i-th field of a record; type-checks against the given record-type.  Internal.',
                       category=CATEGORY)

    register_primitive('%make-record-accessor', (3, 3), _prim_make_record_accessor,
                       doc=('Build a record accessor value.  Applied at a call site, performs '
                            'a type-checked field read with the call-site as the error position.  '
                            'Internal: emitted by define-record-type.'),
                       category=CATEGORY)

    register_primitive('%make-record-mutator', (3, 3), _prim_make_record_mutator,
                       doc=('Build a record mutator value.  Same call-site error story as '
                            '%make-record-accessor.  Internal: emitted by define-record-type.'),
                       category=CATEGORY)

    # Parameter objects
    register_primitive('make-parameter', (1, 2), _prim_make_parameter_unreached,
                       usage='(make-parameter <init> [<converter>])',
                       doc=(
        "Return a new parameter object with initial value <init>.  When\n"
        "called with zero arguments, a parameter returns its current\n"
        "dynamic value; use parameterize to bind a different value for\n"
        "a dynamic extent.  If <converter> is supplied, it is applied\n"
        "to <init> (and to each later parameterize value) to produce the\n"
        "stored value.  R7RS 4.2.6."),
        category=CATEGORY)

    register_primitive('%with-parameters', (3, 3), _prim_with_parameters_unreached,
                       doc=(
        "Dynamically bind parameters for the extent of a thunk.  Internal:\n"
        "the Expander emits calls to %with-parameters for parameterize."),
        category=CATEGORY)

    register_primitive('%continuation-depth', (0, 0),
                       _prim_continuation_depth_unreached,
                       doc=(
        "Return the current continuation-stack (K) length as an integer.\n"
        "Internal: used by tail-call tests to assert bounded continuation\n"
        "space.  Intercepted by the Evaluator (a normal primitive body\n"
        "cannot see K)."),
        category=CATEGORY)

    # Exception object accessors
    register_primitive('error-object-message', (1, 1),
                       _prim_error_object_message,
                       doc='Return the message string of an error object.  R7RS 6.11.',
                       category=CATEGORY)

    register_primitive('error-object-irritants', (1, 1),
                       _prim_error_object_irritants,
                       doc='Return the irritants list of an error object.  R7RS 6.11.',
                       category=CATEGORY)
    register_primitive('file-error?', (1, 1), _prim_file_error_p,
                       doc=('(file-error? obj) returns #t if obj is an error raised by a '
                            'file-related operation.  pyScheme does not currently tag file '
                            'errors distinctly, so this always returns #f (R7RS allows).'),
                       category=CATEGORY)
    register_primitive('read-error?', (1, 1), _prim_read_error_p,
                       doc=('(read-error? obj) returns #t if obj is an error raised during '
                            'parsing by read.  Same caveat as file-error?: always returns '
                            '#f for now.'),
                       category=CATEGORY)

    register_primitive('null-environment', (1, 1), _prim_null_environment,
                       doc=('(null-environment version) returns a minimal frozen environment '
                            'with no variable bindings.  R7RS §6.12 / (scheme r5rs).'),
                       category=CATEGORY)

    register_primitive('scheme-report-environment', (1, 1),
                       _prim_scheme_report_environment,
                       doc=('(scheme-report-environment version) returns an environment '
                            'containing the standard procedures.  pyScheme returns the '
                            'interaction environment.  R7RS §6.12 / (scheme r5rs).'),
                       category=CATEGORY)

    register_primitive('load', (1, 2), _prim_load,
                       doc=('(load filename) reads and evaluates all forms in filename in '
                            'the current environment.  R7RS §6.14 / (scheme load).'),
                       category=CATEGORY)

    register_primitive('command-line', (0, 0), _prim_command_line,
                       doc=('(command-line) returns the command-line arguments as a list '
                            'of strings.  R7RS §6.14 / (scheme process-context).'),
                       category=CATEGORY)

    register_primitive('exit', (0, 1), _prim_exit,
                       doc=('(exit [obj]) exits the process.  #t or no arg exits 0; #f exits 1; '
                            'exact integer uses that code.  R7RS §6.14 / (scheme process-context).'),
                       category=CATEGORY)

    register_primitive('emergency-exit', (0, 1), _prim_emergency_exit,
                       doc=('(emergency-exit [obj]) terminates the process immediately via '
                            'os._exit(), bypassing port flushing and dynamic-wind after-thunks.  '
                            'R7RS §6.14 / (scheme process-context).'),
                       category=CATEGORY)

    register_primitive('get-environment-variable', (1, 1),
                       _prim_get_environment_variable,
                       doc=('(get-environment-variable name) returns the value of the named '
                            'OS environment variable as a string, or #f if unset.  '
                            'R7RS §6.14 / (scheme process-context).'),
                       category=CATEGORY)

    register_primitive('get-environment-variables', (0, 0),
                       _prim_get_environment_variables,
                       doc=('(get-environment-variables) returns an alist of (name . value) '
                            'strings for all OS environment variables.  '
                            'R7RS §6.14 / (scheme process-context).'),
                       category=CATEGORY)

    register_primitive('unicode-version', (0, 0), _prim_unicode_version,
                       doc=('(unicode-version) returns the version string of the Unicode '
                            'character database backing char/string operations (e.g. '
                            '"16.0.0").  Not in R7RS; pyscheme reports the host Python\'s '
                            'unicodedata.unidata_version.'),
                       category=CATEGORY)

    register_primitive('syntax-expand', (1, 1), _prim_syntax_expand,
                       usage='(syntax-expand form)',
                       doc=(
        "Expand a Scheme form through the macro/sugar expander and return\n"
        "the result as a Scheme value.  The argument should be a quoted\n"
        "datum.  Introduced symbols carry internal scope annotations that\n"
        "do not appear in the printed output.  Gensym names introduced by\n"
        "do, define-record-type, or let-values are visible as-is.\n"
        "Non-standard extension; intended for debugging and test baselines."),
        category=CATEGORY)

    register_primitive('runtime', (0, 0), _prim_runtime,
                       doc=('(runtime) returns the CPU process time used so far as an inexact '
                            'real number of seconds.  MIT Scheme compatibility for SICP.'),
                       category=CATEGORY)

    register_primitive('current-second', (0, 0), _prim_current_second,
                       doc=('(current-second) returns the current UTC time as an inexact '
                            'real (seconds since 1970-01-01T00:00:00Z).  '
                            'R7RS §6.14 / (scheme time).'),
                       category=CATEGORY)

    register_primitive('current-jiffy', (0, 0), _prim_current_jiffy,
                       doc=('(current-jiffy) returns a monotonic count of jiffies (ms here) '
                            'since an unspecified epoch.  R7RS §6.14 / (scheme time).'),
                       category=CATEGORY)

    register_primitive('jiffies-per-second', (0, 0), _prim_jiffies_per_second,
                       doc=('(jiffies-per-second) returns 1000: pyScheme jiffies are milliseconds.  '
                            'R7RS §6.14 / (scheme time).'),
                       category=CATEGORY)
