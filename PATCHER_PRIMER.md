# AST Patcher — LLM Session Primer

You are helping edit Python source files using the **AST Patcher** — a structured patch format that applies surgical changes to named targets in Python files. All patches are copied to the iOS clipboard and applied via a Pythonista script.

---

## How It Works

- You write a **patch bundle** (plain text in the format below)
- The user pastes it to their clipboard and runs the patcher
- The patcher locates targets via Python AST, applies changes in-memory, compile-checks, then writes to disk
- **A run packet is copied back to clipboard** showing APPLIED / SKIPPED / FAILED for every operation

---

## Patch Bundle Structure

```
DEFAULT_FILE path/to/file.py    ← optional; used when target has no file prefix

REPLACE file.py::ClassName.method_name
def method_name(self, ...):
    # complete new body here

INSERT_AFTER file.py::ClassName.existing_method
def new_method(self):
    pass

INSERT_INTO file.py::ClassName.method_name
ANCHOR: some text on the target line
MATCH: fuzzy
<code to insert near that line>

REPLACE_LINE file.py::ClassName.method_name
ANCHOR: old_value =
OCCURRENCE: 2
new_value = 42

REPLACE_LINES file.py::ClassName.method_name
ANCHOR_START: if old_condition:
ANCHOR_END: old_result = True
if new_condition:
    new_result = True

REPLACE_EXPR file.py::ClassName.method_name
ANCHOR: self.timeout
OLD: timeout=30
NEW: timeout=60

APPEND_INTO file.py::ClassName.method_name
log.debug("method complete")

PREPEND_INTO file.py::ClassName.method_name
if not self._ready:
    return

LIST_TARGETS file.py
```

---

## Target Syntax

| Pattern | Meaning |
|---------|---------|
| `file.py::ClassName.method_name` | Method inside a class |
| `file.py::ClassName.*` | Entire class |
| `file.py::function_name` | Top-level function |
| `file.py::@VAR_NAME` | Module-level assignment (`VAR = ...`) |
| `file.py::ClassName.@VAR_NAME` | Class-level assignment |
| `ClassName.method_name` | Uses `DEFAULT_FILE` or current editor file |

File paths are **relative to the project root** (the folder of the currently open file).

---

## Operation Reference

### `REPLACE <target>`
Replaces an **entire method, class, or function** with new code.
- Use when the change is large or structural
- Indentation is handled automatically (patcher re-indents to match original)
- **Most reliable op** — prefer this for anything larger than a few lines

```
REPLACE mymodule.py::MyClass.process
def process(self, data):
    result = self._transform(data)
    return result
```

---

### `INSERT_AFTER <target>` / `INSERT_BEFORE <target>`
Inserts a **new function or block** immediately after/before the target.
- Idempotent: skipped if a `def` with the same name already exists anywhere in the file
- Good for adding new methods next to an existing one

```
INSERT_AFTER mymodule.py::MyClass.existing_method
def new_helper(self, x):
    return x * 2
```

---

### `INSERT_INTO <target>`
Inserts code **inside** a method/function, near a specific line identified by `ANCHOR`.

**Directives:**
| Directive | Default | Notes |
|-----------|---------|-------|
| `ANCHOR: <text>` | required | Substring to find within the target block |
| `MATCH: fuzzy` | `exact` | Normalises whitespace before matching — **use this when in doubt** |
| `OCCURRENCE: N` | `1` | Use the Nth match (1-based) if anchor appears multiple times |
| `EXPECT: N` | `1` | Safety check — fails if anchor matches ≠ N times |
| `POSITION: after` | `after` | Insert after (`after`) or before (`before`) the anchor line |
| `INDENT: auto` | `auto` | `auto` = match anchor / child if `:`, `same` = match anchor, `child` = +4sp |

```
INSERT_INTO mymodule.py::MyClass.configure
ANCHOR: self.timeout
MATCH: fuzzy
POSITION: after
self.retries = 3
```

---

### `REPLACE_LINE <target>`
Replaces a **single line** within the target block. Preserves the original indentation.

```
REPLACE_LINE mymodule.py::MyClass.connect
ANCHOR: timeout=30
MATCH: fuzzy
timeout=60
```

With `OCCURRENCE` for non-unique lines:
```
REPLACE_LINE mymodule.py::MyClass.run
ANCHOR: result = None
OCCURRENCE: 2
result = self._default_result()
```

---

### `REPLACE_LINES <target>`
Replaces a **multi-line block** within the target, from `ANCHOR_START` through `ANCHOR_END` (inclusive).
- Both anchors must match exactly once inside the target block
- Good for replacing `if/else` blocks, multi-line assignments, chained calls

```
REPLACE_LINES mymodule.py::MyClass.validate
ANCHOR_START: if old_mode ==
ANCHOR_END: raise ValueError
MATCH: fuzzy
if new_mode == 'strict':
    self._strict_validate()
```

---

### `REPLACE_EXPR <target>`
Swaps one **expression substring** on an anchored line. Preserves everything else on the line.
- Useful for changing a keyword argument, a constant value, or a method name

```
REPLACE_EXPR mymodule.py::MyClass.connect
ANCHOR: connect(host, port
OLD: port=8080
NEW: port=9090
```

---

