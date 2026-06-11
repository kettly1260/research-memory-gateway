param(
    [string]$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path,
    [string]$KiloSkillsRoot = (Join-Path $env:USERPROFILE ".config\kilo\skills"),
    [string]$PromptTargetDir = "",
    [switch]$SkipKilo
)

$ErrorActionPreference = "Stop"

function Copy-FileChecked {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Source file not found: $Source"
    }

    $parent = Split-Path -Parent $Destination
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    "Copied: $Source -> $Destination"
}

$skillSource = Join-Path $RepoRoot "skills\research-memory-gateway\SKILL.md"
$promptSource = Join-Path $RepoRoot "prompts\research-memory-system-prompt.md"

if (-not $SkipKilo) {
    $kiloSkillDir = Join-Path $KiloSkillsRoot "research-memory-gateway"
    $kiloSkillTarget = Join-Path $kiloSkillDir "SKILL.md"
    Copy-FileChecked -Source $skillSource -Destination $kiloSkillTarget
}

if ($PromptTargetDir) {
    if (-not (Test-Path -LiteralPath $PromptTargetDir)) {
        New-Item -ItemType Directory -Force -Path $PromptTargetDir | Out-Null
    }

    $promptTarget = Join-Path $PromptTargetDir "research-memory-system-prompt.md"
    Copy-FileChecked -Source $promptSource -Destination $promptTarget
}

"Client asset sync complete. Run this after git pull when clients use copied skills or prompts."
