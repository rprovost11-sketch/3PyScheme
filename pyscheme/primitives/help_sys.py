"""(help) and (apropos) primitives for interactive documentation.

(help)                - list every callable in the global environment,
                        grouped by category (primitives by source module,
                        closures under 'Procedures'), plus available
                        help topics.
(help <callable>)     - show the entry (type label, usage, docstring)
                        for a primitive or procedure value.
(help <symbol>)       - resolve the symbol's binding and show its entry.
(help <string>)       - if the string names a help topic, print it;
                        otherwise do an apropos-style substring search.
(apropos <string>)    - print every global name containing the substring.

Help topics are plain-text files stored in pyscheme/help/.  Each file
name (without extension) is the topic name.  Subdirectories under help/
partition the topics by section (e.g., tutorial/, reference/) which is
reflected in the listing.
"""

import os

from pyscheme.primitives import register_primitive
from pyscheme.AST import (
   is_string, is_symbol, is_primitive, is_closure,
   as_string, as_symbol,
   as_closure_params, as_closure_rest_name, as_closure_docstring,
   as_primitive_name,
   VOID_VALUE,
)
from pyscheme.Environment import SchemeTypeError


CATEGORY = 'help_sys'


HELP_DIR = os.path.join(
   os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
   'help')


# ---- layout helper ----------------------------------------------------


class _Layout:
   """Result of column-packing a list of items into a grid.  POD."""
   def __init__(self, nrows, ncols, colwidths):
      self.nrows     = nrows
      self.ncols     = ncols
      self.colwidths = colwidths


def _try_layout(items, nrows, width):
   """If items pack into nrows rows with total width <= width, return a
   _Layout; otherwise return None."""
   size = len(items)
   ncols = (size + nrows - 1) // nrows
   colwidths = []
   total = -2
   c = 0
   while c < ncols:
      w = 0
      r = 0
      while r < nrows:
         i = r + nrows * c
         if i >= size:
            break
         if len(items[i]) > w:
            w = len(items[i])
         r = r + 1
      colwidths.append(w)
      total = total + w + 2
      if total > width:
         return None
      c = c + 1
   return _Layout(nrows, ncols, colwidths)


def _widest(items):
   """Length of the longest string in items."""
   widest = 0
   i = 0
   while i < len(items):
      if len(items[i]) > widest:
         widest = len(items[i])
      i = i + 1
   return widest


def _columnize(items, width=78):
   """Arrange items into compact columns that fit within `width`.
   Returns a list of formatted lines.  Deterministic - no color escapes."""
   if not items:
      return []
   if len(items) == 1:
      return [items[0]]
   best = None
   nrows = 1
   while nrows <= len(items):
      layout = _try_layout(items, nrows, width)
      if layout is not None:
         best = layout
         break
      nrows = nrows + 1
   if best is None:
      best = _Layout(len(items), 1, [_widest(items)])

   lines = []
   r = 0
   while r < best.nrows:
      cells = []
      c = 0
      while c < best.ncols:
         i = r + best.nrows * c
         if i < len(items):
            cells.append(items[i].ljust(best.colwidths[c]))
         c = c + 1
      lines.append('  '.join(cells).rstrip())
      r = r + 1
   return lines


# ---- topic discovery (file-system walk) -------------------------------


def _scan_dir(root, section, topics):
   """Recurse into root; add .txt files under it to topics[section]."""
   entries = os.listdir(root)
   i = 0
   while i < len(entries):
      name = entries[i]
      full = os.path.join(root, name)
      if os.path.isdir(full):
         if section == '':
            _scan_dir(full, name, topics)
         else:
            _scan_dir(full, section, topics)
      elif name.endswith('.txt'):
         stem = os.path.splitext(name)[0]
         if section not in topics:
            topics[section] = []
         topics[section].append(stem)
      i = i + 1


