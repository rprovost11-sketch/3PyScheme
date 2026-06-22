"""Listener-side utility helpers.

Ported from pythonslisp (D:/swdev/python3/lisp/pythonslisp/Utils.py).  These
support the Listener (REPL, test runner, dribble logging, help listings)
rather than the evaluator itself.
"""

import os


def retrieveFileList(dirname):
    """Return a sorted list of .log file paths in `dirname`."""
    names = os.listdir(dirname)
    names.sort()
    result = []
    i = 0
    while i < len(names):
        full = os.path.join(dirname, names[i])
        if os.path.isfile(full) and names[i].endswith('.log'):
            result.append(full)
        i = i + 1
    return result


# --- columnize helpers -------------------------------------------------


class _Cell:
    """One rendered cell: text content plus optional color escape.  POD."""

    def __init__(self, content, color):
        self.content = content
        self.color = color


def _try_column_layout(lst, nrows, display_width):
    """If lst packs into nrows rows within display_width, return the
    per-column width list; otherwise return None."""
    size = len(lst)
    ncols = (size + nrows - 1) // nrows
    colwidths = []
    totwidth = -2
    col = 0
    while col < ncols:
        colwidth = 0
        row = 0
        while row < nrows:
            i = row + nrows * col
            if i >= size:
                break
            if len(lst[i]) > colwidth:
                colwidth = len(lst[i])
            row = row + 1
        colwidths.append(colwidth)
        totwidth = totwidth + colwidth + 2
        if totwidth > display_width:
            return None
        col = col + 1
    return colwidths


def _resolve_colors(size, itemColor, itemColors):
    """Turn the column's color arguments into a size-long list of codes
    or None placeholders."""
    if itemColors is not None:
        return itemColors
    colors = []
    i = 0
    while i < size:
        if itemColor:
            colors.append(itemColor)
        else:
            colors.append(None)
        i = i + 1
    return colors


def columnize(lst, displayWidth=80, file=None, itemColor=None, itemColors=None):
    """Display a list of strings as a compact set of columns.

    Each column is only as wide as necessary.  Columns are separated by
    two spaces.  itemColors gives per-item ANSI color codes; itemColor
    is a single code applied to all items when itemColors is None.
    Column widths are computed from uncolored string lengths so color
    escapes never affect alignment.
    """
    RESET = '\033[0m'
    size = len(lst)
    if size == 0:
        return
    colors = _resolve_colors(size, itemColor, itemColors)
    if size == 1:
        c = colors[0]
        if c:
            print(c + lst[0] + RESET, file=file)
        else:
            print(lst[0], file=file)
        return

    # Find the smallest nrows for which the layout fits.
    best_nrows = size
    best_ncols = 1
    best_colwidths = [0]
    nrows = 1
    while nrows < size:
        cw = _try_column_layout(lst, nrows, displayWidth)
        if cw is not None:
            best_nrows = nrows
            best_ncols = (size + nrows - 1) // nrows
            best_colwidths = cw
            break
        nrows = nrows + 1

    row = 0
    while row < best_nrows:
        cells = []
        col = 0
        while col < best_ncols:
            i = row + best_nrows * col
            if i < size:
                cells.append(_Cell(lst[i], colors[i]))
            else:
                cells.append(_Cell('', None))
            col = col + 1
        # Trim trailing empty cells.
        while len(cells) > 0 and cells[len(cells) - 1].content == '':
            cells.pop()
        rendered = []
        col = 0
        while col < len(cells):
            content = cells[col].content
            c = cells[col].color
            padded = content.ljust(best_colwidths[col])
            if c and content:
                # Re-split the padded string into "content" + "padding" so
                # the color escape wraps only the content, not the spaces.
                padding_len = len(padded) - len(content)
                padding = ''
                k = 0
                while k < padding_len:
                    padding = padding + ' '
                    k = k + 1
                rendered.append(c + content + RESET + padding)
            else:
                rendered.append(padded)
            col = col + 1
        print('  '.join(rendered), file=file)
        row = row + 1


# --- paren-state scanner -----------------------------------------------


class ParenState:
    """Net-paren-depth plus in-string flag after scanning a chunk of text.
    stack holds each unclosed delimiter ('(' or '[') in open order so the
    caller can inspect which kind is innermost."""

    def __init__(self, depth, in_string, stack):
        self.depth = depth
        self.in_string = in_string
        self.stack = stack


def paren_state(text):
    """Scan `text`, ignoring string contents and `;` comments.  Returns a
    ParenState whose depth is the number of unclosed delimiters, in_string
    is True if the scan ended inside a string literal, and stack is the
    list of unclosed delimiter characters ('(' or '[') in open order."""
    stack = []
    in_string = False
    in_pipe = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if escape:
            escape = False
        elif in_string:
            if ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        elif in_pipe:
            if ch == '\\':
                escape = True
            elif ch == '|':
                in_pipe = False
        else:
            if ch == '"':
                in_string = True
            elif ch == '|':
                in_pipe = True
            elif ch == ';':
                while i < n and text[i] != '\n':
                    i = i + 1
                continue
            elif ch == '(' or ch == '[':
                stack.append(ch)
            elif ch == ')' or ch == ']':
                if len(stack) > 0:
                    stack.pop()
        i = i + 1
    return ParenState(len(stack), in_string, stack)


