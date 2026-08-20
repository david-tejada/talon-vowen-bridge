# Run the offline verification without requiring Talon or Vowen.
# Prefer the Windows Python launcher because a global `python` command is not
# guaranteed to exist on a clean Windows installation.
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    # CI and managed workspaces can provide Python outside PATH through this
    # explicit override. The repository does not encode a machine-specific
    # interpreter path.
    if ($env:VOWEN_TALON_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:VOWEN_TALON_PYTHON -PathType Leaf)) {
            throw "VOWEN_TALON_PYTHON no apunta a un ejecutable existente: $env:VOWEN_TALON_PYTHON"
        }
        & $env:VOWEN_TALON_PYTHON -m unittest discover -s tests -p 'test_*.py' -v
        exit $LASTEXITCODE
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        & py -3 -m unittest discover -s tests -p 'test_*.py' -v
        exit $LASTEXITCODE
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCommand) {
        & python -m unittest discover -s tests -p 'test_*.py' -v
        exit $LASTEXITCODE
    }

    throw 'No se encontró py -3 ni python. Instala Python 3 o ejecuta los tests con el intérprete de Talon.'
}
finally {
    Pop-Location
}