def _discover_topics():
   """Return {section_name: [topic_stem, ...]} discovered under HELP_DIR.
   Section '' holds topics placed directly in HELP_DIR (no subdirectory)."""
   topics = {}
   if not os.path.isdir(HELP_DIR):
      return topics
   _scan_dir(HELP_DIR, '', topics)
   for section in topics:
      topics[section].sort()
   return topics


def _find_topic_in_dir(root, lo_name):
   entries = os.listdir(root)
   i = 0
   while i < len(entries):
      entry = entries[i]
      full = os.path.join(root, entry)
      if os.path.isdir(full):
         found = _find_topic_in_dir(full, lo_name)
         if found is not None:
            return found
      elif entry.endswith('.txt'):
         stem = os.path.splitext(entry)[0]
         if stem.lower() == lo_name:
            return full
      i = i + 1
   return None


def _find_topic(name):
   """Look for a topic file named '<name>.txt' anywhere under HELP_DIR."""
   if not os.path.isdir(HELP_DIR):
      return None
   return _find_topic_in_dir(HELP_DIR, name.lower())


# ---- labels -----------------------------------------------------------


def _type_label(kind):
   if kind == 'special':
      return 'Special Form'
   if kind == 'primitive':
      return 'Primitive'
   if kind == 'procedure':
      return 'Procedure'
   if kind == 'macro':
      return 'Macro'
   return kind


def _args_label(kind):
   if kind == 'special' or kind == 'macro':
      return 'args: unevaluated'
   return 'args: pre-evaluated'


# ---- listing ----------------------------------------------------------


def _collect_primitives_by_category():
   """Return {category: [name, ...]} for every registered primitive."""
   from pyscheme.primitives import PRIMITIVE_HELP
   by_cat = {}
   for name in PRIMITIVE_HELP:
      info = PRIMITIVE_HELP[name]
      cat  = info[3]
      if cat not in by_cat:
         by_cat[cat] = []
      by_cat[cat].append(name)
   for cat in by_cat:
      by_cat[cat].sort()
   return by_cat


def _collect_closures(global_env):
   """Return a sorted list of the names bound to closure values in env."""
   closures = []
   for name in global_env._bindings:
      val = global_env._bindings[name]
      if is_closure(val):
         closures.append(name)
   closures.sort()
   return closures


def _category_print_order(by_cat):
   """Return the categories in display order: CATEGORY_ORDER first, then
   any unlisted categories alphabetically."""
   from pyscheme.primitives import CATEGORY_ORDER
   order = []
   seen  = {}
   i = 0
   while i < len(CATEGORY_ORDER):
      cat = CATEGORY_ORDER[i]
      if cat in by_cat:
         order.append(cat)
         seen[cat] = True
      i = i + 1
   remaining = []
   for cat in by_cat:
      if cat not in seen:
         remaining.append(cat)
   remaining.sort()
   i = 0
   while i < len(remaining):
      order.append(remaining[i])
      i = i + 1
   return order


def _quoted_topic_names(stems):
   """Wrap each stem in double quotes for display."""
   result = []
   i = 0
   while i < len(stems):
      result.append('"' + stems[i] + '"')
      i = i + 1
   return result


def _write_topics_section(out, topics):
   """Render the Topics section of the help listing.  Section '' holds
   topics from the top level; other sections appear as labeled rows."""
   out.write('Topics\n')
   out.write('------\n')
   section_keys = list(topics.keys())
   section_keys.sort()
   i = 0
   while i < len(section_keys):
      section = section_keys[i]
      items = _quoted_topic_names(topics[section])
      if section:
         out.write('  ' + section.title() + ': ')
         out.write('  '.join(items))
         out.write('\n')
      else:
         lines = _columnize(items)
         j = 0
         while j < len(lines):
            out.write(lines[j] + '\n')
            j = j + 1
      i = i + 1
   out.write('\n')


