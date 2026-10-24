"""Static check for undefined and unbound names.

The --selftest branch once shipped referencing a variable that does not exist
in that scope. Nothing caught it: no test exercises the CLI branches, and
Python only raises on execution. pyflakes catches exactly this.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
FATAL = ("undefined name", "local variable", "is assigned to but never used",
         "imported but unused", "redefinition of unused")

if __name__ == "__main__":
    files = sorted(str(p) for p in list(ROOT.glob("*.py")) + list((ROOT / "tests").glob("*.py")))
    proc = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                          capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    if output:
        print(output)
    problems = [l for l in output.splitlines() if any(f in l for f in FATAL)]
    print(f"\n{'='*60}")
    if problems:
        print(f"{len(problems)} static problem(s) found")
        sys.exit(1)
    print("0 static problems found")
