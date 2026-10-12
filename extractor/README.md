# Bundled Extractor

The decoding implementation is copied from UnpackFlow commit
`349f58940e031be42f1fc1253579d05c9bb733f2`, `third_party/xtractr`.
The long-name mapper in `path_mapper.go` is copied from that same repository.
Both original MIT license notices are retained in `third_party/`.

The helper reads one bounded JSON request from stdin, handles one extraction,
and returns a JSON result. Passwords never appear in command-line arguments.
It does not own the business queue, delete original files, or run a service.
Python retains task fencing, version checks, publication and cleanup ownership.

Build locally using Go 1.26.4:

```powershell
./scripts/build-extractor.ps1
```

Linux:

```sh
cd extractor
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o ../bin/115work-extract .
```

Docker builds the helper in a separate stage; Go is not installed in the runtime
image. Only the target architecture binary is copied.

Host-policy patches to xtractr reject unsafe paths, links/special files and
duplicate outputs, and enforce a cumulative streaming output budget.
One archive is decoded sequentially. The helper heap target is 512 MiB, not a
claim of a 512 MiB hard memory ceiling; the worker/container imposes hard limits.
Nested extraction matches the reference queue's single additional pass.
ISO recursion and same-name root squashing are explicit rule options.

The MS protocol compatibility work is separate from this helper.
