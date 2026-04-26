"""R7RS syntax-rules pattern matcher and template instantiator (R7RS 4.3).

Called from the Expander when a form's head is a bound syntax transformer.
Public API:

    parse_syntax_rules(tail, def_env, name)
        Parse a (syntax-rules [ellipsis] (literals) rules...) body into
        a SyntaxTransformer.

    apply_syntax_transformer(t, form)
        Run transformer t against form; return the expanded s-expression.
        Raises SchemeSyntaxError if no pattern matches.

Hygiene approach matches the C++ reference: gensym unbound free ids in
the template, substitute def-env VALUES directly for bound free ids.
Full syntactic-closure hygiene is a documented follow-up limitation.
"""

from pyscheme.AST import (
   is_cons, is_nil, is_symbol, is_string, is_integer, is_real, is_boolean,
   is_character, is_vector,
   as_symbol, as_string, as_integer, as_real, as_boolean, as_character,
   as_vector_items,
   alloc_cons, make_symbol, make_vector, list_from_items, src_of, eqv_atom,
   make_syntax_transformer, is_syntax_transformer,
   NIL_VALUE, SYMBOL,
   new_scope, symbol_flip_scope, add_scope_to_form,
)


# Syntactic keywords that must not be renamed during hygienic expansion.
# These name the built-in special forms and desugaring markers the
# Expander / Evaluator recognize directly.
_SYNTACTIC_KEYWORDS = {
   'if', 'lambda', 'begin', 'define', 'set!', 'quote',
   'let', 'let*', 'letrec', 'letrec*',
   'and', 'or', 'case', 'cond', 'when', 'unless', 'do', 'guard',
   'parameterize', 'define-syntax', 'let-syntax', 'letrec-syntax',
   'define-record-type', 'syntax-rules', 'define-library',
   'import', 'export', 'case-lambda',
   'quasiquote', 'unquote', 'unquote-splicing',
   'define-values', 'let-values', 'let*-values',
   'include', 'include-ci', 'cond-expand',
   'delay', 'delay-force',
   'else', '=>', 'library',
   # Also treat keyword-like constants that appear in patterns:
   '_', '...',
}


# Module-level counter for hygiene gensyms.  Shared by all transformers.
_GENSYM_COUNTER = 0


def hygiene_gensym(base):
   """Generate a fresh symbol name unlikely to collide with user code.
   Uses a non-printable marker byte to discourage accidental reuse."""
   global _GENSYM_COUNTER
   _GENSYM_COUNTER = _GENSYM_COUNTER + 1
   return '\x01h.' + base + '.' + str(_GENSYM_COUNTER)


# ── SyntaxMatch: bindings produced by pattern matching ─────────────────────

class _SyntaxMatch:
   """Bindings from one pattern-match attempt.
      scalars     - dict: name -> Scheme value (depth 0 pattern vars)
      ellipsis    - dict: name -> Python list of values (depth >= 1 pvars)
      ell_depth   - dict: name -> int, the ellipsis depth
      renames     - dict: free id name -> gensym name (legacy, unused)
      intro_scopes - dict: free id name -> scope_id (sets-of-scopes hygiene)"""
   def __init__(self):
      self.scalars = {}
      self.ellipsis = {}
      self.ell_depth = {}
      self.renames = {}
      self.intro_scopes = {}


def _is_ellipsis(form, ellipsis_sym):
   return is_symbol(form) and as_symbol(form) == ellipsis_sym


# ── Pattern-variable collectors ────────────────────────────────────────────

def collect_pvars(pat, literals, ellipsis_sym, out):
   """Add to the set `out` every pattern variable in `pat`.  Non-pvars:
   literals, the underscore, the ellipsis symbol."""
   if is_symbol(pat):
      s = as_symbol(pat)
      if s == '_' or s == ellipsis_sym:
         return
      i = 0
      while i < len(literals):
         if s == literals[i]:
            return
         i = i + 1
      out.add(s)
      return
   if is_cons(pat):
      collect_pvars(pat.car, literals, ellipsis_sym, out)
      collect_pvars(pat.cdr, literals, ellipsis_sym, out)
   if is_vector(pat):
      for item in as_vector_items(pat):
         collect_pvars(item, literals, ellipsis_sym, out)


