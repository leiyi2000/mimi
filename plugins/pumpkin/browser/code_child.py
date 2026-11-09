import ctypes
import datetime
import errno
import json
import math
import re
import resource
import socket
import sys
from typing import Any, ClassVar

from browser.code_validator import BrowserCodeValidator
from browser.safe_sdk import BrowserSdkError, PipeTransport, SafeBrowserSdk


class LinuxProcessSandbox:
    DENIED = (
        "open", "openat", "openat2", "creat", "socket", "socketpair", "connect",
        "bind", "listen", "accept", "accept4", "execve", "execveat", "fork", "vfork",
        "clone", "clone3", "ptrace", "process_vm_readv", "process_vm_writev",
        "kill", "tkill", "tgkill", "pidfd_open", "pidfd_getfd", "pidfd_send_signal",
        "mount", "umount2", "unshare", "setns", "bpf", "perf_event_open",
        "io_uring_setup", "io_uring_enter", "io_uring_register", "userfaultfd",
        "unlink", "unlinkat", "rename", "renameat", "renameat2", "mkdir", "mkdirat",
        "rmdir", "link", "linkat", "symlink", "symlinkat", "chmod", "fchmodat",
        "chown", "fchownat", "truncate", "reboot", "kexec_load",
    )

    def install(self) -> None:
        if sys.platform != "linux":
            raise RuntimeError("browser code sandbox requires Linux seccomp")
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        library.seccomp_init.argtypes = [ctypes.c_uint32]
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
        library.seccomp_rule_add.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ]
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_release.argtypes = [ctypes.c_void_p]
        context = library.seccomp_init(0x7FFF0000)
        if not context:
            raise RuntimeError("seccomp initialization failed")
        try:
            for name in self.DENIED:
                syscall = library.seccomp_syscall_resolve_name(name.encode())
                if syscall >= 0 and library.seccomp_rule_add(
                    context, 0x00050000 | errno.EPERM, syscall, 0,
                ) != 0:
                    raise RuntimeError("seccomp rule installation failed")
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(38, 1, 0, 0, 0) != 0 or library.seccomp_load(context) != 0:
                raise RuntimeError("seccomp filter installation failed")
        finally:
            library.seccomp_release(context)


class BrowserCodeChild:
    BUILTINS: ClassVar[dict[str, Any]] = {
        "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
        "enumerate": enumerate, "float": float, "int": int, "isinstance": isinstance,
        "len": len, "list": list, "max": max, "min": min, "range": range,
        "reversed": reversed, "round": round, "set": set, "sorted": sorted,
        "str": str, "sum": sum, "tuple": tuple, "zip": zip, "print": print,
        "Exception": Exception, "ValueError": ValueError, "RuntimeError": RuntimeError,
        "AssertionError": AssertionError, "BrowserSdkError": BrowserSdkError,
    }

    def run(self, channel_fd: int) -> int:
        channel = socket.socket(fileno=channel_fd)
        reader = channel.makefile("r", encoding="utf-8")
        writer = channel.makefile("w", encoding="utf-8")
        try:
            program = sys.stdin.read()
            tree = BrowserCodeValidator().validate(program)
            compiled = compile(tree, "<browser-program>", "exec")
            resource.setrlimit(resource.RLIMIT_AS, (192 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            LinuxProcessSandbox().install()
            sdk = SafeBrowserSdk(PipeTransport(reader, writer))
            scope = {
                "__builtins__": self.BUILTINS,
                "json": json, "re": re, "datetime": datetime, "math": math,
            }
            exec(compiled, scope)  # noqa: S102
            returned = scope["main"](sdk)
            encoded = json.dumps(
                {"return_value": returned}, ensure_ascii=False, separators=(",", ":"),
            )
            if len(encoded.encode()) > 20 * 1024:
                raise ValueError("program return value exceeds output limit")
            writer.write(encoded + "\n")
            writer.flush()
            return 0
        except Exception as exc:  # noqa: BLE001
            error = {
                "code": getattr(exc, "code", "BROWSER_PROGRAM_FAILED"),
                "message": str(exc)[:1_000],
                "field": getattr(exc, "field", None),
                "sdk_call_index": getattr(exc, "sdk_call_index", None),
            }
            writer.write(json.dumps({"program_error": error}) + "\n")
            writer.flush()
            return 1
        finally:
            reader.close()
            writer.close()
            channel.close()


if __name__ == "__main__":
    sys.exit(BrowserCodeChild().run(int(sys.argv[1])))
