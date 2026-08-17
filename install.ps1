<#!
Codex Compressor 소스 CLI를 실행하는 Windows 설치 도우미입니다.
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

$ErrorActionPreference = "Stop"
$sourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = $null

foreach ($candidate in @("python", "python3", "py")) {
    try {
        if ($candidate -eq "py") {
            $versionText = & $candidate -3.11 --version 2>&1
            $versionMatch = [regex]::Match([string]$versionText, "Python 3\.(\d+)")
            if ($LASTEXITCODE -eq 0 -and $versionMatch.Success -and [int]$versionMatch.Groups[1].Value -ge 11) {
                $python = @($candidate, "-3.11")
                break
            }
        } else {
            $versionText = & $candidate --version 2>&1
            $versionMatch = [regex]::Match([string]$versionText, "Python 3\.(\d+)")
            if ($LASTEXITCODE -eq 0 -and $versionMatch.Success -and [int]$versionMatch.Groups[1].Value -ge 11) {
                $python = @($candidate)
                break
            }
        }
    } catch {
        # PATH에 없는 인터프리터는 다음 후보로 진행합니다.
    }
}

if ($null -eq $python) {
    throw "Python 3.11 이상을 찾지 못했습니다."
}

$oldPath = $env:PYTHONPATH
try {
    if ($oldPath) {
        $env:PYTHONPATH = "$sourceRoot\src;$oldPath"
    } else {
        $env:PYTHONPATH = "$sourceRoot\src"
    }
    if ($python.Count -eq 2) {
        & $python[0] $python[1] -m codex_compressor @Arguments
    } else {
        & $python[0] -m codex_compressor @Arguments
    }
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $oldPath
}