def collect_pvars_with_depth(pat, literals, ellipsis_sym, out, depth):
   """Record each pvar's ellipsis depth in `out` (a dict name -> int)."""
   if is_symbol(pat):
      s = as_symbol(pat)
      if s == '_' or s == ellipsis_sym:
         return
      i = 0
      while i < len(literals):
         if s == literals[i]:
            return
         i = i + 1
      out[s] = depth
      return
   if is_cons(pat):
      cur = pat
      while is_cons(cur):
         elem = cur.car
         rest = cur.cdr
         has_ell = is_cons(rest) and _is_ellipsis(rest.car, ellipsis_sym)
         if has_ell:
            collect_pvars_with_depth(elem, literals, ellipsis_sym, out, depth + 1)
            cur = rest.cdr
         else:
            collect_pvars_with_depth(elem, literals, ellipsis_sym, out, depth)
            cur = rest
      if not is_nil(cur):
         collect_pvars_with_depth(cur, literals, ellipsis_sym, out, depth)
   if is_vector(pat):
      items = as_vector_items(pat)
      i = 0
      n = len(items)
      while i < n:
         has_ell = (i + 1 < n
                    and is_symbol(items[i + 1])
                    and as_symbol(items[i + 1]) == ellipsis_sym)
         if has_ell:
            collect_pvars_with_depth(items[i], literals, ellipsis_sym, out, depth + 1)
            i = i + 2
         else:
            collect_pvars_with_depth(items[i], literals, ellipsis_sym, out, depth)
            i = i + 1


# ── Free-identifier collector ──────────────────────────────────────────────

def collect_free_ids(tmpl, pvars, literals, ellipsis_sym, out):
   """Collect template identifiers that aren't pvars / literals / ellipsis /
   underscore / syntactic keywords; skips inside (quote ...) since those
   are data.  `out` is a set to add to."""
   if is_symbol(tmpl):
      s = as_symbol(tmpl)
      if s == ellipsis_sym or s == '_':
         return
      if s in pvars:
         return
      i = 0
      while i < len(literals):
         if s == literals[i]:
            return
         i = i + 1
      if s in _SYNTACTIC_KEYWORDS:
         return
      out.add(s)
      return
   if is_cons(tmpl):
      if is_symbol(tmpl.car) and as_symbol(tmpl.car) == 'quote':
         return
      collect_free_ids(tmpl.car, pvars, literals, ellipsis_sym, out)
      collect_free_ids(tmpl.cdr, pvars, literals, ellipsis_sym, out)
   if is_vector(tmpl):
      for item in as_vector_items(tmpl):
         collect_free_ids(item, pvars, literals, ellipsis_sym, out)


# ── Pattern matching ────────────────────────────────────────────────────────

def _list_length_approx(lst):
   n = 0
   cur = lst
   while is_cons(cur):
      n = n + 1
      cur = cur.cdr
   return n


def _datum_equal(a, b):
   """Structural equality for literal datums appearing in patterns."""
   if eqv_atom(a, b):
      return True
   if is_string(a) and is_string(b):
      return as_string(a) == as_string(b)
   if is_cons(a) and is_cons(b):
      if not _datum_equal(a.car, b.car):
         return False
      return _datum_equal(a.cdr, b.cdr)
   if is_nil(a) and is_nil(b):
      return True
   if is_vector(a) and is_vector(b):
      ia = as_vector_items(a)
      ib = as_vector_items(b)
      if len(ia) != len(ib):
         return False
      i = 0
      while i < len(ia):
         if not _datum_equal(ia[i], ib[i]):
            return False
         i = i + 1
      return True
   return False


def _match_pattern(pat, form, literals, ellipsis_sym, out):
   """Match one pattern node against one form node.  Returns True and
   fills `out`, or False."""
   if is_symbol(pat):
      s = as_symbol(pat)
      if s == '_':
         return True
      # Literal identifier: must match the same symbol exactly.
      i = 0
      while i < len(literals):
         if s == literals[i]:
            return is_symbol(form) and as_symbol(form) == s
         i = i + 1
      # Pattern variable: bind.
      out.scalars[s] = form
      return True
   if is_cons(pat):
      return _match_list_pattern(pat, form, literals, ellipsis_sym, out)
   if is_vector(pat):
      if not is_vector(form):
         return False
      return _match_vector_pattern(as_vector_items(pat), as_vector_items(form),
                                   literals, ellipsis_sym, out)
   if is_nil(pat):
      return is_nil(form)
   # Literal datum (number, string, etc.).
   return _datum_equal(pat, form)


