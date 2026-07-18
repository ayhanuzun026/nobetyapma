$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Assert-LastExitCode([string]$step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$step basarisiz oldu (exit code: $LASTEXITCODE)"
    }
}

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
    "functions\_smoke_test.py",
    "functions\_regression_test.py"
)

Write-Host "[1/5] Python derleme kontrolu"
python -m py_compile @pythonFiles
Assert-LastExitCode "Python derleme kontrolu"

Write-Host "[2/5] Solver smoke test"
python functions\_smoke_test.py
Assert-LastExitCode "Solver smoke test"

Write-Host "[3/5] Regression test"
python functions\_regression_test.py
Assert-LastExitCode "Regression test"

Write-Host "[4/5] Frontend ve config testleri"
npm test
Assert-LastExitCode "Frontend ve config testleri"

Write-Host "[5/5] Git diff whitespace kontrolu"
git diff --check
Assert-LastExitCode "Git diff whitespace kontrolu"

Write-Host "Tum kontroller gecti."
