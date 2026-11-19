$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$goBinary = if ($env:AGY_GO) { $env:AGY_GO } else { 'go' }
if ((& $goBinary version) -notmatch 'devel_298f53e483') {
    throw 'Set AGY_GO to Go revision 298f53e483; see native/README.md.'
}
if (-not (Test-Path -LiteralPath 'dist/index.html')) {
    Push-Location frontend
    try {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE) { throw 'Frontend installation failed.' }
        npm run build
        if ($LASTEXITCODE) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
$env:GOTOOLCHAIN = 'local'
Push-Location native
try {
    & $goBinary build -trimpath -o bin/gcli2api.exe .
    if ($LASTEXITCODE) { throw 'Go build failed.' }
} finally { Pop-Location }
& ./native/bin/gcli2api.exe