def _match_list_pattern(pat_list, form_list, literals, ellipsis_sym, out):
   """Match a list-shaped pattern against a list-shaped form.  Handles
   ellipsis, fixed prefix, and improper (dotted) tail."""
   while is_cons(pat_list):
      pat_elem = pat_list.car
      pat_rest = pat_list.cdr
      has_ell = (is_cons(pat_rest)
                 and _is_ellipsis(pat_rest.car, ellipsis_sym))
      if has_ell:
         suffix_pat  = pat_rest.cdr
         suffix_need = _list_length_approx(suffix_pat)
         # Collect form elements into a Python list.
         form_vec = []
         tmp = form_list
         while is_cons(tmp):
            form_vec.append(tmp.car)
            tmp = tmp.cdr
         total = len(form_vec)
         if total < suffix_need:
            return False
         n_ellipsis = total - suffix_need
         # Record each pvar under pat_elem with its depth + 1.
         pvar_depths = {}
         collect_pvars_with_depth(pat_elem, literals, ellipsis_sym, pvar_depths, 0)
         for pv in pvar_depths:
            out.ellipsis[pv] = []
            out.ell_depth[pv] = pvar_depths[pv] + 1
         # Match pat_elem against each ellipsis-covered form element.
         i = 0
         while i < n_ellipsis:
            sub = _SyntaxMatch()
            if not _match_pattern(pat_elem, form_vec[i], literals, ellipsis_sym, sub):
               return False
            # Scalars from sub -> append to ellipsis list in outer.
            for k in sub.scalars:
               out.ellipsis[k].append(sub.scalars[k])
            # Inner ellipsis -> wrap inner list as a value, append to outer.
            for k in sub.ellipsis:
               out.ellipsis[k].append(sub.ellipsis[k])
            i = i + 1
         # Rebuild suffix form from form_vec[n_ellipsis:] and continue.
         suffix_form = NIL_VALUE
         j = total - 1
         while j >= n_ellipsis:
            suffix_form = alloc_cons(form_vec[j], suffix_form)
            j = j - 1
         return _match_list_pattern(suffix_pat, suffix_form, literals, ellipsis_sym, out)
      # Normal element.
      if not is_cons(form_list):
         return False
      if not _match_pattern(pat_elem, form_list.car, literals, ellipsis_sym, out):
         return False
      pat_list = pat_rest
      form_list = form_list.cdr
   # Pattern list exhausted.
   if is_nil(pat_list):
      return is_nil(form_list)
   # Improper list: dotted tail is a rest pvar.
   if is_symbol(pat_list):
      s = as_symbol(pat_list)
      i = 0
      while i < len(literals):
         if s == literals[i]:
            return False
         i = i + 1
      out.scalars[s] = form_list
      return True
   return False


def _match_vector_pattern(pat_items, form_items, literals, ellipsis_sym, out):
   """Match a vector pattern (Python list) against a vector form (Python list).
   Supports ellipsis and tail patterns identically to _match_list_pattern."""
   i = 0
   j = 0
   n_pat  = len(pat_items)
   n_form = len(form_items)
   while i < n_pat:
      pat_elem = pat_items[i]
      has_ell  = (i + 1 < n_pat
                  and _is_ellipsis(pat_items[i + 1], ellipsis_sym))
      if has_ell:
         suffix_count = n_pat - (i + 2)
         available    = n_form - j
         if available < suffix_count:
            return False
         n_ellipsis = available - suffix_count
         pvar_depths = {}
         collect_pvars_with_depth(pat_elem, literals, ellipsis_sym, pvar_depths, 0)
         for pv in pvar_depths:
            out.ellipsis[pv]  = []
            out.ell_depth[pv] = pvar_depths[pv] + 1
         k = 0
         while k < n_ellipsis:
            sub = _SyntaxMatch()
            if not _match_pattern(pat_elem, form_items[j + k],
                                   literals, ellipsis_sym, sub):
               return False
            for key in sub.scalars:
               out.ellipsis[key].append(sub.scalars[key])
            for key in sub.ellipsis:
               out.ellipsis[key].append(sub.ellipsis[key])
            k = k + 1
         j = j + n_ellipsis
         i = i + 2
         continue
      if j >= n_form:
         return False
      if not _match_pattern(pat_elem, form_items[j], literals, ellipsis_sym, out):
         return False
      i = i + 1
      j = j + 1
   return j == n_form


