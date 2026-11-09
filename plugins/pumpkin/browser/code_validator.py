import ast

from .sdk_protocol import MAX_PROGRAM_BYTES, SdkError
from .protocol import WorkerErrorCode


class BrowserCodeValidator:
    MAX_NODES = 5_000
    MAX_DEPTH = 40
    FORBIDDEN_NAMES = frozenset({
        "exec", "eval", "compile", "open", "input", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "dir", "help", "breakpoint", "memoryview",
        "classmethod", "staticmethod", "property", "super", "type", "object",
    })
    FORBIDDEN_NODES = (
        ast.Import, ast.ImportFrom, ast.ClassDef, ast.AsyncFunctionDef, ast.Await,
        ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Yield, ast.YieldFrom,
    )

    def validate(self, program: str) -> ast.Module:
        if not isinstance(program, str) or not 1 <= len(program.encode()) <= MAX_PROGRAM_BYTES:
            raise SdkError(WorkerErrorCode.PROGRAM_INVALID, "program exceeds source limit")
        wrapped = "def main(browser):\n" + "\n".join(
            "    " + line for line in program.splitlines()
        )
        try:
            tree = ast.parse(wrapped)
        except (SyntaxError, RecursionError, ValueError) as exc:
            raise SdkError(
                WorkerErrorCode.PROGRAM_INVALID,
                f"invalid Python syntax at line {max(1, getattr(exc, 'lineno', 2) - 1)}",
                field="program",
            ) from exc
        pending = [(tree, 0)]
        count = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > self.MAX_NODES or depth > self.MAX_DEPTH:
                raise SdkError(WorkerErrorCode.PROGRAM_INVALID, "AST limit exceeded")
            if isinstance(node, self.FORBIDDEN_NODES):
                raise SdkError(
                    WorkerErrorCode.PROGRAM_INVALID, "Python operation is not available",
                    field=f"program.line[{max(1, getattr(node, 'lineno', 2) - 1)}]",
                )
            if (
                isinstance(node, ast.Name)
                and (node.id.startswith("__") or node.id in self.FORBIDDEN_NAMES)
            ) or (
                isinstance(node, ast.Attribute) and node.attr.startswith("_")
            ):
                raise SdkError(
                    WorkerErrorCode.PROGRAM_INVALID, "private or unsafe Python access",
                    field=f"program.line[{max(1, node.lineno - 1)}]",
                )
            if (
                isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))
                and len(node.value) > MAX_PROGRAM_BYTES
            ):
                raise SdkError(WorkerErrorCode.PROGRAM_INVALID, "literal is too large")
            pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        return tree
