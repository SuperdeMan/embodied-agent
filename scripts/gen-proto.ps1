# 从协议生成 Python 存根到忽略的 gen/python，使用项目 RPC 组工具，无需全局安装。
# Generate Python stubs from proto/ into gen/python (gitignored).
# Uses grpcio-tools from the uv "rpc" dependency group (D011: no global buf install).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force "gen/python" | Out-Null
# 包含路径与文件均使用绝对路径，protoc 要求包含路径是输入文件的精确字符串前缀。
# Absolute include path + absolute file paths: protoc requires the -I to be an EXACT
# string prefix of each input file (relative -I with absolute files does not match).
$protoRoot = Join-Path $root "proto"
$protos = Get-ChildItem -Recurse -Filter *.proto $protoRoot | ForEach-Object { $_.FullName }
uv run --group rpc python -m grpc_tools.protoc -I $protoRoot --python_out=gen/python --grpc_python_out=gen/python --pyi_out=gen/python @protos
if ($LASTEXITCODE -ne 0) { throw "protoc failed with exit code $LASTEXITCODE" }
Write-Host "generated $($protos.Count) proto file(s) -> gen/python"