def _list_all(ctx, env):
   """Print the full help listing to ctx.outStrm."""
   from pyscheme.primitives import CATEGORY_TITLES

   by_cat   = _collect_primitives_by_category()
   closures = _collect_closures(env.getGlobalEnv())

   out = ctx.outStrm
   out.write('Scheme Documentation\n')
   out.write('====================\n\n')

   order = _category_print_order(by_cat)
   i = 0
   while i < len(order):
      cat = order[i]
      if cat in CATEGORY_TITLES:
         title = CATEGORY_TITLES[cat]
      else:
         title = cat.title()
      out.write(title + '\n')
      out.write('-' * len(title) + '\n')
      lines = _columnize(by_cat[cat])
      j = 0
      while j < len(lines):
         out.write(lines[j] + '\n')
         j = j + 1
      out.write('\n')
      i = i + 1

   if closures:
      out.write('Procedures\n')
      out.write('----------\n')
      lines = _columnize(closures)
      j = 0
      while j < len(lines):
         out.write(lines[j] + '\n')
         j = j + 1
      out.write('\n')

   topics = _discover_topics()
   if topics:
      _write_topics_section(out, topics)

   out.write("Type (help <name>) for documentation on a name.\n")
   out.write("Type (help \"topic\") to view a topic or search by substring.\n")
   out.write("Type (apropos \"substring\") to list every matching name.\n")


# ---- per-entry --------------------------------------------------------


def _write_doc_block(out, doc):
   """Write doc text indented by three spaces, preserving blank lines.
   Falls back to a placeholder when doc is empty."""
   if not doc:
      out.write('   (no documentation)\n')
      return
   lines = doc.splitlines()
   i = 0
   while i < len(lines):
      line = lines[i]
      if line:
         out.write('   ' + line + '\n')
      else:
         out.write('\n')
      i = i + 1


def _show_primitive_entry(ctx, name):
   """Print the help entry for the registered primitive `name`.
   Returns True if found, False otherwise."""
   from pyscheme.primitives import PRIMITIVE_HELP
   if name not in PRIMITIVE_HELP:
      return False
   info  = PRIMITIVE_HELP[name]
   kind  = info[0]
   usage = info[1]
   doc   = info[2]
   out = ctx.outStrm
   out.write(_type_label(kind) + '  |  ' + _args_label(kind) + '\n')
   out.write('\n')
   out.write('   Usage: ' + usage + '\n')
   out.write('\n')
   _write_doc_block(out, doc)
   return True


def _closure_usage(name, closure):
   """Build a '(name a b . rest)' style usage string for a closure."""
   params    = as_closure_params(closure)
   rest_name = as_closure_rest_name(closure)
   parts = []
   i = 0
   while i < len(params):
      parts.append(params[i])
      i = i + 1
   if rest_name is not None:
      if len(parts) == 0:
         return '(' + name + ' . ' + rest_name + ')'
      return '(' + name + ' ' + ' '.join(parts) + ' . ' + rest_name + ')'
   if len(parts) == 0:
      return '(' + name + ')'
   return '(' + name + ' ' + ' '.join(parts) + ')'


def _show_closure_entry(ctx, name, closure):
   """Print the help entry for a user-defined procedure."""
   usage     = _closure_usage(name, closure)
   docstring = as_closure_docstring(closure)
   out = ctx.outStrm
   out.write('Procedure  |  args: pre-evaluated\n')
   out.write('\n')
   out.write('   Usage: ' + usage + '\n')
   out.write('\n')
   _write_doc_block(out, docstring)


def _show_topic(ctx, name):
   """Print a topic file if present.  Returns True if found."""
   path = _find_topic(name)
   if path is None:
      return False
   f = open(path, 'rt', encoding='utf-8')
   text = f.read()
   f.close()
   ctx.outStrm.write(text)
   if not text.endswith('\n'):
      ctx.outStrm.write('\n')
   return True


# ---- apropos ----------------------------------------------------------


def _collect_matching_names(global_env, lo):
   """Names in global_env whose lowercase form contains lo."""
   result = []
   for name in global_env._bindings:
      if lo in name.lower():
         result.append(name)
   result.sort()
   return result