# --- writeln fan-out ---------------------------------------------------


def writeln_multiFile(outputString, fileList, flush=False):
    """Print outputString (with a trailing newline) to each file stream in
    fileList.  A stream of None prints to stdout."""
    i = 0
    while i < len(fileList):
        print(outputString, end='\n', flush=flush, file=fileList[i])
        i = i + 1


# --- session-log parsing + match semantics -----------------------------
#
# A second, free-standing implementation of the .log parser and the test
# match semantics, used to back the (parse-log-file ...) and (log-match? ...)
# primitives (and through them the Scheme-side universal interpreter differ,
# which treats a .log file as a reference interpreter).  The Listener keeps
# its OWN copy (Listener._parse_log / _match_retval / the test runner) for the
# in-process harness; these are deliberately independent so differ work does
# not disturb the existing harness.  The two must stay behaviourally identical.


def parse_log(text):
    """Parse a session log into a list of 5-tuples
    (expr, output, retval, error, fold_case).

    Each entry begins with a '>>> ' line; '... ' lines continue the
    expression.  Lines before '==> ' (with no marker) are output; '==> '
    gives the return value, '%%% ' the error message.  '#!fold-case' /
    '#!no-fold-case' directives toggle case folding for following entries."""
    lines = text.splitlines(keepends=True)
    entries = []
    idx = 0
    n = len(lines)
    fold_case = False
    while idx < n:
        while idx < n and not lines[idx].startswith('>>> '):
            stripped = lines[idx].rstrip()
            if stripped == '#!fold-case':
                fold_case = True
            elif stripped == '#!no-fold-case':
                fold_case = False
            idx = idx + 1
        if idx >= n:
            break
        entry_fold_case = fold_case
        expr = lines[idx][4:]
        output = ''
        retval = ''
        error = ''
        idx = idx + 1
        while idx < n and lines[idx].startswith('... '):
            expr = expr + lines[idx][4:]
            idx = idx + 1
        if idx < n and lines[idx].rstrip() == '...' and not lines[idx].startswith('... '):
            idx = idx + 1
        while idx < n:
            line = lines[idx]
            if line.startswith('==> ') or line.rstrip() == '==>':
                break
            if line.startswith('... ') or line.startswith('>>> ') or line.startswith('%%% '):
                break
            output = output + line
            idx = idx + 1
        if idx < n and (lines[idx].startswith('==> ') or lines[idx].rstrip() == '==>'):
            line = lines[idx]
            if len(line) > 4:
                retval = line[4:]
            idx = idx + 1
            while idx < n:
                line = lines[idx]
                if line.startswith('==> ') or line.rstrip() == '==>':
                    break
                if line.startswith('... ') or line.startswith('>>> ') or line.startswith('%%% '):
                    break
                if line.startswith('#!'):
                    break  # fold-case directive
                if line.startswith(';'):
                    expr = expr + line
                else:
                    retval = retval + line
                idx = idx + 1
        if idx < n and lines[idx].startswith('%%% '):
            error = lines[idx][4:]
            idx = idx + 1
            while idx < n and lines[idx].startswith('%%% '):
                error = error + lines[idx][4:]
                idx = idx + 1
        if expr:
            entries.append(
                (expr, output.rstrip(), retval.rstrip(), error.rstrip(), entry_fold_case))
    return entries


def match_retval(actual, expected):
    """True if actual matches expected, honouring 'X or ==> Y' alternatives."""
    if ' or ==> ' in expected:
        parts = expected.split(' or ==> ')
        i = 0
        while i < len(parts):
            if actual == parts[i].strip():
                return True
            i = i + 1
        return False
    return actual == expected


def log_match(expected_output, expected_retval, expected_error,
              actual_output, actual_retval, actual_error, timed_out):
    """Apply the .log test match semantics, returning the triple
    (output_ok, retval_ok, error_ok).

    '%%% *' / '%%% %any-error%' accept any raised error; '%%% %optional-error%'
    models R7RS 'it is an error' (passes whether or not an error is raised); a
    timeout always fails."""
    expected_output = expected_output.rstrip()
    actual_output = actual_output.rstrip()
    if timed_out:
        return (False, False, False)
    if expected_error.startswith('%optional-error%'):
        return (True, True, True)
    if expected_error == '*' or expected_error.startswith('%any-error%'):
        error_ok = bool(actual_error)
    else:
        error_ok = actual_error == expected_error
    retval_ok = match_retval(actual_retval, expected_retval)
    output_ok = actual_output == expected_output
    return (output_ok, retval_ok, error_ok)
