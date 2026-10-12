# Third-party dependency reference inventory

This file identifies declared dependency sources for distribution review. It does not grant a project license, reproduce every upstream notice, or establish legal clearance. The repository has no root LICENSE at the observed public baseline; project-license selection and final distribution notices remain owner decisions.

| Declared dependency | Pinned version | Primary upstream reference |
| --- | --- | --- |
| MCP Python SDK (`mcp`) | 2.2.0 | https://github.com/modelcontextprotocol/python-sdk |
| PyMuPDF | 1.26.4 | https://pymupdf.io/licensing |
| python-docx | 1.2.0 | https://github.com/python-openxml/python-docx |
| Pillow | 11.3.0 | https://github.com/python-pillow/Pillow |
| setuptools (build backend) | 80.9.0 | https://github.com/pypa/setuptools |

PyMuPDF's official licensing page describes AGPLv3 and commercial licensing paths. Including the dependency in a public package requires the owner to assess the applicable path and distribution obligations; this candidate does not select one. Final review must inspect the exact installed/distributed dependency artifacts and their license/notice files, including transitive components.

TDLib is provisioned separately as the pinned Homebrew runtime described in [installation](install.md); it is not vendored in this Python source export. Optional ffmpeg/ffprobe/whisper-cli binaries and the Whisper model are likewise external local prerequisites. Their licenses, provenance and any future redistribution must be reviewed separately. The banner is retained byte-for-byte from the previously public repository; retention does not independently establish its provenance rights.
