import os
import sys
import zipfile
import re
import ast
import tempfile
import shutil
import json
import io
from contextlib import redirect_stdout

CONFIG_FILE = 'config.json'

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"[-] Error: {CONFIG_FILE} not found.")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def check_raw_text_style(code_content, filename):
    errors = []
    lines = code_content.splitlines()
    
    in_multiline_string = False
    
    for i, line in enumerate(lines):
        line_num = i + 1

        if len(line) > 79:
            errors.append(f"{filename}:{line_num}: line exceeds 79 characters ({len(line)} chars)")

        # מסוך (Masking) של תוכן בתוך גרשיים כדי למנוע אזעקות שווא על רווחים בתוך טקסט
        masked_line = re.sub(r'(".*?"|\'.*?\')', '""', line)

        # בדיקת רווח אחרי פסיק
        if re.search(r',(?=[^\s\'"\]\)])', masked_line):
            errors.append(f"{filename}:{line_num}: Missing space after comma.")
            
        # בדיקת רווחים מסביב לאופרטורים של השוואה והשמה (==, !=, <=, >=, +=, -=, *=, /=)
        if re.search(r'[^\s](==|!=|<=|>=|\+=|-=|\*=|/=)|(==|!=|<=|>=|\+=|-=|\*=|/=)[^\s]', masked_line):
            errors.append(f"{filename}:{line_num}: Missing spaces around operator (e.g., ==, !=, +=).")

        # בדיקת רווח אחרי סולמית בהערות (מתעלם מ-#! של שורת shebang)
        if '#' in line:
            comment_part = line[line.find('#'):]
            if re.match(r'#[^\s!]', comment_part):
                errors.append(f"{filename}:{line_num}: Missing space after '#' in comment.")

        # בדיקות הסטייל המקוריות
        if '\t' in line:
            errors.append(f"{filename}:{line_num}: Tabs are not allowed. Use 4 spaces for indentation.")
        if re.search(r'def\s+\w+\(.*?\s+=\s+.*?\):', line):
            errors.append(f"{filename}:{line_num}: Do not use spaces around '=' for default function arguments.")
            
    return errors

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
    builtins = dir(__builtins__)
    lines = code_content.splitlines()

    for node in ast.walk(tree):
        # שימוש ב-getattr כדי למנוע אזהרות של Pylance/MyPy
        line_suffix = f":{getattr(node, 'lineno')}" if hasattr(node, 'lineno') else ""

        if isinstance(node, ast.FunctionDef):
            if not ast.get_docstring(node):
                if "staircase" not in filename and "smiley" not in filename:
                    errors["docstring"].append(f"{filename}{line_suffix}: Missing docstring in function '{node.name}'.")
            
            if not re.match(r'^[a-z_][a-z0-9_]*$', node.name):
                errors["convention"].append(f"{filename}{line_suffix}: Function '{node.name}' should be snake_case.")
                
            if node.name in builtins:
                errors["convention"].append(f"{filename}{line_suffix}: Function '{node.name}' overrides a Python built-in.")
                
            if hasattr(node, 'end_lineno') and hasattr(node, 'lineno'):
                if (getattr(node, 'end_lineno') - getattr(node, 'lineno')) > 40:
                    errors["convention"].append(f"{filename}{line_suffix}: Function '{node.name}' is too long (> 40 lines).")

        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if target.id in builtins:
                        errors["convention"].append(f"{filename}{line_suffix}: Variable '{target.id}' overrides a Python built-in.")
                    elif not (re.match(r'^[a-z_][a-z0-9_]*$', target.id) or re.match(r'^[A-Z_][A-Z0-9_]*$', target.id)):
                        errors["convention"].append(f"{filename}{line_suffix}: Variable '{target.id}' should be snake_case or UPPER_CASE.")

        elif isinstance(node, ast.arg):
            if not re.match(r'^[a-z_][a-z0-9_]*$', node.arg):
                errors["convention"].append(f"{filename}{line_suffix}: Parameter '{node.arg}' should be snake_case.")

        # בדיקת Magic Numbers
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                if node.value not in (-1, 0, 1, 2):
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

        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            call_str = f"{node.value.id}.{node.attr}"
            found_calls.add(call_str)

    for req in required_calls:
        if req not in found_calls:
            errors["rule_violations"].append(f"{filename}: Missing required function call: {req}")

    return errors

