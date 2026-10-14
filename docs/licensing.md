# Licensing and the reason for AGPLv3

[Overview](../README.md) · [Full license](../LICENSE) · [Third-party notices](third-party-notices.md) · [Roadmap](roadmap.md)

## Current decision

Version 0.51.0 licenses the project-owned code under **GNU AGPL version 3 only**,
with the SPDX identifier **AGPL-3.0-only**. The maintainer intends to make the
project available free of charge, with public source code and no requirement to
purchase a project license. The full, unmodified license is in [LICENSE](../LICENSE).

The practical reason for this choice is the existing **PyMuPDF 1.26.4** dependency,
used for PDF text extraction and page previews. PyMuPDF offers GNU AGPLv3 or an
Artifex commercial license. This release follows the open-source AGPL route;
it does not assume a purchased commercial license. See the
[official PyMuPDF licensing explanation](https://pymupdf.io/licensing).

MIT and Apache 2.0 were also considered. Placing one of those licenses on our
original files would not remove the AGPL obligations of the current combined
application. AGPLv3 is therefore the direct open-source path for this release
without changing the PDF implementation or purchasing additional rights.

## What this means

- You can use the software without paying the maintainer, inspect it, modify it,
  and redistribute it subject to the license.
- Distribution of covered modified versions carries corresponding-source and
  license obligations. Section 13 additionally covers users interacting remotely
  over a network with a modified version that supports such interaction.
- Commercial use and paid services are permitted by AGPLv3; the maintainer's
  free-distribution intention is not a restriction on other people's commercial use.
- These obligations concern the covered software and its corresponding source.
  They do not require publication of Telegram conversations, credentials or sessions.
- Third-party components retain their own licenses. Project licensing does not
  grant ownership of their code, notices, names or trademarks.

The [license text](../LICENSE) governs; this explanation is a summary.

## Possible future change

The maintainer is considering replacing PyMuPDF with a PDF implementation under
compatible permissive terms, which could make an MIT or Apache 2.0 release possible.
**No replacement, new license or migration date is committed in 0.51.0.** The current
PDF functionality and pinned dependency remain unchanged.

Before a different license is applied to a future release, the project must:

1. Choose and validate the replacement's extraction, rendering and safety behavior.
2. Review the replacement and transitive dependency licenses and notices.
3. Confirm that the rights to project contributions permit the proposed relicensing;
   replacing one dependency alone does not obtain other contributors' permission.
4. State the license of the new release explicitly.

A future decision cannot withdraw the AGPL rights already granted for copies
released under this version. Existing 0.51.0 copies remain available under AGPLv3.

## Sources and notices

- [GNU AGPLv3 text](https://www.gnu.org/licenses/agpl-3.0.html)
- [PyMuPDF licensing](https://pymupdf.io/licensing)
- [PyMuPDF 1.26.4 source](https://github.com/pymupdf/PyMuPDF/tree/1.26.4)
- [Third-party reference inventory and notices](third-party-notices.md)

The public source tree and source distribution contain the project source and
build instructions. Separately installed dependencies and external runtime tools
must retain their respective upstream licenses and corresponding-source obligations.
# Unreleased bundled candidate inventory

The `0.52.0.dev0` private bundle vendors standalone CPython/PBS, all 32 exact
runtime distributions, installer-only tomlkit 0.13.3, TDLib/OpenSSL and an adapted
native verifier under this project's selected AGPL license. The bundle keeps
full upstream notices, the four installed SBOMs, available project/verifier
build sources and exact TDLib d1085f9 tracked source without Git history.
Its relative-path component inventory explicitly marks source completeness
as incomplete. Nonvendoring-wheel notices below do not prove this bundle's
corresponding-source or redistribution completeness.

Cryptography's installed SBOM declares embedded OpenSSL 4.0.2, distinct from
TDLib's relocated OpenSSL 3.6.4. CPython/PBS embedded build dependencies,
native wheel build sources/recipes, embedded-library mappings and complete
corresponding-source acceptance remain open. No publication or source-complete
claim follows from private technical assembly. Available sources and notices
are preserved; missing files require applicable retrieval/publication authority.