# ── Template instantiation ──────────────────────────────────────────────────

def _collect_ell_refs(tmpl, match, out):
   """Find all ellipsis-bound pvars referenced in a template sub-element.
   `out` is a Python list; may contain duplicates (caller doesn't care)."""
   if is_symbol(tmpl):
      s = as_symbol(tmpl)
      if s in match.ellipsis:
         out.append(s)
      return
   if is_cons(tmpl):
      _collect_ell_refs(tmpl.car, match, out)
      _collect_ell_refs(tmpl.cdr, match, out)
   if is_vector(tmpl):
      for item in as_vector_items(tmpl):
         _collect_ell_refs(item, match, out)


def _instantiate(tmpl, match, ellipsis_sym, use_src):
   """Expand a template sub-expression against match bindings.  use_src
   is the macro use-site source position; new cons cells synthesized
   from the template carry this src so downstream errors point at the
   user's macro call rather than nowhere."""
   if is_symbol(tmpl):
      s = as_symbol(tmpl)
      if s in match.scalars:
         return match.scalars[s]
      if s in match.intro_scopes:
         return symbol_flip_scope(tmpl, match.intro_scopes[s])
      return tmpl
   if is_cons(tmpl):
      return _instantiate_list(tmpl, match, ellipsis_sym, use_src)
   if is_vector(tmpl):
      return _instantiate_vector(as_vector_items(tmpl), match, ellipsis_sym, use_src)
   return tmpl


def _instantiate_vector(tmpl_items, match, ellipsis_sym, use_src):
   """Instantiate a vector template; returns a fresh vector value."""
   output = []
   i = 0
   n = len(tmpl_items)
   while i < n:
      elem    = tmpl_items[i]
      has_ell = (i + 1 < n
                 and _is_ellipsis(tmpl_items[i + 1], ellipsis_sym))
      if has_ell:
         ell_syms = []
         _collect_ell_refs(elem, match, ell_syms)
         if ell_syms:
            count = len(match.ellipsis[ell_syms[0]])
            k = 0
            while k < count:
               sub = _SyntaxMatch()
               for key in match.scalars:
                  sub.scalars[key] = match.scalars[key]
               for key in match.renames:
                  sub.renames[key] = match.renames[key]
               for key in match.intro_scopes:
                  sub.intro_scopes[key] = match.intro_scopes[key]
               for key in match.ellipsis:
                  sub.ellipsis[key]  = match.ellipsis[key]
                  sub.ell_depth[key] = match.ell_depth.get(key, 0)
               j = 0
               while j < len(ell_syms):
                  sv     = ell_syms[j]
                  d      = match.ell_depth[sv]
                  peeled = match.ellipsis[sv][k]
                  if d == 1:
                     sub.scalars[sv] = peeled
                     if sv in sub.ellipsis:
                        del sub.ellipsis[sv]
                     if sv in sub.ell_depth:
                        del sub.ell_depth[sv]
                  else:
                     sub.ellipsis[sv]  = peeled
                     sub.ell_depth[sv] = d - 1
                  j = j + 1
               output.append(_instantiate(elem, sub, ellipsis_sym, use_src))
               k = k + 1
         i = i + 2
         continue
      output.append(_instantiate(elem, match, ellipsis_sym, use_src))
      i = i + 1
   return make_vector(output)