def generate_and_run_tests(temp_dir, tests):
    if not tests:
        return {"passed": 0, "failed": 0, "details": []}
        
    test_script_path = os.path.join(temp_dir, "run_dynamic_tests.py")
    tests_data_path = os.path.join(temp_dir, "tests_data.json")
    
    # 1. שמירת הבדיקות לקובץ JSON נפרד, במקום הזרקה לתוך קוד פייתון
    with open(tests_data_path, 'w', encoding='utf-8') as f:
        json.dump(tests, f, indent=2)
    
    script_content = [
        "import sys, json, io, traceback, builtins",
        "import copy",
        "from contextlib import redirect_stdout",
        
        "try:",
        "    import karel",
        "except ImportError:",
        "    pass",
        
        "results = {'passed': 0, 'failed': 0, 'details': []}",
        
        # 2. קריאת הנתונים מהקובץ בצורה בטוחה
        "with open('tests_data.json', 'r', encoding='utf-8') as f:",
        "    tests = json.load(f)",
        
        "for test in tests:",
        "    try:",
        "        mod = __import__(test['file'].replace('.py', ''))",
        "        func = getattr(mod, test['func_name'])",
        "        args_list = copy.deepcopy(test.get('inputs', []))",
        
        # Setup Karel if needed
        "        if test.get('output_method') == 'karel_state':",
        "            karel.new_world(**test['setup_karel'])",
        
        # MOCK INPUT SETUP
        "        if 'mock_inputs' in test:",
        "            input_iter = iter(test['mock_inputs'])",
        "            def mock_input(prompt=''):",
        "                print(prompt, end='')",
        "                try: val = str(next(input_iter))",
        "                except StopIteration: raise EOFError('End of mock inputs')",  # <-- שורה מעודכנת שזורקת שגיאת EOFError טבעית
        "                print(val)",
        "                return val",
        "            builtins.input = mock_input",
        "        else:",
        "            builtins.input = input",
        
        # RUN FUNCTION
        "        f = io.StringIO()",
        "        with redirect_stdout(f):",
        "            res = func(*args_list)",
        "        stdout_output = f.getvalue().strip()",
        
        # VALIDATE KAREL STATE
        "        if test.get('output_method') == 'karel_state':",
        "            expected_karel = test['expected_beepers']",
        "            actual_karel = {str(k): v for k, v in karel._w.beepers.items()}",
        "            if expected_karel == actual_karel:",
        "                results['passed'] += 1",
        "                results['details'].append(f\"[V] {test['description']} - Passed\")",
        "            else:",
        "                results['failed'] += 1",
        "                results['details'].append(f\"[X] {test['description']} - Failed. Karel state mismatch.\")",
        "            continue",
        
        # VALIDATE IN-PLACE MUTATION
        "        if 'expected_mutation' in test:",
        "            idx = test['mutated_arg_index']",
        "            actual_mutation = args_list[idx]",
        "            if actual_mutation != test['expected_mutation']:",
        "                results['failed'] += 1",
        "                results['details'].append(f\"[X] {test['description']} - Failed. State mutation mismatch.\")",
        "                continue",
        
        # VALIDATE RETURNS AND STDOUT
        "        if test.get('output_method') == 'stdout':",
        "            actual = stdout_output",
        "            expected = str(test['expected']).strip()",
        "        else:",
        "            actual = res",
        "            expected = test['expected']",
        "            if test.get('expected_type') == 'tuple' and isinstance(expected, list):",
        "                expected = tuple(expected)",
        
        "        if str(actual) == str(expected) or actual == expected:",
        "            results['passed'] += 1",
        "            results['details'].append(f\"[V] {test['description']} - Passed\")",
        "        else:",
        "            results['failed'] += 1",
        "            results['details'].append(f\"[X] {test['description']} - Failed. Expected {expected}, got {actual}\")",
        
        "    except Exception as e:",
        "        results['failed'] += 1",
        "        results['details'].append(f\"[X] {test['description']} - Crashed: {type(e).__name__} - {str(e)}\")",
        "print(json.dumps(results))" 
    ]
    
    with open(test_script_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(script_content))
        
    print("\n--- Running Dynamic Unit Tests ---")
    
    import io
    from contextlib import redirect_stdout
    
    old_cwd = os.getcwd()
    os.chdir(temp_dir)
    sys.path.insert(0, temp_dir)
    
    f = io.StringIO()
    try:
        with redirect_stdout(f):
            # הרצה ישירה במקום subprocess
            exec(open("run_dynamic_tests.py", encoding="utf-8").read(), {})
    except Exception as e:
        print(json.dumps({"passed": 0, "failed": len(tests), "details": [f"[X] Crashed: {type(e).__name__} - {e}"]}))
    finally:
        sys.path.pop(0)
        os.chdir(old_cwd)
        
    stdout_output = f.getvalue().strip()
    try:
        # חילוץ ה-JSON משורת הפלט האחרונה
        lines = [line for line in stdout_output.split("\n") if line.strip()]
        test_results = json.loads(lines[-1]) if lines else {"passed": 0, "failed": len(tests), "details": []}
        for detail in test_results.get('details', []):
            print(detail)
        return test_results
    except Exception:
        print("[!] Execution Warnings/Errors:")
        print(stdout_output)
        return {"passed": 0, "failed": len(tests), "details": ["[X] Test script crashed or produced invalid output."]}