def _collect_matching_topics(topics, lo):
   """Topic stems whose lowercase form contains lo, each wrapped in
   double quotes."""
   result = []
   section_keys = list(topics.keys())
   section_keys.sort()
   i = 0
   while i < len(section_keys):
      stems = topics[section_keys[i]]
      j = 0
      while j < len(stems):
         if lo in stems[j].lower():
            result.append('"' + stems[j] + '"')
         j = j + 1
      i = i + 1
   result.sort()
   return result


def _apropos(ctx, env, pattern):
   """Print every global name containing `pattern` (case-insensitive)
   and every topic whose stem contains it."""
   out = ctx.outStrm
   lo  = pattern.lower()
   global_env    = env.getGlobalEnv()
   names         = _collect_matching_names(global_env, lo)
   topics        = _discover_topics()
   topic_matches = _collect_matching_topics(topics, lo)

   if not names and not topic_matches:
      out.write("No matches for '" + pattern + "'.\n")
      return
   if names:
      out.write('Names\n')
      out.write('-----\n')
      lines = _columnize(names)
      i = 0
      while i < len(lines):
         out.write(lines[i] + '\n')
         i = i + 1
      out.write('\n')
   if topic_matches:
      out.write('Topics\n')
      out.write('------\n')
      lines = _columnize(topic_matches)
      i = 0
      while i < len(lines):
         out.write(lines[i] + '\n')
         i = i + 1
      out.write('\n')


# ---- primitives -------------------------------------------------------


def _find_closure_name(global_env, closure):
   """Find the first global name bound to `closure` (by identity)."""
   for name in global_env._bindings:
      if global_env._bindings[name] is closure:
         return name
   return None


def _prim_help(ctx, env, args, app_node):
   if not args:
      _list_all(ctx, env)
      return VOID_VALUE

   target = args[0]

   if is_string(target):
      name = as_string(target)
      if _show_topic(ctx, name):
         return VOID_VALUE
      _apropos(ctx, env, name)
      return VOID_VALUE

   if is_primitive(target):
      name = as_primitive_name(target)
      if not _show_primitive_entry(ctx, name):
         ctx.outStrm.write("No help for primitive '" + name + "'.\n")
      return VOID_VALUE

   if is_closure(target):
      global_env   = env.getGlobalEnv()
      matched_name = _find_closure_name(global_env, target)
      if matched_name is None:
         matched_name = '<anonymous>'
      _show_closure_entry(ctx, matched_name, target)
      return VOID_VALUE

   if is_symbol(target):
      name = as_symbol(target)
      global_env = env.getGlobalEnv()
      if name not in global_env._bindings:
         ctx.outStrm.write("No binding for '" + name + "'.\n")
         return VOID_VALUE
      val = global_env._bindings[name]
      if is_primitive(val):
         _show_primitive_entry(ctx, as_primitive_name(val))
      elif is_closure(val):
         _show_closure_entry(ctx, name, val)
      else:
         ctx.outStrm.write(name + ': non-callable binding.\n')
      return VOID_VALUE

   raise SchemeTypeError(
      'help: argument must be a procedure, primitive, symbol, string, '
      'or omitted', app_node)


def _prim_apropos(ctx, env, args, app_node):
   target = args[0]
   if not is_string(target):
      raise SchemeTypeError('apropos: expected a string argument', app_node)
   _apropos(ctx, env, as_string(target))
   return VOID_VALUE


def register():
   register_primitive('help', (0, 1), _prim_help,
      doc=(
         "Interactive documentation.\n"
         "\n"
         "With no argument, list everything available in the global environment.\n"
         "With a callable argument, print usage and documentation for that\n"
         "procedure, primitive, or special form.  With a string argument, look up\n"
         "a help topic; if not found, perform an apropos-style substring search\n"
         "against all names and topic stems."),
      category=CATEGORY)
   register_primitive('apropos', (1, 1), _prim_apropos,
      doc=(
         "Print every global binding whose name contains the given\n"
         "substring (case-insensitive), along with any help topic whose name\n"
         "matches."),
      category=CATEGORY)
