# -*- coding: utf-8 -*-
"""
AST PATCHER — V2 (Pythonista prototype)

Features:
- Wrench-menu friendly UI: Apply (clipboard) / Dry Run / Revert / Cancel
- Root = directory of the currently open editor file (editor.get_path())
- Patches can target any file under root (including subfolders)
- Whole-run revert (revert last run)
- Run storage: patch_runs/<stamp>/ (bundle, manifest, snapshots, logs)
- Prune old runs (keep last N)
- Compile check + rollback-on-fail (per touched file, best-effort)

Patch bundle format:
- DEFAULT_FILE <path>          (optional)
- REPLACE <target>             (replace whole method / class / function)
- INSERT_AFTER <target>        (insert code block after target)
- INSERT_BEFORE <target>       (insert code block before target)
- INSERT_INTO <target>         (ANCHOR/EXPECT/MATCH/OCCURRENCE/INDENT/POSITION + code)
- REPLACE_LINE <target>        (ANCHOR/EXPECT/MATCH/OCCURRENCE + single-line replacement)
- REPLACE_LINES <target>       (ANCHOR_START/ANCHOR_END/MATCH + multi-line replacement)
- REPLACE_EXPR <target>        (ANCHOR/MATCH/OCCURRENCE/OLD/NEW — swap expression in a line)
- APPEND_INTO <target>         (append code at end of target body, no anchor needed)
- PREPEND_INTO <target>        (prepend code at start of target body, no anchor needed)
- LIST_TARGETS <file.py>       (meta-op: list all patchable targets, copies to clipboard)

Targets:
- file.py::Class.method
- file.py::Class.*             (whole class)
- file.py::function_name
- file.py::@var_name           (module-level assignment)
- file.py::Class.@var_name     (class-level assignment)
- Class.method                 (uses DEFAULT_FILE or current editor file)

Micro-targeting directives (INSERT_INTO / REPLACE_LINE / REPLACE_LINES / REPLACE_EXPR):
- ANCHOR: <substr>      line to target (substring match)
- ANCHOR_START: <substr>  start of range (REPLACE_LINES only)
- ANCHOR_END: <substr>    end of range (REPLACE_LINES only)
- MATCH: fuzzy          normalise whitespace before matching (default: exact)
- OCCURRENCE: N         use the Nth match, 1-based (default: 1)
- EXPECT: N             require exactly N hits for safety (default: 1)
- INDENT: auto|same|child  INSERT_INTO indent mode (default: auto)
- POSITION: before|after   INSERT_INTO position (default: after)
- OLD: <expr>           expression to replace (REPLACE_EXPR only)
- NEW: <expr>           replacement expression (REPLACE_EXPR only)

Notes:
- This patcher patches DISK files.
- If the currently open file is targeted and has unsaved edits, we refuse.
- DRY_RUN=True previews patches without writing anything to disk.
"""

import os
import ast
import json
import time
import hashlib
import textwrap

try:
    import clipboard
except Exception:
    clipboard = None

# Pythonista UI modules (optional at runtime)
try:
    import console
except Exception:
    console = None

try:
    import editor
except Exception:
    editor = None

try:
    import dialogs
except Exception:
    dialogs = None


# =========================
# CONFIG
# =========================
RUNS_DIRNAME = "patch_runs"
KEEP_RUNS = 5

ROLLBACK_ON_COMPILE_FAIL = True
DEFAULT_CONTEXT_LINES = 25

PRINT_OP_LINES_TO_CONSOLE = True
ALWAYS_COPY_RUN_PACKET = True

DRY_RUN = False          # Set True to preview patches without writing to disk


# =========================
# UTIL
# =========================
def now_stamp():
    return time.strftime("%Y%m%d_%H%M%S")

def sha256_text(s):
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()

def ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path)

def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_text(path, text):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def get_line_indent(line):
    return line[:len(line) - len(line.lstrip())]

def smoke_compile(source, filename="<patched>"):
    compile(source, filename, "exec")
    return True

def get_excerpt(source, line1, line2, context=DEFAULT_CONTEXT_LINES):
    lines = source.splitlines()
    n = len(lines)
    a = max(1, line1 - context)
    b = min(n, line2 + context)
    out = []
    for i in range(a, b + 1):
        prefix = ">> " if (line1 <= i <= line2) else "   "
        out.append(f"{prefix}{i:04d}: {lines[i-1]}")
    return "\n".join(out)

def _hud(msg, style="success", d=1.0):
    if console:
        try:
            console.hud_alert(msg, style, d)
        except Exception:
            pass
    else:
        print(msg)

def _alert(title, message, *buttons):
    # returns 1..n
    if console:
        try:
            return console.alert(title, message, *buttons)
        except Exception:
            return 1
    print(title + ":", message)
    return 1

def _editor_path():
    if editor:
        try:
            return editor.get_path()
        except Exception:
            return None
    return None

def _editor_text():
    if editor:
        try:
            return editor.get_text() or ""
        except Exception:
            return ""
    return ""

def _editor_replace_all(text):
    if editor:
        try:
            cur = editor.get_text() or ""
            editor.replace_text(0, len(cur), text)
            return True
        except Exception:
            return False
    return False


# =========================
# AST LOCATOR
# =========================
def supports_end_lineno():
    src = "def f():\n    return 1\n"
    t = ast.parse(src)
    fn = t.body[0]
    return hasattr(fn, "end_lineno") and fn.end_lineno is not None

