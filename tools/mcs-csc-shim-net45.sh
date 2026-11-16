#!/bin/bash
# csc-style command line -> Mono mcs, compiled against strict .NET Framework 4.5 REFERENCE assemblies
# (not Mono's own runtime library, which has newer overloads such as String.TrimEnd(char) that
# .NET Framework on Windows does not have).
API=/opt/homebrew/Cellar/mono/6.14.1/lib/mono/4.5-api
args=(-nostdlib -noconfig "-lib:$API" -r:mscorlib.dll -r:System.dll -r:System.Core.dll)
for a in "$@"; do
  if [ "$a" = "/nologo" ]; then continue; fi
  if [[ "$a" =~ ^/[A-Za-z0-9]+([:+-].*)?$ ]]; then args+=("-${a#/}"); else args+=("$a"); fi
done
exec /opt/homebrew/bin/mcs "${args[@]}"
