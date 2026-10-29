# PyMuPDF / MuPDF source distribution

This local Beta includes PyMuPDF 1.28.2 and MuPDF. The installed package declares AGPLv3 or an Artifex commercial license. No separate commercial entitlement was established in this review. The full [GNU AGPLv3 text](licenses/AGPL-3.0.txt) is supplied; the installed wheel's `COPYING` file contains only a short dual-license declaration and is also retained.

The original Pocket Memory source keeps its Apache-2.0 copyright and license notices. That does not make a binary incorporating AGPL-covered code Apache-only. For a combined distribution using the AGPL route, apply the AGPL requirements to the covered combined work while retaining original permissive notices. Apache-2.0 permits inclusion in a version-3 copyleft work; it does not let a distributor remove the copyleft terms. See the [ASF compatibility explanation](https://www.apache.org/licenses/GPL-compatibility.html), [GNU compatibility discussion](https://www.gnu.org/licenses/license-compatibility.en.html), and [Artifex licensing guidance](https://artifex.com/licensing/).

## Exact locally recorded upstream build identity

The installed `pymupdf/_build.py` records:

- PyMuPDF version: `1.28.2`.
- PyMuPDF commit: `12786d12b2962a1d77fd13b103162eddf749644c`, branch `aptest-jules`; recorded git diff is empty.
- MuPDF source archive: <https://mupdf.com/downloads/archive/mupdf-1.28.2-source.tar.gz>.
- SWIG version: `4.4.1`.

Pin the source to that recorded commit and MuPDF archive, rather than assuming that a moving default branch or a similarly named release matches this installed wheel. [PyMuPDF source commit](https://github.com/pymupdf/PyMuPDF/commit/12786d12b2962a1d77fd13b103162eddf749644c). Its public accessibility and correspondence to all native binaries must be checked when preparing the source delivery. A wheel or installed `.py` files alone are not the complete source of the native MuPDF component.

## Concrete approach for a later binary delivery

For an electronic download, place a complete matching corresponding-source archive beside the binary download and identify it clearly in the package README. Include the exact Pocket Memory application/frontend sources, modifications, dependency versions, build spec and scripts, relevant interface files, and the PyMuPDF/MuPDF source and build material needed for the covered work. Include the licenses and source provenance for other bundled components that impose their own source obligations, such as GEOS when bundled through Shapely. A source tree that omits the newly integrated runtime, or just links to the repository's latest branch, does not establish correspondence to the Beta executable.

AGPL section 6(d) allows source access from another server when clear directions are next to the object-code download and the arrangement actually provides the required source. The distributor remains responsible for that availability. Exact third-party URLs and hashes are useful for reproducibility, but an unverified list of homepages is not a substitute for complete corresponding source. Keeping a verified source archive alongside the release avoids depending solely on moving upstream references.

If distribution instead relies on a written source offer under section 6(b), that provision has specific media, recipient, duration and fulfillment conditions; a generic “contact us for source” note is not automatically such an offer. Use direct corresponding-source delivery for the intended downloadable Beta when possible. The project [issue tracker](https://github.com/AshleySama/pocket-memory-agent/issues) is a contact route, not evidence that a source offer has already been fulfilled.

This document records a source-delivery procedure and the local build identity. It does not state that an uncreated source archive exists, confer an Artifex commercial license, or certify a public binary release. No product source was relicensed or altered by this notice review. The current task delivers a local Beta and uploads application source only; it does not publish a public binary release.