def process_submission(exercise_id, zip_path, config_data):
    ex_config = config_data["exercises"].get(exercise_id)
    if not ex_config:
        print(f"[-] Error: Configuration for '{exercise_id}' not found in JSON.")
        return

    print(f"\n{'='*60}\n[+] Checking submission for {exercise_id}\n{'='*60}")
    
    zip_name = os.path.basename(zip_path)
    if re.match(ex_config["expected_zip_regex"], zip_name):
        print(f"[V] Zip filename '{zip_name}' is valid.")
    else:
        print(f"[X] Invalid zip filename '{zip_name}'. Expected regex: {ex_config['expected_zip_regex']}")

    temp_dir = tempfile.mkdtemp()
    
    # If it's ex1, we must provide karel.py to the temp folder
    if exercise_id == 'ex1':
        if os.path.exists("karel.py"):
            shutil.copy("karel.py", temp_dir)
        else:
            print("[!] Warning: karel.py not found in the current directory. Karel tests may fail.")
            
    all_static_errors = []
    
    try:
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(temp_dir)
        except Exception as e:
            print(f"[-] Failed to open/extract zip: {e}")
            return

        for expected_file in ex_config["expected_files"]:
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

        test_results = generate_and_run_tests(temp_dir, ex_config.get("specific_tests", []))

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
    
    print(f"\n{'='*60}\nFINAL SUMMARY FOR: {zip_name}\n{'='*60}")
    
    print("\n--- STATIC ANALYSIS & RULES ---")
    if not all_static_errors:
        print("[V] All rule checks and conventions passed perfectly.")
    else:
        print(f"[X] Found {len(all_static_errors)} rule violations or missing files:")
        import textwrap
        for err in all_static_errors:
            # שבירת הטקסט אחרי 90 תווים והוספת הזחה עמוקה יותר לשורות הגולשות
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
                     failed_count +=1
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