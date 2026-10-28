#!/bin/bash
# Builds the server as one more target of ClickHouse's build.
#
#   ./build.sh                 fetches ClickHouse into ./ClickHouse (once) and builds against it
#   ./build.sh ~/src/ClickHouse  builds against an existing ClickHouse checkout, with its submodules
#
# The first build compiles most of ClickHouse and takes a long while; later ones only rebuild main.cpp.
set -euo pipefail
cd "$(dirname "$0")"

CLICKHOUSE_TAG=v26.10.2.103-stable

if [ $# -gt 0 ]; then
    source=$(realpath "$1")
    [ -f "$source/CMakeLists.txt" ] && [ -d "$source/src/Parsers" ] || { echo "$1 is not a ClickHouse source tree" >&2; exit 1; }
else
    source=$PWD/ClickHouse
    if [ ! -d "$source" ]; then
        git clone --depth 1 --branch "$CLICKHOUSE_TAG" https://github.com/ClickHouse/ClickHouse.git "$source"
        "$source/contrib/update-submodules.sh"
    fi
fi

build=$PWD/build
if [ -f "$build/CMakeCache.txt" ] && ! grep -qx "CMAKE_HOME_DIRECTORY:INTERNAL=$source" "$build/CMakeCache.txt"; then
    echo "build/ was configured for another ClickHouse source tree: remove it to build against $source" >&2
    exit 1
fi

if [ ! -f "$build/build.ninja" ]; then
    # macOS: brew install llvm lld ninja findutils grep ccache, then put llvm first in PATH:
    #   export PATH="$(brew --prefix llvm)/bin:$PATH"
    cmake -S "$source" -B "$build" -G Ninja \
        -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
        -DCMAKE_BUILD_TYPE=RelWithDebInfo \
        -DENABLE_LIBRARIES=0 -DENABLE_RUST=0 -DENABLE_EMBEDDED_COMPILER=0 -DENABLE_TESTS=0 \
        -DCMAKE_PROJECT_ClickHouse_INCLUDE="$PWD/clickhamlet.cmake"
fi

ninja -C "$build" clickhamlet
echo "Built $build/clickhamlet. Run it with: $build/clickhamlet 9001"
