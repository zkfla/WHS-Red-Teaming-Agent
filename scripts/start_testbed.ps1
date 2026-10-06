param(
    [Parameter(Mandatory = $true)]
    [string]$Image,
    [int]$HostPort = 3000,
    [int]$ContainerPort = 3000,
    [string]$Name = "redteaming-testbed"
)

$ErrorActionPreference = "Stop"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is not installed or is not on PATH. Install Docker Desktop first."
}

docker run --rm --name $Name -p "127.0.0.1:${HostPort}:${ContainerPort}" $Image
