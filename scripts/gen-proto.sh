#!/usr/bin/env bash
# Generate Python stubs from proto/ into gen/python (gitignored).
# Uses grpcio-tools from the uv "rpc" dependency group (D011: no global buf install).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p gen/python
mapfile -t protos < <(find proto -name '*.proto')
uv run --group rpc python -m grpc_tools.protoc -I proto \
  --python_out=gen/python --grpc_python_out=gen/python --pyi_out=gen/python \
  "${protos[@]}"
echo "generated ${#protos[@]} proto file(s) -> gen/python"
