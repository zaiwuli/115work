# Third Party Notices

## UnpackFlow / Unpackerr

Behavioral port and copied extraction/name-mapping source based on reference commit
`349f58940e031be42f1fc1253579d05c9bb733f2` of
https://github.com/zaiwuli/unpackflow .

Copyright (c) 2018-2022 David Newhall II
Copyright (c) 2023-2026 Go Lift - Building Strong Go Tools

Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
"Software"), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR
ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The vendored `third_party/xtractr` decoder and `extractor/path_mapper.go`
are copied from this reference. Original notices are retained in
`third_party/xtractr/LICENSE` and `third_party/UNPACKFLOW_LICENSE`.
Host-policy safety hooks are project modifications. Transitive decoder
licenses are retained under `third_party/licenses/`.

## p115client

Runtime dependency, pinned to commit
`856e4f8f09ce034534bd458f06d3dee4474a1698`.
https://github.com/ChenyangGao/p115client

MIT License

Copyright (c) 2024 ChenyangGao <https://github.com/ChenyangGao>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Other Dependencies

CloudDrive2 gRPC bindings are generated from the vendor protocol definition
`https://www.clouddrive2.com/clouddrive.proto`, retrieved on 2026-10-12.
Protocol option version: 1.1.2. SHA256 of the original definition:
`A00B4620CBCE0A9BD6009F8E771BA68EA7375080CA7F15F4C7C206310BA2EC9D`.
The vendor protocol is not represented as project-owned MIT code. Redistribution
terms must be confirmed before a public release.

7-Zip is an external extraction executable. Windows uses the installed tool;
container builds use the distribution package. Its bundled LGPL/BSD/unRAR
notices and format restrictions must accompany release images.

Vue, Pinia, Vue Router, Vite, Vuetify, Lucide and Playwright are consumed through
their published packages. Their packaged license files are retained by the
dependency manager. MoviePilot is a layout reference only; its branding, images
and application code have not been copied.
