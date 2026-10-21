"""All production child entry points must share the guarded macOS launcher."""

import ast
from pathlib import Path

import email_reader


def test_no_production_module_bypasses_safe_process_launcher():
    root = Path(email_reader.__file__).parent
    violations = []
    unsafe = {
        "subprocess": {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
        "os": {"fork", "forkpty", "system", "popen"},
    }
    for path in root.rglob("*.py"):
        if path.name == "process_launch.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules, functions = {}, set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update({item.asname or item.name: item.name for item in node.names if item.name in unsafe})
            elif isinstance(node, ast.ImportFrom) and node.module in unsafe:
                functions.update(item.asname or item.name for item in node.names if item.name in unsafe[node.module])
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            direct = isinstance(function, ast.Name) and function.id in functions
            qualified = (isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                         and function.attr in unsafe.get(modules.get(function.value.id), set()))
            if direct or qualified:
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, f"Unsafe process-launch call sites bypass the guarded helper: {violations}"
