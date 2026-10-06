param(
    [Parameter(Mandatory = $true)]
    [string]$TargetImage,
    [int]$HostPort = 3000,
    [int]$ContainerPort = 3000
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:REDTEAM_TARGET_IMAGE = $TargetImage
$env:REDTEAM_TARGET_HOST_PORT = "$HostPort"
$env:REDTEAM_TARGET_CONTAINER_PORT = "$ContainerPort"
docker compose -f deploy/docker/compose.testbed.yaml up --build
