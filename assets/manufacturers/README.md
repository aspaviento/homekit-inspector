# Manufacturer logos

The original monochrome SVG sources are vendored from
[Simple Icons](https://github.com/simple-icons/simple-icons) at revision
`90646e4bf7f3b89df6c37e65baf3e7bc759a517c`. The upstream CC0 license and
disclaimer are included in this directory. Brand marks remain the property of
their respective owners; inclusion identifies recorded manufacturers and does
not imply endorsement. Upstream per-icon metadata lists no additional license
for these selected assets.

The upstream sources are Apple (apple.com), Shelly (shelly.com), Ring
(store.ring.com/press), Siemens (siemens.com), Philips Hue
(philips-hue.com/en-us/support/faq), Samsung
(samsung.com/us/about-us/brand-identity/logo/), LG
(lg.com/global/our-brand/brand-expression/elements/logo/index.jsp), Sonos
(sonos.com/en-gb/home), Home Assistant (github.com/home-assistant/assets), and
Homebridge (github.com/homebridge/branding).

`catalog.json` maps explicit manufacturer values to asset filenames. Matching
ignores case and repeated whitespace but does not inspect accessory names or
models. Unknown manufacturers appear as text. Missing manufacturer values are
omitted. In generated reports the manufacturer value is retained verbatim for
tooltips and accessible labels, even when an alias selects a logo.

Only logos used by the report are embedded as data URLs. Viewing the report
does not fetch logos from any server. The CLI installer includes this directory.
Reports embed transparent PNGs rasterized from the SVG sources. CSS renders
the marks in dark or light monochrome according to the color scheme. Accessory
cards place compact logos in the upper-right; manufacturer headings use larger
logos next to the group name.

The Signify logo is sourced separately from the
[official Signify website](https://www.signify.com/content/dam/signify-multibrand/master/navigation/logo/signify-logo.svg).
It is a Signify trademark, not part of the Simple Icons collection. It identifies
explicit Signify manufacturer values without substituting a Philips Hue logo.
Manufacturer group headers use a larger logo than individual accessory cards.

The Eufy wordmark was extracted from the inline `logoSvg` in the header of the
[official Eufy website](https://www.eufy.com/) on 2026-10-30. Its SVG
`currentColor` fills are normalized to black before rasterization. It is an
Anker/Eufy trademark and is not part of the Simple Icons collection.
