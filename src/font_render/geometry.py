"""Per-row glyph geometry, collected from the DOM after the screenshot."""

# Collects per-row glyph geometry from the DOM. Runs AFTER the screenshot is
# taken, so the zero-size baseline probe cannot affect the captured pixels.
# The probe is an inline-block of height 0: with `align-items: baseline` on
# the flex row its top edge sits exactly on the shared text baseline.
# Per-character advance boxes come from the Range API — no markup changes,
# fractional (subpixel) coordinates preserved.
GEOMETRY_JS = """
() => {
    const rows = [];
    document.querySelectorAll('.row[data-ppem]').forEach(rowEl => {
        const span = rowEl.querySelector('[class*="sample-"]');
        if (!span || !span.firstChild) return;
        const textNode = span.firstChild;
        const text = textNode.textContent;
        const probe = document.createElement('span');
        probe.style.cssText =
            'display:inline-block;width:0;height:0;padding:0;margin:0;';
        rowEl.appendChild(probe);
        const baseline = probe.getBoundingClientRect().y;
        probe.remove();
        const range = document.createRange();
        const glyphs = [];
        for (let i = 0; i < text.length; i++) {
            range.setStart(textNode, i);
            range.setEnd(textNode, i + 1);
            const r = range.getClientRects()[0];
            if (!r) continue;
            glyphs.push({i: i, char: text[i],
                         x0: r.x, x1: r.x + r.width,
                         y0: r.y, y1: r.y + r.height});
        }
        const b = span.getBoundingClientRect();
        rows.push({
            ppem: parseInt(rowEl.dataset.ppem),
            font: parseInt(rowEl.dataset.font || '1'),
            baseline_y: baseline,
            box: [b.x, b.y, b.x + b.width, b.y + b.height],
            glyphs: glyphs,
        });
    });
    return rows;
}
"""


def scale_geometry(rows: list[dict], factor: float) -> list[dict]:
    """Convert CSS-pixel coordinates to final-PNG pixels."""
    if factor == 1.0:
        return rows
    for row in rows:
        row["baseline_y"] *= factor
        row["box"] = [v * factor for v in row["box"]]
        for g in row["glyphs"]:
            for k in ("x0", "x1", "y0", "y1"):
                g[k] *= factor
    return rows