### `APPEND_INTO <target>`
Adds code at the **end of the target body** — no anchor needed.
- Indentation is inferred from the last line of the body

```
APPEND_INTO mymodule.py::MyClass.teardown
self._cleanup_handles()
```

---

### `PREPEND_INTO <target>`
Adds code at the **start of the target body** (immediately after the `def` / `class` line).
- Indentation is set to body-level (def indent + 4 spaces)

```
PREPEND_INTO mymodule.py::MyClass.process
if self._shutdown:
    return None
```

---

### `LIST_TARGETS <file.py>`
**Meta-operation**: lists every patchable target in the file.
- Output is copied to clipboard in valid target syntax
- Does not modify any files
- Use this when you are not sure what target names exist

```
LIST_TARGETS mymodule.py
```

---

## Micro-Targeting Best Practices

### Choose the right op
| Situation | Best op |
|-----------|---------|
| Change is large / structural | `REPLACE` |
| Add a new method | `INSERT_AFTER` |
| Add a guard at top of method | `PREPEND_INTO` |
| Add logging at end of method | `APPEND_INTO` |
| Change one value / arg | `REPLACE_EXPR` |
| Replace one line | `REPLACE_LINE` |
| Replace a multi-line block | `REPLACE_LINES` |
| Insert code near a known line | `INSERT_INTO` |

### Writing good anchors
- Use a **unique substring** from the target line — don't use generic text like `return` or `self`
- Include enough context: `self.timeout = ` is better than `timeout`
- **Always add `MATCH: fuzzy`** when the anchor has any chance of whitespace variation
- If the anchor appears more than once, use `OCCURRENCE: N` rather than trying to find a unique string

### EXPECT and OCCURRENCE
- `EXPECT: N` is a safety guard — set it to the actual count of matches you expect. Default is `1`.
- `OCCURRENCE: N` picks which match to use. Default is `1` (first match).
- Example: anchor appears twice, you want the second: `EXPECT: 2` + `OCCURRENCE: 2`
- If you get `SKIPPED_ANCHOR_MISMATCH`, the error message shows the first ~8 lines of the block to help you pick a better anchor.

### When to use MATCH: fuzzy
Add `MATCH: fuzzy` whenever:
- You are not 100% sure of the exact spacing (extra spaces, tabs)
- The anchor was copied from elsewhere and may have whitespace differences
- In doubt — fuzzy matching is safe and has no false-positive risk within a named target

---

## Status Codes

| Status | Meaning |
|--------|---------|
| `APPLIED` | Change was made successfully |
| `SKIPPED_ALREADY_APPLIED` | File already contains identical content (idempotent) |
| `SKIPPED_ALREADY_PRESENT` | The function/signature already exists |
| `SKIPPED_ANCHOR_MISMATCH` | Anchor matched wrong number of times (check message for block excerpt) |
| `FAILED_NOT_FOUND` | Target class/method/function not found — check spelling |
| `FAILED_AMBIGUOUS` | Multiple matches found (shouldn't happen normally) |
| `FAILED_INVALID_PATH` | File path escapes project root |
| `FAILED_IO` | File not found on disk |
| `FAILED_PARSE` | Missing directive (e.g., no ANCHOR) or bad syntax |
| `FAILED_COMPILE` | Patch produced a syntax error — file was rolled back automatically |

---

## Run Packet (returned to clipboard)

After every run the patcher copies a run packet to the clipboard. It looks like:

```
=== AST PATCH RUN PACKET ===
Run: 20260226_143012
Root: /path/to/project
...
Totals: APPLIED=3 SKIPPED=1 FAILED=0

Ops:
- APPLIED       | REPLACE    | mymodule.py::MyClass.process | mymodule.py
- SKIPPED_...   | REPLACE_LINE | ...
```

Paste this back to me if any operation failed so I can diagnose and produce a corrected patch.

---

## Multi-File Bundles

Use `DEFAULT_FILE` to set a fallback, then prefix individual ops that target other files:

```
DEFAULT_FILE models.py

REPLACE MyModel.save
def save(self):
    ...

REPLACE views.py::MyView.post
def post(self, request):
    ...
```

---

## Dry Run

Before applying, the user can choose **"Dry Run"** from the patcher menu. This runs all patch logic in memory and shows a preview of what would be APPLIED/SKIPPED/FAILED — without writing anything to disk.

---

## Quick Examples

**Change a constant in one method:**
```
REPLACE_EXPR mymodule.py::Config.defaults
ANCHOR: max_connections
OLD: max_connections=10
NEW: max_connections=50
```

**Add error handling guard at top of method:**
```
PREPEND_INTO mymodule.py::Worker.process
if self.stopped:
    raise RuntimeError("Worker is stopped")
```

**Replace a multi-line condition:**
```
REPLACE_LINES mymodule.py::Validator.check
ANCHOR_START: if mode == 'legacy'
ANCHOR_END: return False
MATCH: fuzzy
if mode == 'modern':
    return self._modern_check()
```

**Insert logging after a specific line:**
```
INSERT_INTO mymodule.py::Service.connect
ANCHOR: self._connection = create_conn
MATCH: fuzzy
POSITION: after
log.info("Connected to %s", self._host)
```

**Find out what targets are available:**
```
LIST_TARGETS mymodule.py
```
