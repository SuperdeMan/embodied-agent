# Generate Python stubs from proto/ into gen/python (gitignored).
# Uses grpcio-tools from the uv "rpc" dependency group (D011: no global buf install).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force "gen/python" | Out-Null
# Absolute include path + absolute file paths: protoc requires the -I to be an EXACT
# string prefix of each input file (relative -I with absolute files does not match).
$protoRoot = Join-Path $root "proto"
$protos = Get-ChildItem -Recurse -Filter *.proto $protoRoot | ForEach-Object { $_.FullName }
uv run --group rpc python -m grpc_tools.protoc -I $protoRoot --python_out=gen/python --grpc_python_out=gen/python --pyi_out=gen/python @protos
if ($LASTEXITCODE -ne 0) { throw "protoc failed with exit code $LASTEXITCODE" }
Write-Host "generated $($protos.Count) proto file(s) -> gen/python"
