# Generate Python stubs from proto/ into gen/python (gitignored).
# Uses grpcio-tools from the uv "rpc" dependency group (D011: no global buf install).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force "gen/python" | Out-Null
$protos = Get-ChildItem -Recurse -Filter *.proto proto | ForEach-Object { $_.FullName }
uv run --group rpc python -m grpc_tools.protoc -I proto --python_out=gen/python --grpc_python_out=gen/python --pyi_out=gen/python @protos
Write-Host "generated $($protos.Count) proto file(s) -> gen/python"
