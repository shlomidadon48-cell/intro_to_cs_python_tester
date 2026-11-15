import os
import sys
import zipfile
import re
import ast
import tokenize
import tempfile
import shutil
import json
import io
import copy
import runpy
import textwrap
from pathlib import Path
from contextlib import redirect_stdout

CONFIG_FILE = 'config.json'
MAX_EXTRACT_BYTES = 10 * 1024 * 1024  # מגבלת 10MB למניעת Zip Bomb


def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[-] Error: {CONFIG_FILE} not found.")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def safe_extract_zip(zip_path, dest_dir):
    """חילוץ מאובטח של קובץ ZIP תוך חסימת Zip Slip ו-Zip Bomb."""
    dest_real = os.path.realpath(dest_dir)
    total_size = 0
    extracted_rel_paths = []

    with zipfile.ZipFile(zip_path, 'r') as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue

            total_size += info.file_size
            if total_size > MAX_EXTRACT_BYTES:
                raise ValueError("Zip archive exceeds maximum allowed uncompressed size (10MB).")

            target_path = os.path.realpath(os.path.join(dest_dir, info.filename))
            if not target_path.startswith(dest_real + os.sep):
                raise ValueError(f"Unsafe path detected in zip archive: {info.filename}")

            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with zf.open(info) as src, open(target_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)

            rel_path = os.path.relpath(target_path, dest_real).replace("\\", "/")
            extracted_rel_paths.append(rel_path)

    return extracted_rel_paths


def check_raw_text_style(code_content, filename):
    """בדיקות סגנון מבוססות tokenize (ללא אזעקות שווא על Docstrings וללא ReDoS)."""
    errors = []
    lines = code_content.splitlines()

    for i, line in enumerate(lines):
        line_num = i + 1
        if len(line) > 79:
            errors.append(f"{filename}:{line_num}: line exceeds 79 characters ({len(line)} chars)")
        if '\t' in line:
            errors.append(f"{filename}:{line_num}: Tabs are not allowed. Use 4 spaces for indentation.")

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(code_content).readline))
    except tokenize.TokenError:
        return errors

    spaced_ops = {'==', '!=', '<=', '>=', '+=', '-=', '*=', '/='}
    in_def_header = False
    paren_depth = 0

    for idx, tok in enumerate(tokens):
        line_num = tok.start[0]
        prev_tok = tokens[idx - 1] if idx > 0 else None
        next_tok = tokens[idx + 1] if idx + 1 < len(tokens) else None

        if tok.type == tokenize.COMMENT:
            if re.match(r'^#[^\s!]', tok.string):
                errors.append(f"{filename}:{line_num}: Missing space after '#' in comment.")

        elif tok.type == tokenize.NAME and tok.string == 'def':
            in_def_header = True
            paren_depth = 0

        elif tok.type == tokenize.OP:
            if in_def_header:
                if tok.string == '(':
                    paren_depth += 1
                elif tok.string == ')':
                    paren_depth -= 1
                    if paren_depth <= 0:
                        in_def_header = False
                elif tok.string == '=' and paren_depth == 1 and prev_tok and next_tok:
                    space_before = (prev_tok.end[0] == tok.start[0] and prev_tok.end[1] < tok.start[1])
                    space_after = (next_tok.start[0] == tok.end[0] and tok.end[1] < next_tok.start[1])
                    if space_before or space_after:
                        errors.append(f"{filename}:{line_num}: Do not use spaces around '=' for default function arguments.")

            if tok.string == ',' and next_tok:
                if (
                    next_tok.start[0] == tok.end[0]
                    and next_tok.start[1] == tok.end[1]
                    and next_tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER, tokenize.COMMENT)
                    and next_tok.string not in (')', ']', '}')
                ):
                    errors.append(f"{filename}:{line_num}: Missing space after comma.")

            elif tok.string in spaced_ops and prev_tok and next_tok:
                missing_before = (
                    prev_tok.end[0] == tok.start[0]
                    and prev_tok.end[1] == tok.start[1]
                    and prev_tok.type not in (tokenize.INDENT, tokenize.NEWLINE, tokenize.NL)
                )
                missing_after = (
                    next_tok.start[0] == tok.end[0]
                    and next_tok.start[1] == tok.end[1]
                    and next_tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER)
                )
                if missing_before or missing_after:
                    errors.append(f"{filename}:{line_num}: Missing spaces around operator '{tok.string}'.")

    return errors