def _instantiate_list(tmpl_list, match, ellipsis_sym, use_src):
   output = []
   cur = tmpl_list
   while is_cons(cur):
      elem = cur.car
      rest = cur.cdr
      has_ell = is_cons(rest) and _is_ellipsis(rest.car, ellipsis_sym)
      if has_ell:
         ell_syms = []
         _collect_ell_refs(elem, match, ell_syms)
         if ell_syms:
            n = len(match.ellipsis[ell_syms[0]])
            i = 0
            while i < n:
               sub = _SyntaxMatch()
               for k in match.scalars:
                  sub.scalars[k] = match.scalars[k]
               for k in match.renames:
                  sub.renames[k] = match.renames[k]
               for k in match.intro_scopes:
                  sub.intro_scopes[k] = match.intro_scopes[k]
               # Copy ellipsis bindings, peeling one layer for ell_syms.
               for k in match.ellipsis:
                  sub.ellipsis[k] = match.ellipsis[k]
                  sub.ell_depth[k] = match.ell_depth.get(k, 0)
               j = 0
               while j < len(ell_syms):
                  sv = ell_syms[j]
                  d = match.ell_depth[sv]
                  peeled = match.ellipsis[sv][i]
                  if d == 1:
                     sub.scalars[sv] = peeled
                     if sv in sub.ellipsis:
                        del sub.ellipsis[sv]
                     if sv in sub.ell_depth:
                        del sub.ell_depth[sv]
                  else:
                     sub.ellipsis[sv] = peeled
                     sub.ell_depth[sv] = d - 1
                  j = j + 1
               output.append(_instantiate(elem, sub, ellipsis_sym, use_src))
               i = i + 1
         # Zero ellipsis-bound pvars under elem -> zero repetitions.
         cur = rest.cdr
         continue
      output.append(_instantiate(elem, match, ellipsis_sym, use_src))
      cur = rest
   # Handle tail (nil or rest pvar).
   if is_nil(cur):
      tail = NIL_VALUE
   elif is_symbol(cur):
      s = as_symbol(cur)
      if s in match.scalars:
         tail = match.scalars[s]
      elif s in match.intro_scopes:
         tail = symbol_flip_scope(cur, match.intro_scopes[s])
      else:
         tail = cur
   else:
      tail = _instantiate(cur, match, ellipsis_sym, use_src)
   result = tail
   i = len(output) - 1
   while i >= 0:
      result = alloc_cons(output[i], result, use_src)
      i = i - 1
   return result


# ── Transformer application ─────────────────────────────────────────────────

def apply_syntax_transformer(t, form):
   """Try each rule in order; on match, instantiate the template with
   sets-of-scopes hygiene.  Raises SchemeSyntaxError if no pattern matches."""
   from pyscheme.Parser import SchemeSyntaxError
   literals = t.literals
   ellipsis_sym = t.ellipsis
   use_src  = src_of(form)
   sc_use   = new_scope()
   sc_intro = new_scope()
   scoped_form = add_scope_to_form(form, sc_use)
   i = 0
   while i < len(t.rules):
      pattern = t.rules[i][0]
      template = t.rules[i][1]
      if is_cons(pattern):
         match = _SyntaxMatch()
         form_tail = scoped_form.cdr if is_cons(scoped_form) else NIL_VALUE
         if _match_list_pattern(pattern.cdr, form_tail,
                                literals, ellipsis_sym, match):
            _apply_hygiene(t, pattern, template, match, literals, ellipsis_sym, sc_intro)
            return _instantiate(template, match, ellipsis_sym, use_src)
      i = i + 1
   raise SchemeSyntaxError(
      "syntax-rules: no matching pattern for '" + t.name + "'", use_src)


def _apply_hygiene(t, pattern, template, match, literals, ellipsis_sym, sc_intro):
   """Populate match bindings for free ids in the template.
   Ids bound at the macro's def site get their value substituted directly.
   All other free ids get sc_intro flipped into their scope_set so they
   are distinguishable from same-named use-site identifiers."""
   pvars = set()
   collect_pvars(pattern, literals, ellipsis_sym, pvars)
   free_ids = set()
   collect_free_ids(template, pvars, literals, ellipsis_sym, free_ids)
   for fid in free_ids:
      if fid in t.def_env:
         match.scalars[fid] = t.def_env[fid]
         continue
      match.intro_scopes[fid] = sc_intro


# ── Parse (syntax-rules [ellipsis] (literals) rules...) ─────────────────────