def find_method_range(source, class_name, method_name):
    """
    Return (start_line, end_line) 1-based inclusive for a method inside a top-level class.

    Strategy:
    - start_line includes decorators
    - end_line prefers "next sibling lineno - 1" (prevents wiping inserted methods)
    - falls back to node.end_lineno for last sibling
    """
    tree = ast.parse(source)
    matches = []

    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue

        items = [it for it in node.body if getattr(it, "lineno", None) is not None]

        for idx, it in enumerate(items):
            if not isinstance(it, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if it.name != method_name:
                continue

            start_line = it.lineno
            for d in getattr(it, "decorator_list", []) or []:
                dl = getattr(d, "lineno", None)
                if dl is not None:
                    start_line = min(start_line, dl)

            end_line = getattr(it, "end_lineno", None)

            if idx + 1 < len(items):
                next_it = items[idx + 1]
                next_line = getattr(next_it, "lineno", None)
                if next_line is not None and next_line > start_line:
                    end_line = next_line - 1

            if end_line is None:
                raise RuntimeError("end_lineno not available; cannot locate method end reliably.")

            matches.append((start_line, end_line))

    if not matches:
        return None
    if len(matches) > 1:
        return ("AMBIGUOUS", matches)
    return matches[0]

def find_class_range(source, class_name):
    tree = ast.parse(source)
    matches = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            if getattr(node, "lineno", None) is None:
                continue
            if getattr(node, "end_lineno", None) is None:
                raise RuntimeError("end_lineno not available; cannot locate class end reliably.")
            matches.append((node.lineno, node.end_lineno))
    if not matches:
        return None
    if len(matches) > 1:
        return ("AMBIGUOUS", matches)
    return matches[0]

def find_function_range(source, func_name):
    tree = ast.parse(source)
    matches = []
    items = [n for n in tree.body if getattr(n, 'lineno', None) is not None]
    for idx, node in enumerate(items):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        start_line = node.lineno
        for d in getattr(node, 'decorator_list', []) or []:
            dl = getattr(d, 'lineno', None)
            if dl is not None:
                start_line = min(start_line, dl)
        end_line = getattr(node, 'end_lineno', None)
        if idx + 1 < len(items):
            next_node = items[idx + 1]
            next_line = getattr(next_node, 'lineno', None)
            if next_line is not None and next_line > start_line:
                end_line = next_line - 1
        if end_line is None:
            raise RuntimeError('end_lineno not available; cannot locate function end reliably.')
        matches.append((start_line, end_line))
    if not matches:
        return None
    if len(matches) > 1:
        return ('AMBIGUOUS', matches)
    return matches[0]


# =========================
# TEXT APPLY
# =========================

def find_global_assign_range(source, var_name):
    """
    Return (start_line, end_line) 1-based inclusive for a *module-level* assignment:

        NAME = ...
        NAME: T = ...

    Notes:
    - Only scans top-level statements (tree.body)
    - Supports multi-line RHS via end_lineno
    - If multiple matches exist, returns ("AMBIGUOUS", matches)
    """
    tree = ast.parse(source)
    matches = []

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in (node.targets or []):
                if isinstance(t, ast.Name) and t.id == var_name:
                    if getattr(node, "lineno", None) is None:
                        continue
                    end_line = getattr(node, "end_lineno", None)
                    if end_line is None:
                        raise RuntimeError("end_lineno not available; cannot locate assignment end reliably.")
                    matches.append((node.lineno, end_line))
                    break

        elif isinstance(node, ast.AnnAssign):
            t = getattr(node, "target", None)
            if isinstance(t, ast.Name) and t.id == var_name:
                if getattr(node, "lineno", None) is None:
                    continue
                end_line = getattr(node, "end_lineno", None)
                if end_line is None:
                    raise RuntimeError("end_lineno not available; cannot locate assignment end reliably.")
                matches.append((node.lineno, end_line))

    if not matches:
        return None
    if len(matches) > 1:
        return ("AMBIGUOUS", matches)
    return matches[0]

def find_class_assign_range(source, class_name, var_name):
    """
    Return (start_line, end_line) 1-based inclusive for a *class-level* assignment:

        class C:
            NAME = ...
            NAME: T = ...

    Notes:
    - Only scans direct class body statements (no nested classes)
    - Supports multi-line RHS via end_lineno
    - If multiple matches exist, returns ("AMBIGUOUS", matches)
    """
    tree = ast.parse(source)
    matches = []

    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue

        for item in (node.body or []):
            # NAME = ...
            if isinstance(item, ast.Assign):
                for t in (item.targets or []):
                    if isinstance(t, ast.Name) and t.id == var_name:
                        if getattr(item, "lineno", None) is None:
                            continue
                        end_line = getattr(item, "end_lineno", None)
                        if end_line is None:
                            raise RuntimeError("end_lineno not available; cannot locate assignment end reliably.")
                        matches.append((item.lineno, end_line))
                        break

            # NAME: T = ...
            elif isinstance(item, ast.AnnAssign):
                t = getattr(item, "target", None)
                if isinstance(t, ast.Name) and t.id == var_name:
                    if getattr(item, "lineno", None) is None:
                        continue
                    end_line = getattr(item, "end_lineno", None)
                    if end_line is None:
                        raise RuntimeError("end_lineno not available; cannot locate assignment end reliably.")
                    matches.append((item.lineno, end_line))

    if not matches:
        return None
    if len(matches) > 1:
        return ("AMBIGUOUS", matches)
    return matches[0]

def replace_lines(source, start_line, end_line, replacement_block):
    lines = source.splitlines(True)
    if not lines:
        return (replacement_block or '').strip('\n') + '\n'

    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError('Invalid line range %d..%d for %d-line source' % (start_line, end_line, len(lines)))

    before = lines[:start_line - 1]
    after  = lines[end_line:]

    indent = get_line_indent(lines[start_line - 1])
    replacement_block = textwrap.dedent((replacement_block or '').strip('\n'))

    new_lines = []
    for line in replacement_block.splitlines():
        if line.strip():
            new_lines.append(indent + line + '\n')
        else:
            new_lines.append('\n')

    # ensure blank line after block if next line is not already blank
    if after and after[0].strip():
        new_lines.append('\n')

    return ''.join(before) + ''.join(new_lines) + ''.join(after)

def insert_after_lines(source, line_no, insert_block, indent, tight=False):
    """
    Insert after line_no (1-based line count in splitlines(True) world: we accept line_no in that space).
    If tight=True, do not auto-add blank lines before/after insert block.
    """
    src_lines = source.splitlines(True)

    if line_no < 0 or line_no > len(src_lines):
        raise ValueError('Invalid insert position after line %d in %d-line source' % (line_no, len(src_lines)))

    insert_block = textwrap.dedent((insert_block or '').strip('\n'))

    ins_lines = []
    for line in insert_block.splitlines():
        if line.strip():
            ins_lines.append(indent + line + '\n')
        else:
            ins_lines.append('\n')

    before = src_lines[:line_no]
    after  = src_lines[line_no:]

    if not tight:
        if before and before[-1].strip():
            ins_lines.insert(0, '\n')
        if after and after[0].strip():
            ins_lines.append('\n')

    if src_lines and not src_lines[-1].endswith('\n'):
        src_lines[-1] = src_lines[-1] + '\n'

    return ''.join(before) + ''.join(ins_lines) + ''.join(after)

def parse_target(raw_target, default_file_abs, op_default_file=None):
    raw_target = (raw_target or '').strip()
    file_ref = None
    sym = None

    if '::' in raw_target:
        file_part, sym_part = raw_target.split('::', 1)
        file_ref = file_part.strip()
        sym = sym_part.strip()
    else:
        sym = raw_target
        file_ref = op_default_file or default_file_abs

    if not sym:
        raise ValueError('Bad target: ' + repr(raw_target))

    if '.' in sym:
        class_name, method_name = sym.split('.', 1)
        class_name = class_name.strip()
        method_name = method_name.strip()
        if not class_name or not method_name:
            raise ValueError('Bad target: ' + repr(raw_target))
        return file_ref, class_name, method_name
    else:
        return file_ref, None, sym

def parse_patch_bundle(text):
    if not text or not text.strip():
        return [], None

    lines = text.splitlines()
    ops = []
    i = 0
    bundle_default_file = None

    def is_op_header(line):
        s = line.strip()
        return (s.startswith('REPLACE ') or
                s.startswith('REPLACE_LINE ') or
                s.startswith('REPLACE_LINES ') or
                s.startswith('REPLACE_EXPR ') or
                s.startswith('INSERT_AFTER ') or
                s.startswith('INSERT_BEFORE ') or
                s.startswith('INSERT_INTO ') or
                s.startswith('APPEND_INTO ') or
                s.startswith('PREPEND_INTO ') or
                s.startswith('LIST_TARGETS '))

    def is_default_file(line):
        return line.strip().startswith('DEFAULT_FILE ')

    def first_sig(body):
        for ln in (body or '').splitlines():
            if ln.strip():
                return ln.strip()
        return ''

    def parse_line_op_body(body_lines):
        anchor = None
        anchor_start = None
        anchor_end = None
        expect = 1
        occurrence = 1
        match_mode = 'exact'
        indent_mode = 'auto'
        position = 'after'
        old_expr = None
        new_expr = None
        code_lines = []
        in_code = False
        for ln in body_lines:
            if not in_code:
                s = ln.strip()
                if s.startswith('ANCHOR:'):
                    anchor = s[len('ANCHOR:'):].strip()
                elif s.startswith('ANCHOR_START:'):
                    anchor_start = s[len('ANCHOR_START:'):].strip()
                elif s.startswith('ANCHOR_END:'):
                    anchor_end = s[len('ANCHOR_END:'):].strip()
                elif s.startswith('EXPECT:'):
                    try:
                        expect = int(s[len('EXPECT:'):].strip())
                    except ValueError:
                        expect = 1
                elif s.startswith('OCCURRENCE:'):
                    try:
                        occurrence = int(s[len('OCCURRENCE:'):].strip())
                    except ValueError:
                        occurrence = 1
                elif s.startswith('MATCH:'):
                    match_mode = s[len('MATCH:'):].strip().lower()
                elif s.startswith('INDENT:'):
                    indent_mode = s[len('INDENT:'):].strip().lower()
                elif s.startswith('POSITION:'):
                    position = s[len('POSITION:'):].strip().lower()
                elif s.startswith('OLD:'):
                    old_expr = s[len('OLD:'):].strip()
                elif s.startswith('NEW:'):
                    new_expr = s[len('NEW:'):].strip()
                elif s:
                    in_code = True
                    code_lines.append(ln)
            else:
                code_lines.append(ln)
        code = '\n'.join(code_lines).rstrip() + '\n' if code_lines else ''
        return (anchor, anchor_start, anchor_end, expect, occurrence, match_mode,
                indent_mode, position, old_expr, new_expr, code)

    while i < len(lines):
        while i < len(lines) and not lines[i].strip():
            i += 1
        if i >= len(lines):
            break

        line = lines[i].rstrip('\n')

        if is_default_file(line):
            bundle_default_file = line.strip()[len('DEFAULT_FILE '):].strip() or None
            i += 1
            continue

        if not is_op_header(line):
            raise ValueError('Patch parse error: expected op header at line ' + str(i+1) + ': ' + repr(line))

        s = line.strip()
        if s.startswith('REPLACE_LINES '):
            op = 'REPLACE_LINES'
            target = s[len('REPLACE_LINES '):].strip()
        elif s.startswith('REPLACE_LINE '):
            op = 'REPLACE_LINE'
            target = s[len('REPLACE_LINE '):].strip()
        elif s.startswith('REPLACE_EXPR '):
            op = 'REPLACE_EXPR'
            target = s[len('REPLACE_EXPR '):].strip()
        elif s.startswith('REPLACE '):
            op = 'REPLACE'
            target = s[len('REPLACE '):].strip()
        elif s.startswith('INSERT_AFTER '):
            op = 'INSERT_AFTER'
            target = s[len('INSERT_AFTER '):].strip()
        elif s.startswith('INSERT_BEFORE '):
            op = 'INSERT_BEFORE'
            target = s[len('INSERT_BEFORE '):].strip()
        elif s.startswith('INSERT_INTO '):
            op = 'INSERT_INTO'
            target = s[len('INSERT_INTO '):].strip()
        elif s.startswith('APPEND_INTO '):
            op = 'APPEND_INTO'
            target = s[len('APPEND_INTO '):].strip()
        elif s.startswith('PREPEND_INTO '):
            op = 'PREPEND_INTO'
            target = s[len('PREPEND_INTO '):].strip()
        elif s.startswith('LIST_TARGETS '):
            op = 'LIST_TARGETS'
            target = s[len('LIST_TARGETS '):].strip()
        else:
            op = 'REPLACE'
            target = s

        i += 1

        body_lines = []
        while i < len(lines) and not is_op_header(lines[i]) and not is_default_file(lines[i]):
            body_lines.append(lines[i])
            i += 1

        if op in ('INSERT_INTO', 'REPLACE_LINE', 'REPLACE_LINES', 'REPLACE_EXPR'):
            (anchor, anchor_start, anchor_end, expect, occurrence, match_mode,
             indent_mode, position, old_expr, new_expr, code) = parse_line_op_body(body_lines)
            ops.append({
                'op': op,
                'target': target,
                'body': '\n'.join(body_lines).rstrip() + '\n' if body_lines else '',
                'sig': first_sig(code) if code else '',
                'default_file': bundle_default_file,
                'anchor': anchor,
                'anchor_start': anchor_start,
                'anchor_end': anchor_end,
                'expect': expect,
                'occurrence': occurrence,
                'match_mode': match_mode,
                'indent_mode': indent_mode,
                'position': position,
                'old_expr': old_expr,
                'new_expr': new_expr,
                'code': code,
            })
        else:
            body = '\n'.join(body_lines).rstrip() + '\n' if body_lines else ''
            ops.append({
                'op': op,
                'target': target,
                'body': body,
                'sig': first_sig(body),
                'default_file': bundle_default_file,
            })

    return ops, bundle_default_file


# =========================
# APPLY OPS
# =========================
def apply_ops(ops, project_root, default_file_abs):
    import re

    results = []
    touched_files = {}   # file_abs -> meta
    file_cache = {}      # file_abs -> in-memory updated source

    root_abs = os.path.abspath(project_root)

    for op in ops:
        results.append({
            'op': op.get('op'),
            'target': op.get('target'),
            'status': None,
            'file': None,
            'range': None,
            'hash_before': None,
            'hash_after': None,
            'compile_ok': None,
            'message': '',
            'sig': op.get('sig', '')
        })

    def _first_sig_line(body, fallback_sig=''):
        s = (fallback_sig or '').strip()
        if s:
            return s
        for ln in (body or '').splitlines():
            if ln.strip():
                return ln.strip()
        return ''

    def _def_name_from_sig(sig_line):
        m = re.match(r'^\s*def\s+([A-Za-z_]\w*)\s*\(', sig_line or '')
        return m.group(1) if m else None

    def _has_def_anywhere(src, name):
        pat = r'^\s*def\s+' + re.escape(name) + r'\s*\('
        return re.search(pat, src, flags=re.M) is not None

    def _locate(src, class_name, method_name):
        # Whole-class target support: Class.*  (e.g. file.py::MyClass.*)
        if class_name is not None and method_name == '*':
            return find_class_range(src, class_name)

        # Class/module assignment support via @NAME
        if class_name is not None and isinstance(method_name, str) and method_name.startswith('@'):
            return find_class_assign_range(src, class_name, method_name[1:])
        if class_name is None and isinstance(method_name, str) and method_name.startswith('@'):
            return find_global_assign_range(src, method_name[1:])

        # Function vs method
        if class_name is None:
            return find_function_range(src, method_name)
        return find_method_range(src, class_name, method_name)

    def _find_anchor_hits(src_lines, block_start, block_end, anchor, match_mode='exact'):
        anchor_cmp = ' '.join(anchor.split()) if match_mode == 'fuzzy' else anchor
        hits = []
        for i, line in enumerate(src_lines[block_start - 1:block_end]):
            line_cmp = ' '.join(line.split()) if match_mode == 'fuzzy' else line
            if anchor_cmp in line_cmp:
                hits.append((block_start + i, line))
        return hits

    def _anchor_mismatch_msg(anchor, hits_count, expect, src_lines, start_line, end_line):
        block_lines = src_lines[start_line - 1 : min(start_line + 7, end_line)]
        excerpt = '\n'.join('  ' + l for l in block_lines)
        return ('ANCHOR %r matched %d times, expected %d.\nBlock starts:\n%s'
                % (anchor, hits_count, expect, excerpt))

    for idx, op in enumerate(ops):
        rec = results[idx]
        op_kind = op.get('op')
        try:
            # LIST_TARGETS is a meta-op: only needs a file path, no class/method
            if op_kind == 'LIST_TARGETS':
                target_raw = (op.get('target') or '').strip()
                if '::' in target_raw:
                    file_ref = target_raw.split('::', 1)[0].strip()
                else:
                    file_ref = target_raw or default_file_abs
                file_abs = os.path.realpath(os.path.abspath(
                    file_ref if os.path.isabs(file_ref)
                    else os.path.join(project_root, file_ref)
                ))
                if not (file_abs == root_abs or file_abs.startswith(root_abs + os.sep)):
                    rec['status'] = 'FAILED_INVALID_PATH'
                    rec['message'] = 'Target file escapes project root'
                    continue
                if not os.path.isfile(file_abs):
                    rec['status'] = 'FAILED_IO'
                    rec['message'] = 'File not found: ' + file_abs
                    continue
                rec['file'] = os.path.relpath(file_abs, project_root)
                src = file_cache.get(file_abs) or read_text(file_abs)
                try:
                    tree = ast.parse(src)
                except SyntaxError as e:
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'SyntaxError: ' + str(e)
                    continue
                file_rel = rec['file']
                targets_found = []
                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        targets_found.append(file_rel + '::' + node.name + '.*')
                        for item in node.body:
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                targets_found.append(file_rel + '::' + node.name + '.' + item.name)
                            elif isinstance(item, ast.Assign):
                                for t in (item.targets or []):
                                    if isinstance(t, ast.Name):
                                        targets_found.append(file_rel + '::' + node.name + '.@' + t.id)
                            elif isinstance(item, ast.AnnAssign):
                                t = getattr(item, 'target', None)
                                if isinstance(t, ast.Name):
                                    targets_found.append(file_rel + '::' + node.name + '.@' + t.id)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        targets_found.append(file_rel + '::' + node.name)
                    elif isinstance(node, ast.Assign):
                        for t in (node.targets or []):
                            if isinstance(t, ast.Name):
                                targets_found.append(file_rel + '::@' + t.id)
                    elif isinstance(node, ast.AnnAssign):
                        t = getattr(node, 'target', None)
                        if isinstance(t, ast.Name):
                            targets_found.append(file_rel + '::@' + t.id)
                targets_str = '\n'.join(targets_found)
                rec['status'] = 'APPLIED'
                rec['message'] = 'Targets found: %d' % len(targets_found)
                rec['hash_before'] = sha256_text(src)
                rec['hash_after'] = rec['hash_before']
                if clipboard is not None:
                    try:
                        clipboard.set(targets_str)
                    except Exception:
                        pass
                continue

            file_ref, class_name, method_name = parse_target(
                op.get('target'),
                default_file_abs,
                op_default_file=op.get('default_file')
            )

            file_abs = os.path.realpath(os.path.abspath(
                file_ref if os.path.isabs(file_ref)
                else os.path.join(project_root, file_ref)
            ))

            if not (file_abs == root_abs or file_abs.startswith(root_abs + os.sep)):
                rec['status'] = 'FAILED_INVALID_PATH'
                rec['message'] = 'Target file escapes project root'
                continue

            if not os.path.isfile(file_abs):
                rec['status'] = 'FAILED_IO'
                rec['message'] = 'File not found: ' + file_abs
                continue

            rec['file'] = os.path.relpath(file_abs, project_root)

            src = file_cache.get(file_abs)
            if src is None:
                src = read_text(file_abs)
                file_cache[file_abs] = src

            if file_abs not in touched_files:
                touched_files[file_abs] = {
                    'before': src,
                    'after': None,
                    'compile_ok': None,
                    'compile_error': ''
                }

            found = _locate(src, class_name, method_name)
            if found is None:
                rec['status'] = 'FAILED_NOT_FOUND'
                rec['message'] = 'Target not found: ' + str(class_name) + '.' + str(method_name)
                continue
            if isinstance(found, tuple) and len(found) == 2 and found[0] == 'AMBIGUOUS':
                rec['status'] = 'FAILED_AMBIGUOUS'
                rec['message'] = 'Ambiguous matches: ' + str(found[1])
                continue

            start_line, end_line = found
            rec['range'] = [start_line, end_line]

            src_lines = src.splitlines()
            before_block = '\n'.join(src_lines[start_line - 1:end_line]) + '\n'
            rec['hash_before'] = sha256_text(before_block)

            op_kind = op.get('op')

            if op_kind == 'REPLACE':
                patched = replace_lines(src, start_line, end_line, op.get('body'))
                if sha256_text(patched) == sha256_text(src):
                    rec['status'] = 'SKIPPED_ALREADY_APPLIED'
                    rec['hash_after'] = rec['hash_before']
                    continue
                file_cache[file_abs] = patched
                rec['status'] = 'APPLIED'
                rec['hash_after'] = sha256_text(patched)

            elif op_kind in ('INSERT_AFTER', 'INSERT_BEFORE'):
                if op_kind == 'INSERT_AFTER':
                    insert_line = end_line
                    ref_line = src.splitlines(True)[start_line - 1]
                else:
                    insert_line = start_line - 1
                    ref_line = src.splitlines(True)[start_line - 1]

                indent = get_line_indent(ref_line)

                sig_line = _first_sig_line(op.get('body'), op.get('sig'))
                def_name = _def_name_from_sig(sig_line)

                if def_name and _has_def_anywhere(src, def_name):
                    rec['status'] = 'SKIPPED_ALREADY_PRESENT'
                    rec['hash_after'] = rec['hash_before']
                    continue
                if (not def_name) and sig_line and (sig_line in src):
                    rec['status'] = 'SKIPPED_ALREADY_PRESENT'
                    rec['hash_after'] = rec['hash_before']
                    continue

                patched = insert_after_lines(src, insert_line, op.get('body'), indent, tight=False)
                if sha256_text(patched) == sha256_text(src):
                    rec['status'] = 'SKIPPED_ALREADY_PRESENT'
                    rec['hash_after'] = rec['hash_before']
                    continue

                file_cache[file_abs] = patched
                rec['status'] = 'APPLIED'
                rec['hash_after'] = sha256_text(patched)

            elif op_kind == 'INSERT_INTO':
                anchor = op.get('anchor')
                expect = op.get('expect', 1)
                occurrence = op.get('occurrence', 1)
                match_mode = op.get('match_mode', 'exact')
                indent_mode = op.get('indent_mode', 'auto')
                position = op.get('position', 'after')
                code = op.get('code', '')

                if not anchor:
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'INSERT_INTO requires ANCHOR'
                    continue

                hits = _find_anchor_hits(src.splitlines(), start_line, end_line, anchor, match_mode)
                if len(hits) != expect:
                    rec['status'] = 'SKIPPED_ANCHOR_MISMATCH'
                    rec['message'] = _anchor_mismatch_msg(anchor, len(hits), expect, src.splitlines(), start_line, end_line)
                    continue

                if occurrence < 1 or occurrence > len(hits):
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'OCCURRENCE %d out of range (1..%d)' % (occurrence, len(hits))
                    continue

                anchor_lineno, anchor_line_text = hits[occurrence - 1]
                anchor_indent = get_line_indent(anchor_line_text)

                if indent_mode == 'same':
                    insert_indent = anchor_indent
                elif indent_mode == 'child':
                    opens_block = anchor_line_text.rstrip().endswith(':')
                    has_deeper = any(
                        bl.strip() and len(get_line_indent(bl)) > len(anchor_indent)
                        for bl in src.splitlines()[anchor_lineno:end_line]
                    )
                    if not opens_block and not has_deeper:
                        rec['status'] = 'FAILED_PARSE'
                        rec['message'] = 'INDENT: child refused - anchor does not open a block'
                        continue
                    insert_indent = anchor_indent + '    '
                else:
                    if anchor_line_text.rstrip().endswith(':'):
                        insert_indent = anchor_indent + '    '
                    else:
                        insert_indent = anchor_indent

                sig_line = (op.get('sig') or '').strip()
                if sig_line and sig_line in src:
                    rec['status'] = 'SKIPPED_ALREADY_PRESENT'
                    rec['hash_after'] = rec['hash_before']
                    continue

                if position == 'before':
                    insert_line = anchor_lineno - 1
                else:
                    insert_line = anchor_lineno

                patched = insert_after_lines(src, insert_line, code, insert_indent, tight=True)
                if sha256_text(patched) == sha256_text(src):
                    rec['status'] = 'SKIPPED_ALREADY_PRESENT'
                    rec['hash_after'] = rec['hash_before']
                    continue

                file_cache[file_abs] = patched
                rec['status'] = 'APPLIED'
                rec['hash_after'] = sha256_text(patched)

            elif op_kind == 'REPLACE_LINE':
                anchor = op.get('anchor')
                expect = op.get('expect', 1)
                occurrence = op.get('occurrence', 1)
                match_mode = op.get('match_mode', 'exact')
                code = (op.get('code', '') or '').strip()

                if not anchor:
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'REPLACE_LINE requires ANCHOR'
                    continue

                src_lines_raw = src.splitlines(True)
                hits = _find_anchor_hits([ln.rstrip("\n") for ln in src_lines_raw], start_line, end_line, anchor, match_mode)

                if len(hits) != expect:
                    rec['status'] = 'SKIPPED_ANCHOR_MISMATCH'
                    rec['message'] = _anchor_mismatch_msg(anchor, len(hits), expect, src.splitlines(), start_line, end_line)
                    continue

                if occurrence < 1 or occurrence > len(hits):
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'OCCURRENCE %d out of range (1..%d)' % (occurrence, len(hits))
                    continue

                anchor_lineno, anchor_line_text = hits[occurrence - 1]
                line_indent = get_line_indent(anchor_line_text)
                new_line = line_indent + code + '\n'

                if new_line.rstrip() == anchor_line_text.rstrip():
                    rec['status'] = 'SKIPPED_ALREADY_APPLIED'
                    rec['hash_after'] = rec['hash_before']
                    continue

                new_src_lines = src_lines_raw[:anchor_lineno - 1] + [new_line] + src_lines_raw[anchor_lineno:]
                patched = ''.join(new_src_lines)

                file_cache[file_abs] = patched
                rec['status'] = 'APPLIED'
                rec['hash_after'] = sha256_text(patched)

            elif op_kind == 'REPLACE_LINES':
                anchor_start = op.get('anchor_start')
                anchor_end = op.get('anchor_end')
                match_mode = op.get('match_mode', 'exact')
                code = op.get('code', '')

                if not anchor_start or not anchor_end:
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'REPLACE_LINES requires ANCHOR_START and ANCHOR_END'
                    continue

                src_lines_stripped = [ln.rstrip("\n") for ln in src.splitlines(True)]
                hits_s = _find_anchor_hits(src_lines_stripped, start_line, end_line, anchor_start, match_mode)
                hits_e = _find_anchor_hits(src_lines_stripped, start_line, end_line, anchor_end, match_mode)

                if len(hits_s) != 1:
                    rec['status'] = 'SKIPPED_ANCHOR_MISMATCH'
                    rec['message'] = 'ANCHOR_START %r matched %d times, expected 1' % (anchor_start, len(hits_s))
                    continue
                if len(hits_e) != 1:
                    rec['status'] = 'SKIPPED_ANCHOR_MISMATCH'
                    rec['message'] = 'ANCHOR_END %r matched %d times, expected 1' % (anchor_end, len(hits_e))
                    continue

                line_s, _ = hits_s[0]
                line_e, _ = hits_e[0]
                if line_e < line_s:
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'ANCHOR_END appears before ANCHOR_START in source'
                    continue

                patched = replace_lines(src, line_s, line_e, code)
                if sha256_text(patched) == sha256_text(src):
                    rec['status'] = 'SKIPPED_ALREADY_APPLIED'
                    rec['hash_after'] = rec['hash_before']
                    continue
                file_cache[file_abs] = patched
                rec['status'] = 'APPLIED'
                rec['hash_after'] = sha256_text(patched)

            elif op_kind in ('APPEND_INTO', 'PREPEND_INTO'):
                code = op.get('body', '')
                src_lines_list = src.splitlines(True)
                block_lines = src_lines_list[start_line - 1 : end_line]

                if op_kind == 'PREPEND_INTO':
                    # Insert right after the def/class header line
                    ref_line = src_lines_list[start_line - 1] if src_lines_list else ''
                    insert_indent = get_line_indent(ref_line) + '    '
                    insert_pos = start_line
                else:
                    # Find last non-blank line in block, insert after it
                    last_content_idx = None
                    for i, ln in enumerate(block_lines):
                        if ln.strip():
                            last_content_idx = i
                    if last_content_idx is None:
                        last_content_idx = len(block_lines) - 1
                    ref_line = block_lines[last_content_idx] if block_lines else ''
                    insert_indent = get_line_indent(ref_line)
                    insert_pos = start_line - 1 + last_content_idx + 1

                sig_line = (op.get('sig') or '').strip()
                if sig_line and sig_line in src:
                    rec['status'] = 'SKIPPED_ALREADY_PRESENT'
                    rec['hash_after'] = rec['hash_before']
                    continue

                patched = insert_after_lines(src, insert_pos, code, insert_indent, tight=True)
                if sha256_text(patched) == sha256_text(src):
                    rec['status'] = 'SKIPPED_ALREADY_PRESENT'
                    rec['hash_after'] = rec['hash_before']
                    continue
                file_cache[file_abs] = patched
                rec['status'] = 'APPLIED'
                rec['hash_after'] = sha256_text(patched)

            elif op_kind == 'REPLACE_EXPR':
                anchor = op.get('anchor')
                expect = op.get('expect', 1)
                occurrence = op.get('occurrence', 1)
                match_mode = op.get('match_mode', 'exact')
                old_expr = op.get('old_expr')
                new_expr = op.get('new_expr')

                if not anchor:
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'REPLACE_EXPR requires ANCHOR'
                    continue
                if old_expr is None or new_expr is None:
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'REPLACE_EXPR requires OLD and NEW'
                    continue

                src_lines_raw = src.splitlines(True)
                hits = _find_anchor_hits([ln.rstrip("\n") for ln in src_lines_raw], start_line, end_line, anchor, match_mode)

                if len(hits) != expect:
                    rec['status'] = 'SKIPPED_ANCHOR_MISMATCH'
                    rec['message'] = _anchor_mismatch_msg(anchor, len(hits), expect, src.splitlines(), start_line, end_line)
                    continue
                if occurrence < 1 or occurrence > len(hits):
                    rec['status'] = 'FAILED_PARSE'
                    rec['message'] = 'OCCURRENCE %d out of range (1..%d)' % (occurrence, len(hits))
                    continue

                anchor_lineno, anchor_line_text = hits[occurrence - 1]
                if old_expr not in anchor_line_text:
                    rec['status'] = 'SKIPPED_ANCHOR_MISMATCH'
                    rec['message'] = 'OLD %r not found in anchor line: %r' % (old_expr, anchor_line_text.strip())
                    continue

                new_line_text = anchor_line_text.replace(old_expr, new_expr, 1)
                if new_line_text == anchor_line_text:
                    rec['status'] = 'SKIPPED_ALREADY_APPLIED'
                    rec['hash_after'] = rec['hash_before']
                    continue

                if not new_line_text.endswith('\n'):
                    new_line_text += '\n'
                new_src_lines = src_lines_raw[:anchor_lineno - 1] + [new_line_text] + src_lines_raw[anchor_lineno:]
                patched = ''.join(new_src_lines)
                file_cache[file_abs] = patched
                rec['status'] = 'APPLIED'
                rec['hash_after'] = sha256_text(patched)

            else:
                rec['status'] = 'FAILED_PARSE'
                rec['message'] = 'Unsupported op: ' + str(op_kind)

        except Exception as e:
            rec['status'] = 'FAILED_PARSE'
            rec['message'] = type(e).__name__ + ': ' + str(e)

    return results, touched_files, file_cache


# =========================
# RUN STORAGE (v2)
# =========================
def runs_root(project_root):
    return os.path.join(project_root, RUNS_DIRNAME)

def list_runs(project_root):
    rr = runs_root(project_root)
    if not os.path.isdir(rr):
        return []
    items = []
    for name in os.listdir(rr):
        p = os.path.join(rr, name)
        if os.path.isdir(p):
            items.append(name)
    items.sort(reverse=True)
    return items

def prune_runs(project_root, keep_n=KEEP_RUNS):
    rr = runs_root(project_root)
    if not os.path.isdir(rr):
        return
    runs = list_runs(project_root)
    if keep_n <= 0 or len(runs) <= keep_n:
        return
    old = runs[keep_n:]
    for name in old:
        p = os.path.join(rr, name)
        try:
            # recursive delete
            for root, dirs, files in os.walk(p, topdown=False):
                for fn in files:
                    try: os.remove(os.path.join(root, fn))
                    except Exception: pass
                for dn in dirs:
                    try: os.rmdir(os.path.join(root, dn))
                    except Exception: pass
            try: os.rmdir(p)
            except Exception: pass
        except Exception:
            pass

def write_run_artifacts(project_root, stamp, bundle_text, results, touched_files, file_cache):
    run_dir = os.path.join(runs_root(project_root), stamp)
    snap_dir = os.path.join(run_dir, "snapshots")
    log_dir = os.path.join(run_dir, "logs")
    ensure_dir(snap_dir)
    ensure_dir(log_dir)

    # Save bundle
    write_text(os.path.join(run_dir, "bundle.txt"), (bundle_text or "").strip() + "\n")

    # Save snapshots (BEFORE for touched files)
    touched_list = []
    for file_abs, meta in touched_files.items():
        rel = os.path.relpath(file_abs, project_root)
        snap_path = os.path.join(snap_dir, rel)
        write_text(snap_path, meta.get("before") or "")
        touched_list.append({
            "rel": rel,
            "snapshot_rel": os.path.relpath(snap_path, run_dir),
            "before_sha": sha256_text(meta.get("before") or ""),
            "after_sha": sha256_text(file_cache.get(os.path.realpath(file_abs), meta.get("before") or "")),
            "compile_ok": meta.get("compile_ok"),
            "compile_error": meta.get("compile_error", "")
        })

    # Logs
    summary_lines = []
    summary_lines.append(f"Run: {stamp}")
    summary_lines.append(f"Root: {os.path.abspath(project_root)}")
    summary_lines.append("")
    for r in results:
        st = r.get("status") or "UNKNOWN"
        summary_lines.append(f"{st:22} {r.get('op','?'):12} {r.get('target','?')}  [{r.get('file','?')}]  {r.get('message','')}".rstrip())
    summary_lines.append("")
    summary_path = os.path.join(log_dir, "run_summary.txt")
    write_text(summary_path, "\n".join(summary_lines) + "\n")

    jsonl_path = os.path.join(log_dir, "run_log.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Manifest
    manifest = {
        "stamp": stamp,
        "root": os.path.abspath(project_root),
        "bundle_sha": sha256_text(bundle_text or ""),
        "touched": touched_list,
        "results": results,
    }
    write_text(os.path.join(run_dir, "manifest.json"), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    return run_dir, summary_path, jsonl_path

def verify_write_and_maybe_rollback(touched_files, file_cache):
    """
    Write updated files and compile check. Rollback per-file on compile failure if configured.
    """
    for file_abs, meta in touched_files.items():
        real_abs = os.path.realpath(file_abs)
        new_src = file_cache.get(real_abs, file_cache.get(file_abs, meta["before"]))
        meta["after"] = new_src

        try:
            write_text(file_abs, new_src)
        except Exception as e:
            meta["compile_ok"] = False
            meta["compile_error"] = "WRITE_FAIL " + type(e).__name__ + ": " + str(e)
            if ROLLBACK_ON_COMPILE_FAIL:
                try:
                    write_text(file_abs, meta["before"])
                except Exception:
                    pass
            continue

        try:
            disk_src = read_text(file_abs)
        except Exception as e:
            meta["compile_ok"] = False
            meta["compile_error"] = "READBACK_FAIL " + type(e).__name__ + ": " + str(e)
            if ROLLBACK_ON_COMPILE_FAIL:
                try:
                    write_text(file_abs, meta["before"])
                except Exception:
                    pass
            continue

        if sha256_text(disk_src) != sha256_text(new_src):
            meta["compile_ok"] = False
            meta["compile_error"] = "WRITEBACK_MISMATCH: file on disk != intended content"
            if ROLLBACK_ON_COMPILE_FAIL:
                try:
                    write_text(file_abs, meta["before"])
                except Exception:
                    pass
            continue

        try:
            smoke_compile(disk_src, filename=file_abs)
            meta["compile_ok"] = True
            meta["compile_error"] = ""
        except Exception as e:
            meta["compile_ok"] = False
            meta["compile_error"] = type(e).__name__ + ": " + str(e)
            if ROLLBACK_ON_COMPILE_FAIL:
                try:
                    write_text(file_abs, meta["before"])
                except Exception:
                    pass

def propagate_compile_to_results(project_root, results, touched_files):
    by_rel = {}
    for file_abs, meta in touched_files.items():
        rel = os.path.relpath(file_abs, project_root)
        by_rel[rel] = meta

    for r in results:
        rel = r.get("file")
        if rel and rel in by_rel:
            r["compile_ok"] = by_rel[rel].get("compile_ok")
            if by_rel[rel].get("compile_ok") is False:
                if r.get("status") == "APPLIED":
                    r["status"] = "FAILED_COMPILE"
                if not r.get("message"):
                    r["message"] = by_rel[rel].get("compile_error", "")


# =========================
# REVERT
# =========================
def revert_run(project_root, run_stamp):
    run_dir = os.path.join(runs_root(project_root), run_stamp)
    manifest_path = os.path.join(run_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        return False, "Manifest not found for run: " + run_stamp

    try:
        manifest = json.loads(read_text(manifest_path))
    except Exception as e:
        return False, "Manifest unreadable: " + type(e).__name__ + ": " + str(e)

    touched = manifest.get("touched") or []
    snap_dir = os.path.join(run_dir, "snapshots")
    if not os.path.isdir(snap_dir):
        return False, "Snapshots folder missing for run: " + run_stamp

    # Restore each snapshot file to its original location
    restored = 0
    failed = 0
    errors = []

    for t in touched:
        rel = t.get("rel")
        if not rel:
            continue
        snap_path = os.path.join(snap_dir, rel)
        target_path = os.path.abspath(os.path.join(project_root, rel))
        try:
            src = read_text(snap_path)
            write_text(target_path, src)
            restored += 1
        except Exception as e:
            failed += 1
            errors.append(f"{rel}: {type(e).__name__}: {e}")

    if failed:
        msg = f"Revert completed with errors. Restored {restored}, failed {failed}."
        if errors:
            msg += "\n" + "\n".join(errors[:5])
        return False, msg

    return True, f"Reverted run {run_stamp} (restored {restored} file(s))."


# =========================
# UI + MAIN
# =========================
def determine_root_and_default_file():
    """
    Root = folder of current editor file (preferred). Fallback to folder of this script.
    Default file = current editor file path if available else this script.
    """
    cur_path = _editor_path()
    if cur_path and os.path.isfile(cur_path):
        root = os.path.dirname(os.path.abspath(cur_path))
        default_file_abs = os.path.abspath(cur_path)
        return root, default_file_abs, cur_path
    # fallback
    this_file = os.path.abspath(__file__)
    return os.path.dirname(this_file), this_file, None

def current_file_dirty(cur_path):
    """
    Only checks the currently open editor file.
    """
    if not cur_path or not os.path.isfile(cur_path):
        return False
    try:
        disk = read_text(cur_path)
    except Exception:
        disk = None
    buf = _editor_text()
    if disk is None:
        return False
    return sha256_text(disk) != sha256_text(buf)

def apply_from_clipboard(project_root, default_file_abs, cur_path, dry_run=False):
    if clipboard is None:
        _hud("clipboard module unavailable", "error", 1.2)
        return

    bundle_text = clipboard.get() or ""
    if not bundle_text.strip():
        _hud("Clipboard empty", "error", 1.2)
        return

    ops, _bundle_default = parse_patch_bundle(bundle_text)
    if not ops:
        _hud("No operations found in clipboard", "error", 1.2)
        return

    # Preflight: if current editor file is among targets and dirty -> refuse
    # (We can only safely detect "dirty" for the current file.)
    targets_abs = set()
    for op in ops:
        try:
            file_ref, _cls, _meth = parse_target(op.get("target"), default_file_abs, op_default_file=op.get("default_file"))
            file_abs = os.path.realpath(os.path.abspath(
                file_ref if os.path.isabs(file_ref)
                else os.path.join(project_root, file_ref)
            ))
            targets_abs.add(file_abs)
        except Exception:
            pass

    if cur_path and os.path.realpath(os.path.abspath(cur_path)) in targets_abs:
        if current_file_dirty(cur_path):
            _alert("AST Patcher", "Refused: the current file has unsaved edits.\n\nSave it, then run again.", "OK")
            return

    stamp = now_stamp()

    # Apply in memory
    results, touched_files, file_cache = apply_ops(ops, project_root, default_file_abs)

    if not dry_run:
        # Write + compile verify (+ rollback)
        verify_write_and_maybe_rollback(touched_files, file_cache)
        propagate_compile_to_results(project_root, results, touched_files)
        # Persist run artifacts
        run_dir, summary_path, jsonl_path = write_run_artifacts(project_root, stamp, bundle_text, results, touched_files, file_cache)
        prune_runs(project_root, KEEP_RUNS)
    else:
        run_dir = summary_path = jsonl_path = '(dry run — nothing written)'

    # Console lines
    if PRINT_OP_LINES_TO_CONSOLE:
        for r in results:
            print((r.get("status") or "UNKNOWN") + " | " + (r.get("op") or "?") + " | " + (r.get("target") or "?"))

    # Reload current editor buffer if it was targeted (skip in dry-run)
    if not dry_run and cur_path and os.path.realpath(os.path.abspath(cur_path)) in targets_abs:
        try:
            new_disk = read_text(cur_path)
            _editor_replace_all(new_disk)
        except Exception:
            pass

    # Build small run packet
    applied = sum(1 for r in results if r.get("status") == "APPLIED")
    failed = sum(1 for r in results if (r.get("status") or "").startswith("FAILED"))
    skipped = len(results) - applied - failed

    packet = []
    packet.append("=== AST PATCH RUN PACKET ===")
    packet.append("Run: " + stamp)
    packet.append("Root: " + os.path.abspath(project_root))
    packet.append("Run dir: " + run_dir)
    packet.append("Summary: " + summary_path)
    packet.append("JSONL: " + jsonl_path)
    packet.append("")
    packet.append(f"Totals: APPLIED={applied} SKIPPED={skipped} FAILED={failed}")
    packet.append("")
    packet.append("Ops:")
    for r in results:
        st = r.get("status") or "UNKNOWN"
        opn = r.get("op") or "?"
        tgt = r.get("target") or "?"
        rel = r.get("file") or "?"
        msg = r.get("message") or ""
        if msg:
            packet.append(f"- {st} | {opn} | {tgt} | {rel} :: {msg}")
        else:
            packet.append(f"- {st} | {opn} | {tgt} | {rel}")

    if ALWAYS_COPY_RUN_PACKET and clipboard is not None:
        try:
            clipboard.set("\n".join(packet).strip() + "\n")
        except Exception:
            pass

    if dry_run:
        summary_lines = [r.get('status', '?') + ' | ' + r.get('op', '?') + ' | ' + r.get('target', '?') for r in results]
        _alert("DRY RUN — nothing written",
               f"APPLIED={applied}  SKIPPED={skipped}  FAILED={failed}\n\n" + "\n".join(summary_lines),
               "OK")
    elif failed:
        _hud(f"Applied {applied} • Failed {failed} • Skipped {skipped}", "error", 1.3)
    else:
        _hud(f"Applied {applied} • Skipped {skipped}", "success", 1.2)

def revert_last_run_ui(project_root, cur_path):
    runs = list_runs(project_root)
    if not runs:
        _hud("No runs to revert", "error", 1.2)
        return

    # Pick run: last by default, or list dialog if available
    chosen = runs[0]
    if dialogs:
        try:
            picked = dialogs.list_dialog("Revert which run?", runs[:min(len(runs), KEEP_RUNS)])
            if picked:
                chosen = picked
            else:
                _hud("Cancelled", "error", 0.8)
                return
        except Exception:
            chosen = runs[0]
    else:
        c = _alert("Revert", f"Revert last run?\n\n{chosen}", "Revert", "Cancel")
        if c != 1:
            _hud("Cancelled", "error", 0.8)
            return

    # Safety: if current editor file is dirty and is under root, revert might overwrite it.
    # We refuse only if the current file is dirty (since we can't detect other open tabs).
    if cur_path and current_file_dirty(cur_path):
        _alert("AST Patcher", "Refused: the current file has unsaved edits.\n\nSave it (or close without saving), then revert.", "OK")
        return

    ok, msg = revert_run(project_root, chosen)

    # Reload editor buffer if current file exists (safe: we refused dirty)
    if ok and cur_path and os.path.isfile(cur_path):
        try:
            new_disk = read_text(cur_path)
            _editor_replace_all(new_disk)
        except Exception:
            pass

    _hud(msg, "success" if ok else "error", 1.4 if not ok else 1.2)

def main():
    if clipboard is None:
        _alert("AST Patcher", "clipboard module unavailable in this environment.", "OK")
        return
    if not supports_end_lineno():
        _alert("AST Patcher", "This Python build lacks AST end_lineno.\nNeed a tokenize fallback for safe ranges.", "OK")
        return

    project_root, default_file_abs, cur_path = determine_root_and_default_file()

    # UI
    msg = "Root:\n" + os.path.abspath(project_root)
    choice = _alert("AST Patcher", msg, "Apply (clipboard)", "Dry Run", "Revert")

    if choice == 1:
        apply_from_clipboard(project_root, default_file_abs, cur_path, dry_run=DRY_RUN)
        return
    if choice == 2:
        apply_from_clipboard(project_root, default_file_abs, cur_path, dry_run=True)
        return
    if choice == 3:
        revert_last_run_ui(project_root, cur_path)
        return

    _hud("Cancelled", "error", 0.8)

if __name__ == "__main__":
    main()
