$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$pythonFiles = @(
    "functions\utils.py",
    "functions\solve_strategy.py",
    "functions\solver_models.py",
    "functions\preflight_analyzer.py",
    "functions\planlayici.py",
    "functions\parsers.py",
    "functions\ortools_solver.py",
    "functions\main.py",
    "functions\kapasite.py",
    "functions\http_helpers.py",
    "functions\hedef_hesaplayici.py",
    "functions\gun_iskelet_planlayici.py",
    "functions\firestore_logger.py",
    "functions\excel_export.py",
    "functions\hedef_teshis.py",
    "functions\_smoke_test.py"
)

Write-Host "[1/3] Python derleme kontrolu"
python -m py_compile @pythonFiles

Write-Host "[2/3] Solver smoke test"
python functions\_smoke_test.py

Write-Host "[3/3] Git diff whitespace kontrolu"
git diff --check

Write-Host "Tum kontroller gecti."