def parse_syntax_rules(tail, def_env, name):
   """Parse a syntax-rules body into a SyntaxTransformer.  `tail` is the
   cdr of the (syntax-rules ...) form."""
   from pyscheme.Parser import SchemeSyntaxError
   if not is_cons(tail):
      raise SchemeSyntaxError('syntax-rules: malformed', src_of(tail))
   ellipsis_sym = '...'
   first = tail.car
   rest = tail.cdr
   # Optional custom ellipsis: (syntax-rules my-dots (literals) rules...)
   if is_symbol(first) and is_cons(rest):
      second = rest.car
      if is_nil(second) or is_cons(second):
         # Treat first as custom ellipsis identifier.
         ellipsis_sym = as_symbol(first)
         lit_list = second
         rules_list = rest.cdr
      else:
         lit_list = first
         rules_list = rest
   else:
      lit_list = first
      rules_list = rest
   literals = []
   cur = lit_list
   while is_cons(cur):
      if not is_symbol(cur.car):
         raise SchemeSyntaxError(
            'syntax-rules: literal must be a symbol', src_of(cur.car))
      literals.append(as_symbol(cur.car))
      cur = cur.cdr
   rules = []
   cur = rules_list
   while is_cons(cur):
      rule = cur.car
      if not is_cons(rule) or not is_cons(rule.cdr):
         raise SchemeSyntaxError(
            'syntax-rules: each rule must be (pattern template)',
            src_of(rule))
      pattern = rule.car
      template = rule.cdr.car
      rules.append((pattern, template))
      cur = cur.cdr
   # Copy def_env so later mutations don't affect this transformer's view.
   return make_syntax_transformer(name, literals, ellipsis_sym, rules,
                                   dict(def_env) if def_env is not None else {})


# ── Self-test ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
   from pyscheme.Parser import parse_one

   n_pass = 0
   n_fail = 0

   def check(label, cond):
      global n_pass, n_fail
      if cond:
         print('[ OK ] ' + label)
         n_pass = n_pass + 1
      else:
         print('[FAIL] ' + label)
         n_fail = n_fail + 1

   # Pattern matching: literal symbol.
   pat = parse_one('(foo bar)')
   form = parse_one('(foo bar)')
   m = _SyntaxMatch()
   ok = _match_list_pattern(pat.cdr, form.cdr, ['bar'], '...', m)
   check('literal matches',  ok)

   # Pattern variable capture.
   pat = parse_one('(foo x)')
   form = parse_one('(foo 42)')
   m = _SyntaxMatch()
   ok = _match_list_pattern(pat.cdr, form.cdr, [], '...', m)
   check('pvar matches',  ok)
   check('pvar bound',    'x' in m.scalars)

   # Ellipsis.
   pat = parse_one('(foo x ...)')
   form = parse_one('(foo 1 2 3)')
   m = _SyntaxMatch()
   ok = _match_list_pattern(pat.cdr, form.cdr, [], '...', m)
   check('ellipsis matches', ok)
   check('ellipsis depth 1', m.ell_depth.get('x') == 1)
   check('ellipsis count 3', len(m.ellipsis.get('x', [])) == 3)

   # parse_syntax_rules.
   body = parse_one('(() ((_ a b) (cons a b)))')
   t = parse_syntax_rules(body, {}, 'mymacro')
   check('parsed no literals',   t.literals == [])
   check('parsed one rule',      len(t.rules) == 1)
   check('default ellipsis',     t.ellipsis == '...')

   # Apply a simple transformer.
   body2 = parse_one('(() ((_ a b) (list a b)))')
   t2 = parse_syntax_rules(body2, {}, 'pair-up')
   form2 = parse_one('(pair-up 1 2)')
   result = apply_syntax_transformer(t2, form2)
   # result should be (list 1 2) with 'list' gensym-renamed (unbound in def_env).
   # So it's ((gensym-for-list) 1 2).  Verify the shape.
   check('expansion is cons',    is_cons(result))
   check('expansion arity 3',    _list_length_approx(result) == 3)
   # The first element is a renamed 'list' symbol.
   check('first is symbol',      is_symbol(result.car))
   check('second is 1',          is_integer(result.cdr.car) and as_integer(result.cdr.car) == 1)
   check('third is 2',           is_integer(result.cdr.cdr.car) and as_integer(result.cdr.cdr.car) == 2)

   print()
   print('%d passed, %d failed' % (n_pass, n_fail))