def _get_function_code_length(func_node):
    """חישוב אורך פונקציה ללא שורות ה-Docstring שלה."""
    if not (hasattr(func_node, 'end_lineno') and hasattr(func_node, 'lineno')):
        return 0
    total_lines = func_node.end_lineno - func_node.lineno
    if (
        func_node.body
        and isinstance(func_node.body[0], ast.Expr)
        and isinstance(func_node.body[0].value, ast.Constant)
        and isinstance(func_node.body[0].value.value, str)
    ):
        doc_node = func_node.body[0]
        if hasattr(doc_node, 'end_lineno') and hasattr(doc_node, 'lineno'):
            total_lines -= (doc_node.end_lineno - doc_node.lineno + 1)
    return total_lines


def analyze_code(code_content, filename, ast_rules_config):
    errors = {"docstring": [], "convention": [], "rule_violations": []}

    raw_errors = check_raw_text_style(code_content, filename)
    errors["convention"].extend(raw_errors)

    try:
        tree = ast.parse(code_content)
    except SyntaxError as e:
        errors["rule_violations"].append(f"{filename}: Syntax error: {e}")
        return errors

    file_rules = ast_rules_config.get(filename, {})
    allowed_imports = file_rules.get("allowed_imports", [])
    required_calls = file_rules.get("required_calls", [])
    forbidden_nodes = file_rules.get("forbidden_nodes", [])

    found_calls = set()
    builtins_set = set(dir(__builtins__))
    lines = code_content.splitlines()

    for node in ast.walk(tree):
        line_suffix = f":{getattr(node, 'lineno')}" if hasattr(node, 'lineno') else ""

        if isinstance(node, ast.FunctionDef):
            if not ast.get_docstring(node):
                if node.name != "main":
                    errors["docstring"].append(f"{filename}{line_suffix}: Missing docstring in function '{node.name}'.")

            if not re.match(r'^[a-z_][a-z0-9_]*$', node.name):
                errors["convention"].append(f"{filename}{line_suffix}: Function '{node.name}' should be snake_case.")

            if node.name in builtins_set:
                errors["convention"].append(f"{filename}{line_suffix}: Function '{node.name}' overrides a Python built-in.")

            if _get_function_code_length(node) > 40:
                errors["convention"].append(f"{filename}{line_suffix}: Function '{node.name}' is too long (> 40 lines).")

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id in builtins_set:
                        errors["convention"].append(f"{filename}{line_suffix}: Variable '{target.id}' overrides a Python built-in.")
                    elif not (re.match(r'^[a-z_][a-z0-9_]*$', target.id) or re.match(r'^[A-Z_][A-Z0-9_]*$', target.id)):
                        errors["convention"].append(f"{filename}{line_suffix}: Variable '{target.id}' should be snake_case or UPPER_CASE.")

        elif isinstance(node, ast.arg):
            if not re.match(r'^[a-z_][a-z0-9_]*$', node.arg):
                errors["convention"].append(f"{filename}{line_suffix}: Parameter '{node.arg}' should be snake_case.")

        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                if node.value not in (-1, 0, 1, 2, 6, 26):
                    if hasattr(node, 'lineno'):
                        line_str = lines[getattr(node, 'lineno') - 1]
                        if not re.match(r'^\s*[A-Z_][A-Z0-9_]*\s*=', line_str):
                            errors["convention"].append(f"{filename}{line_suffix}: Magic number '{node.value}' used. Consider assigning it to a named CONSTANT.")

        elif type(node).__name__ in forbidden_nodes:
            errors["rule_violations"].append(f"{filename}{line_suffix}: Forbidden syntax used: {type(node).__name__}")

        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if "Import" in forbidden_nodes or "ImportFrom" in forbidden_nodes:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if allowed_imports and alias.name not in allowed_imports:
                        errors["rule_violations"].append(f"{filename}{line_suffix}: Forbidden import: {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if allowed_imports and node.module not in allowed_imports:
                    errors["rule_violations"].append(f"{filename}{line_suffix}: Forbidden from-import: {node.module}")

        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            found_calls.add(node.func.id)

        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            call_str = f"{node.value.id}.{node.attr}"
            found_calls.add(call_str)

    for top_node in tree.body:
        if isinstance(top_node, ast.Expr) and isinstance(top_node.value, ast.Call):
            ln = f":{top_node.lineno}" if hasattr(top_node, 'lineno') else ""
            errors["rule_violations"].append(
                f"{filename}{ln}: Top-level function call found outside 'if __name__ == \"__main__\":'."
            )

    for req in required_calls:
        if req not in found_calls:
            errors["rule_violations"].append(f"{filename}: Missing required function call: {req}")

    return errors


def _load_clean_module(module_name):
    """טעינה נקייה של מודול הסטודנט תוך מחיקת גרסה קודמת מ-sys.modules."""
    if module_name in sys.modules:
        del sys.modules[module_name]
    return __import__(module_name)


def run_dynamic_tests(temp_dir, tests, pytest_files=None):
    """הרצה ישירה ומבודדת של הטסטים הדינמיים ללא שימוש ב-exec חיצוני."""
    pytest_files = pytest_files or []
    if not tests and not pytest_files:
        return {"passed": 0, "failed": 0, "details": []}

    print("\n--- Running Dynamic Unit Tests ---")
    results = {'passed': 0, 'failed': 0, 'details': []}

    try:
        import karel
    except ImportError:
        karel = None

    old_cwd = os.getcwd()
    old_argv = sys.argv[:]
    old_input = __builtins__['input'] if isinstance(__builtins__, dict) else __builtins__.input
    temp_real = os.path.realpath(temp_dir)

    os.chdir(temp_dir)
    sys.path.insert(0, temp_dir)

    try:
        for test in tests:
            try:
                if 'setup_files' in test:
                    for fname, content in test['setup_files'].items():
                        target_setup = os.path.realpath(os.path.join(temp_dir, fname))
                        if not target_setup.startswith(temp_real + os.sep):
                            raise ValueError(f"Unsafe setup file path: {fname}")
                        with open(target_setup, 'w', encoding='utf-8') as sf:
                            sf.write(content)

                # הרצה כסקריפט ראשי (__main__) עבור בדיקות CLI וקודי יציאה
                if test.get('run_as_main'):
                    sys.argv = test.get('mock_argv', [test['file']])
                    exit_code = 0
                    f_out = io.StringIO()
                    try:
                        with redirect_stdout(f_out):
                            runpy.run_path(test['file'], run_name='__main__')
                    except SystemExit as se:
                        exit_code = se.code if isinstance(se.code, int) else (1 if se.code else 0)
                    finally:
                        sys.argv = old_argv[:]

                    stdout_output = f_out.getvalue().strip()
                    expected_code = test.get('expected_exit_code', 0)
                    if exit_code != expected_code:
                        results['failed'] += 1
                        results['details'].append(
                            f"[X] {test['description']} - Failed. Expected exit code {expected_code}, got {exit_code}. Output: {stdout_output}"
                        )
                        continue

                    if 'expected' in test and stdout_output != str(test['expected']).strip():
                        results['failed'] += 1
                        results['details'].append(
                            f"[X] {test['description']} - Failed. Expected stdout '{test['expected']}', got '{stdout_output}'"
                        )
                        continue

                    # 2. תמיכה בהשוואה מדויקת (כולל \n בסוף קובץ) ב-expected_file_content
                    if 'expected_file_content' in test:
                        out_file = test['expected_file_content']['filename']
                        exact = test['expected_file_content'].get('exact_match', False)
                        raw_expected = test['expected_file_content']['content']
                        expected_text = raw_expected if exact else raw_expected.strip()
                        if not os.path.exists(out_file):
                            results['failed'] += 1
                            results['details'].append(
                                f"[X] {test['description']} - Failed. Output file '{out_file}' was not created."
                            )
                            continue
                        with open(out_file, 'r', encoding='utf-8') as rf:
                            raw_actual = rf.read()
                            actual_text = raw_actual if exact else raw_actual.strip()
                        if actual_text == expected_text:
                            results['passed'] += 1
                            results['details'].append(f"[V] {test['description']} - Passed")
                        else:
                            results['failed'] += 1
                            results['details'].append(
                                f"[X] {test['description']} - Failed. File content mismatch. Expected:\n{repr(expected_text)}\nGot:\n{repr(actual_text)}"
                            )
                        continue

                mod = _load_clean_module(test['file'].replace('.py', ''))
                func = getattr(mod, test['func_name'])
                args_list = copy.deepcopy(test.get('inputs', []))

                if test.get('output_method') == 'karel_state' and karel is not None:
                    karel.new_world(**test['setup_karel'])

                if 'mock_argv' in test:
                    sys.argv = test['mock_argv']

                if 'mock_inputs' in test:
                    input_iter = iter(test['mock_inputs'])

                    def mock_input(prompt=''):
                        print(prompt, end='')
                        try:
                            val = str(next(input_iter))
                        except StopIteration:
                            raise EOFError('End of mock inputs')
                        print(val)
                        return val

                    if isinstance(__builtins__, dict):
                        __builtins__['input'] = mock_input
                    else:
                        __builtins__.input = mock_input

                f_out = io.StringIO()
                with redirect_stdout(f_out):
                    res = func(*args_list)
                stdout_output = f_out.getvalue().strip()

                if 'expected_file_content' in test:
                    out_file = test['expected_file_content']['filename']
                    expected_text = test['expected_file_content']['content'].strip()
                    if not os.path.exists(out_file):
                        results['failed'] += 1
                        results['details'].append(
                            f"[X] {test['description']} - Failed. Output file '{out_file}' was not created."
                        )
                        continue
                    with open(out_file, 'r', encoding='utf-8') as rf:
                        actual_text = rf.read().strip()
                    if actual_text == expected_text:
                        results['passed'] += 1
                        results['details'].append(f"[V] {test['description']} - Passed")
                    else:
                        results['failed'] += 1
                        results['details'].append(
                            f"[X] {test['description']} - Failed. File content mismatch. Expected:\n{expected_text}\nGot:\n{actual_text}"
                        )
                    continue

                # 1. תיקון מיפוי פרמטרים עבור karel.new_world
                if test.get('output_method') == 'karel_state' and karel is not None:
                    sk = test['setup_karel']
                    karel.new_world(
                        cols=sk.get('cols', 10),
                        rows=sk.get('rows', 10),
                        robot_col=sk.get('robot_col', sk.get('col', 1)),
                        robot_row=sk.get('robot_row', sk.get('row', 1)),
                        robot_dir=sk.get('robot_dir', sk.get('dir', 'E')),
                        bag=sk.get('bag', -1),
                    )

                if 'expected_mutation' in test:
                    idx = test['mutated_arg_index']
                    actual_mutation = args_list[idx]
                    if actual_mutation != test['expected_mutation']:
                        results['failed'] += 1
                        results['details'].append(f"[X] {test['description']} - Failed. State mutation mismatch.")
                        continue

                if test.get('output_method') == 'stdout':
                    actual = stdout_output
                    expected = str(test['expected']).strip()
                else:
                    actual = res
                    expected = test['expected']
                    if test.get('expected_type') == 'tuple' and isinstance(expected, list):
                        expected = tuple(expected)

                if test.get('sort_tuples_result') and isinstance(actual, list) and isinstance(expected, list):
                    actual_cmp = sorted([tuple(x) for x in actual])
                    expected_cmp = sorted([tuple(x) for x in expected])
                    is_equal = (actual_cmp == expected_cmp)
                else:
                    is_equal = (str(actual) == str(expected) or actual == expected)

                if is_equal:
                    results['passed'] += 1
                    results['details'].append(f"[V] {test['description']} - Passed")
                else:
                    results['failed'] += 1
                    results['details'].append(f"[X] {test['description']} - Failed. Expected {expected}, got {actual}")

            except (Exception, SystemExit) as e:
                results['failed'] += 1
                results['details'].append(f"[X] {test['description']} - Crashed: {type(e).__name__} - {str(e)}")
            finally:
                sys.argv = old_argv[:]
                if isinstance(__builtins__, dict):
                    __builtins__['input'] = old_input
                else:
                    __builtins__.input = old_input

        for pf in pytest_files:
            if os.path.exists(pf):
                try:
                    import pytest  # type: ignore[import-not-found]
                except ImportError:
                    results['details'].append(
                        f"[!] Skipped {pf}: pytest is not installed in this environment."
                    )
                    continue

                f_pytest = io.StringIO()
                with redirect_stdout(f_pytest):
                    ret_code = pytest.main([pf, "-q", "--tb=short"])
                if ret_code == 0:
                    results['passed'] += 1
                    results['details'].append(f"[V] Student pytest suite ({pf}) - Passed")
                else:
                    results['failed'] += 1
                    out_msg = f_pytest.getvalue().strip()
                    results['details'].append(f"[X] Student pytest suite ({pf}) - Failed:\n{out_msg}")

    finally:
        sys.path.pop(0)
        os.chdir(old_cwd)

    for detail in results['details']:
        print(detail)

    return results

def validate_non_code_file(file_path, filename):
    """אימות שקובצי חובה אינם ריקים ותואמים לפורמט המצופה."""
    if os.path.getsize(file_path) == 0:
        return f"Submission file '{filename}' is empty (0 bytes)."
    if filename.lower().endswith(".png"):
        with open(file_path, "rb") as f:
            header = f.read(8)
        if header != b"\x89PNG\r\n\x1a\n":
            return f"File '{filename}' is not a valid PNG image."
    return None


def process_submission(exercise_id, zip_path, config_data, display_name=None):
    ex_config = config_data["exercises"].get(exercise_id)
    if not ex_config:
        print(f"[-] Error: Configuration for '{exercise_id}' not found in JSON.")
        return

    print(f"\n{'='*60}\n[+] Checking submission for {exercise_id}\n{'='*60}")

    zip_name = display_name if display_name else os.path.basename(zip_path)
    if re.match(ex_config["expected_zip_regex"], zip_name):
        print(f"[V] Zip filename '{zip_name}' is valid.")
    else:
        print(f"[X] Invalid zip filename '{zip_name}'. Expected regex: {ex_config['expected_zip_regex']}")

    temp_dir = tempfile.mkdtemp()

    if exercise_id == 'ex1':
        if os.path.exists("karel.py"):
            shutil.copy("karel.py", temp_dir)
        else:
            print("[!] Warning: karel.py not found in the current directory. Karel tests may fail.")

    all_static_errors = []
    test_results = None

    try:
        try:
            extracted_files = safe_extract_zip(zip_path, temp_dir)
        except Exception as e:
            print(f"[-] Failed to open/extract zip: {e}")
            return

        expected_code_files = ex_config.get("expected_files", [])
        expected_non_code_files = ex_config.get("expected_non_code_files", [])
        all_expected = set(expected_code_files) | set(expected_non_code_files)

        # בדיקת קבצים לא-קוד נדרשים (למשל README.txt, runtime_report.png, test_cipher_cracker.py)
        for req_non_code in expected_non_code_files:
            non_code_path = os.path.join(temp_dir, req_non_code)
            if not os.path.exists(non_code_path):
                msg = f"Missing required submission file: {req_non_code}"
                print(f"[X] {msg}")
                all_static_errors.append(msg)
            else:
                val_err = validate_non_code_file(non_code_path, req_non_code)
                if val_err:
                    print(f"[X] {val_err}")
                    all_static_errors.append(val_err)

        # בדיקת קבצים עודפים או תיקיות מקוננות ב-ZIP
        if ex_config.get("strict_zip_contents", False):
            ignored_system = {"karel.py"} if exercise_id == "ex1" else set()
            for rel_f in extracted_files:
                if rel_f in ignored_system or rel_f.startswith("__MACOSX/"):
                    continue
                if rel_f not in all_expected:
                    if "/" in rel_f and os.path.basename(rel_f) in all_expected:
                        msg = f"Nested folder detected ('{rel_f}'). Files must be at the root of the zip."
                    else:
                        msg = f"Unexpected extra file in zip: {rel_f}"
                    print(f"[X] {msg}")
                    all_static_errors.append(msg)

        for expected_file in expected_code_files:
            file_path = os.path.join(temp_dir, expected_file)
            if not os.path.exists(file_path):
                missing_msg = f"Missing required file: {expected_file}"
                print(f"\n[X] {missing_msg}")
                all_static_errors.append(missing_msg)
                continue

            print(f"\n--- Static Analysis: {expected_file} ---")
            with open(file_path, 'r', encoding='utf-8') as f:
                code_content = f.read()

            analysis = analyze_code(code_content, expected_file, ex_config.get("ast_rules", {}))

            has_issues = False
            for cat, errs in analysis.items():
                for err in errs:
                    print(f"  [-] {cat.capitalize()}: {err}")
                    all_static_errors.append(err)
                    has_issues = True

            if not has_issues:
                print("  [V] Code looks clean (Docstrings, Conventions, Rules).")

        test_results = run_dynamic_tests(
            temp_dir,
            ex_config.get("specific_tests", []),
            pytest_files=ex_config.get("pytest_files", [])
        )

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print(f"\n{'='*60}\nFINAL SUMMARY FOR: {zip_name}\n{'='*60}")

    print("\n--- STATIC ANALYSIS & RULES ---")
    if not all_static_errors:
        print("[V] All rule checks and conventions passed perfectly.")
    else:
        print(f"[X] Found {len(all_static_errors)} rule violations or missing files:")
        for err in all_static_errors:
            wrapped_err = textwrap.fill(err, width=90, subsequent_indent="        ")
            print(f"    - {wrapped_err}")

    if test_results:
        print("\n--- DYNAMIC TESTS ---")
        total_tests = test_results['passed'] + test_results['failed']
        if test_results['failed'] == 0 and total_tests > 0:
            print(f"[V] ALL TESTS PASSED! ({test_results['passed']}/{total_tests})")
        else:
            print(f"[X] SOME TESTS FAILED. Passed: {test_results['passed']}, Failed: {test_results['failed']}")
            print("    Failed Tests Breakdown:")
            failed_count = 0
            for detail in test_results['details']:
                if detail.startswith("[X]"):
                    wrapped_detail = textwrap.fill(detail, width=90, subsequent_indent="        ")
                    print(f"    {wrapped_detail}")
                    failed_count += 1
            if failed_count == 0:
                print("    (See detailed output above for crash reports)")
    print(f"\n{'='*60}")


if __name__ == "__main__":
    config_db = load_config()
    print("Available exercises in database:")
    for ex in config_db["exercises"].keys():
        print(f" - {ex}")

    chosen_ex = input("\nEnter the exercise to check (e.g., ex1): ").strip()

    if chosen_ex not in config_db["exercises"]:
        print("Invalid exercise selected.")
    else:
        zip_file_path = input("Enter the full path to the student's zip file: ").strip().strip('"\'')
        if not os.path.exists(zip_file_path):
            print("File not found! Please check the path.")
        else:
            process_submission(chosen_ex, zip_file_path, config_db)