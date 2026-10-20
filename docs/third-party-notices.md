# Third-party dependencies and notices

[Overview](../README.md) · [Project license](../LICENSE) · [Licensing rationale](licensing.md)

The project-owned code uses AGPL-3.0-only. Dependencies retain their own copyrights
and licenses; this project's license does not replace their terms. The wheel does
not vendor the separately installed dependency implementations. The following
reference inventory and copied upstream notices accompany the source and package.

## Direct dependencies

The 0.52.0 private standalone bundle (not distributed) additionally vendors the exact
32-distribution closure, standalone CPython/PBS, pinned TDLib/OpenSSL and
installer-only tomlkit 0.13.3. Its full copied notices, relative component
inventory and four installed SBOMs accompany the archive. Cryptography's SBOM
declares OpenSSL 4.0.2 separately from TDLib's OpenSSL 3.6.4. Available project,
verifier and exact TDLib source is included; source/embedded-build completeness
remains explicitly incomplete. This wheel-oriented table does not close that
bundle's redistribution or corresponding-source gate.

| Dependency | Pinned version | License / notice | Upstream source |
| --- | --- | --- | --- |
| MCP Python SDK (`mcp`) | 2.2.0 | MIT; [verbatim notice](licenses/mcp-MIT.txt) | [python-sdk](https://github.com/modelcontextprotocol/python-sdk) |
| PyMuPDF | 1.26.4 | AGPLv3 or Artifex commercial; this project uses the AGPL route. [Installed notice](licenses/PyMuPDF-COPYING.txt), [full AGPLv3](../LICENSE) | [1.26.4 source](https://github.com/pymupdf/PyMuPDF/tree/1.26.4), [licensing](https://pymupdf.io/licensing) |
| python-docx | 1.2.0 | MIT; [verbatim notice](licenses/python-docx-MIT.txt) | [python-docx](https://github.com/python-openxml/python-docx) |
| Pillow | 11.3.0 | MIT-CMU with bundled third-party notices; [verbatim license collection](licenses/Pillow.txt) | [Pillow](https://github.com/python-pillow/Pillow) |
| segno | 1.6.6 | BSD-3-Clause; [verbatim notice](licenses/segno-BSD.txt). Renders the local login QR only. | [segno](https://github.com/heuer/segno) |
| setuptools (build backend) | 80.9.0 | MIT; installed separately for building | [setuptools](https://github.com/pypa/setuptools) |

The copied notices were taken unchanged from the pinned installed distributions.
Pillow's license collection includes its upstream component notices. PyMuPDF's
installed COPYING file identifies its dual-license terms; the open-source option
is selected here. This is not a purchased Artifex commercial license.

For redistribution of separately installed dependency binaries, preserve their
distributed license/notice files and satisfy their corresponding-source obligations.
Do not treat this direct-dependency inventory as a list of every transitive or
platform library contained in an arbitrary installation.

## External runtime and media prerequisites

TDLib is provisioned separately as the pinned Homebrew runtime described in
[installation](install.md). It is not vendored in the Python source or wheel.
Its upstream source and license are available from [tdlib/td](https://github.com/tdlib/td).

Optional ffmpeg/ffprobe/whisper-cli binaries and the Whisper model are likewise
external local prerequisites. Their license, provenance and redistribution terms
must be checked for the actual selected builds and model; this source release
does not bundle or download them.

## Project presentation

The banner is retained unchanged from the previously public repository. Dependency
licenses do not grant rights to Telegram trademarks or imply Telegram endorsement.
See [NOTICE](../NOTICE) for the independent-project statement.
