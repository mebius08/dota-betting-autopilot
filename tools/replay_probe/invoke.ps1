[CmdletBinding()]
param(
    [ValidateSet('check', 'probeReplay')]
    [string]$Task = 'probeReplay',

    [string]$Replay,

    [string]$Output
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$artifacts = @(
    @{ Group = 'com.skadistats'; Artifact = 'clarity'; Version = '4.0.1' },
    @{ Group = 'com.skadistats'; Artifact = 'clarity-protobuf'; Version = '6.1' },
    @{ Group = 'it.unimi.dsi'; Artifact = 'fastutil-core'; Version = '8.5.12' },
    @{ Group = 'org.xerial.snappy'; Artifact = 'snappy-java'; Version = '1.1.10.4' },
    @{ Group = 'org.slf4j'; Artifact = 'slf4j-api'; Version = '2.0.7' }
)

$cacheRoot = Join-Path $env:USERPROFILE '.gradle\caches\modules-2\files-2.1'
$localLibs = Join-Path $PSScriptRoot 'local-libs'
New-Item -ItemType Directory -Force -Path $localLibs | Out-Null

foreach ($spec in $artifacts) {
    $artifactRoot = Join-Path $cacheRoot "$($spec.Group)\$($spec.Artifact)\$($spec.Version)"
    $fileName = "$($spec.Artifact)-$($spec.Version).jar"
    $source = Get-ChildItem -LiteralPath $artifactRoot -Recurse -Filter $fileName -File |
        Select-Object -First 1
    if ($null -eq $source) {
        throw "Required local artifact is not cached: $($spec.Group):$($spec.Artifact):$($spec.Version)"
    }
    Copy-Item -LiteralPath $source.FullName -Destination (Join-Path $localLibs $fileName) -Force
}

$gradlePattern = Join-Path $env:USERPROFILE '.gradle\wrapper\dists\gradle-8.14.4-bin\*\gradle-8.14.4\bin\gradle.bat'
$gradle = Get-ChildItem -Path $gradlePattern -File | Select-Object -First 1
if ($null -eq $gradle) {
    throw 'The already-local Gradle 8.14.4 distribution was not found; no download was attempted.'
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$arguments = @('--offline', '--no-daemon', '-p', $PSScriptRoot, $Task)
if ($Task -eq 'probeReplay') {
    if ([string]::IsNullOrWhiteSpace($Replay) -or [string]::IsNullOrWhiteSpace($Output)) {
        throw 'probeReplay requires -Replay and -Output.'
    }
    $replayInput = if ([IO.Path]::IsPathRooted($Replay)) { $Replay } else { Join-Path $repoRoot $Replay }
    $outputInput = if ([IO.Path]::IsPathRooted($Output)) { $Output } else { Join-Path $repoRoot $Output }
    $replayPath = (Resolve-Path -LiteralPath $replayInput).Path
    $outputPath = [IO.Path]::GetFullPath($outputInput)
    $arguments += "-Preplay=$replayPath"
    $arguments += "-Poutput=$outputPath"
}

Push-Location $repoRoot
try {
    & $gradle.FullName @arguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}
