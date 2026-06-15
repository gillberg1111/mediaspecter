# MediaSpecter — Logo & Style System

A technical reference for implementing the MediaSpecter brand in the web app.
Theme is dark-only: mint–teal accent on near-black, with a translucent "ghost"
green used for the word *Specter* (and the word *ghost* in copy).

---

## 1. Design tokens

Drop these into a global stylesheet (`:root`). Everything else references them.

```css
:root {
  /* Surfaces (dark only) */
  --ms-bg:            #080B0A;   /* page background */
  --ms-bg-elevated:   #0E1413;   /* cards / panels */
  --ms-surface:       #0C1110;   /* nested surfaces, sidebar */

  /* Brand greens */
  --ms-mint:          #3ECF8E;   /* primary accent (links, active states, dots) */
  --ms-mint-from:     #54E2A2;   /* icon gradient — top */
  --ms-mint-to:       #1FB9AD;   /* icon gradient — bottom */
  --ms-specter:       rgba(62, 207, 142, 0.6); /* translucent "ghost" green */

  /* Text */
  --ms-text:          #F3F7F5;   /* primary text + the word "Media" */
  --ms-text-muted:    #94A39D;   /* body / secondary */
  --ms-text-dim:      #5F6F6A;   /* labels, captions, mono kickers */

  /* Lines */
  --ms-border:        rgba(255, 255, 255, 0.06);
  --ms-border-mint:   rgba(62, 207, 142, 0.16);

  /* Type */
  --ms-font:          'Space Grotesk', system-ui, -apple-system, sans-serif;
  --ms-font-mono:     'JetBrains Mono', ui-monospace, monospace;
}
```

> **Accent vs. gradient:** flat `--ms-mint` (#3ECF8E) is the UI accent (dots,
> active nav, progress). The two-stop gradient (`--ms-mint-from` → `--ms-mint-to`,
> top→bottom) is reserved for the **icon glyph** only.

---

## 2. Typography

| Role            | Family         | Weight | Tracking   | Notes |
|-----------------|----------------|--------|------------|-------|
| Display / H1    | Space Grotesk  | 600    | `-0.04em`  | page titles |
| Section heading | Space Grotesk  | 600    | `-0.02em`  | |
| Body            | Space Grotesk  | 400–500| normal     | use `--ms-text-muted` |
| Mono kicker/label | JetBrains Mono | 500  | `0.15em`, uppercase | small caps-style labels |

```css
h1, .ms-display { font-family: var(--ms-font); font-weight: 600;
  letter-spacing: -0.04em; color: var(--ms-text); line-height: 1.05; }

.ms-kicker { font-family: var(--ms-font-mono); font-size: 12px;
  letter-spacing: 0.15em; text-transform: uppercase; color: var(--ms-mint); }
```

Load the fonts:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

---

## 3. The wordmark

`Media` is solid white at weight 500; `Specter` is the translucent ghost green at
weight 600. They sit tight together with no space.

```html
<span class="ms-logo">
  <span class="ms-logo__media">Media</span><span class="ms-logo__specter">Specter</span>
</span>
```

```css
.ms-logo            { font-family: var(--ms-font); font-weight: 600;
                      letter-spacing: -0.04em; line-height: 1; white-space: nowrap; }
.ms-logo__media     { color: var(--ms-text); font-weight: 500; }
.ms-logo__specter   { color: var(--ms-specter); }
```

**Compact form** (sidebar, where the product is shown as just "Specter"): use the
icon + `Specter` in the ghost green. Bump opacity to `0.66` at small sizes so it
stays legible:

```html
<span class="ms-logo__specter" style="color: rgba(62,207,142,0.66);">Specter</span>
```

---

## 4. The "ghost" highlight rule

Any time the literal word **ghost** appears in brand copy (taglines, hero text),
render it in the same translucent green as *Specter*. Reuse one class:

```css
.ms-ghost { color: var(--ms-specter); font-weight: 600; }
```

```html
<p class="ms-subtagline">A <span class="ms-ghost">ghost</span> of what you watched.</p>
```

Do **not** apply it to the word "ghost" inside running technical text (logs,
docs) — only in marketing/brand copy.

---

## 5. Taglines

```html
<h1 class="ms-display ms-tagline">Reclaim your space. Keep your library.</h1>
<p class="ms-subtagline">A <span class="ms-ghost">ghost</span> of what you watched.</p>
```

```css
.ms-tagline    { font-size: clamp(26px, 3.4vw, 45px); letter-spacing: -0.027em;
                 line-height: 1.08; white-space: nowrap; }
.ms-subtagline { font-size: clamp(16px, 1.6vw, 23px); font-weight: 500;
                 color: var(--ms-text-muted); letter-spacing: -0.01em;
                 margin-top: 18px; }
```

---

## 6. Logo assets

| File                                | Use |
|-------------------------------------|-----|
| `assets/mediaspecter-icon.svg`      | Transparent ghost glyph (knockout eyes). Sidebar mark, inline UI, anywhere on a dark surface. |
| `assets/mediaspecter-icon-tile.svg` | Glyph on the dark rounded tile. Favicon, PWA/app icon, social. |
| `assets/mediaspecter-banner.png`    | README / social banner. |

The glyph is pure geometry (no font) — safe to inline. Source:

```html
<svg viewBox="0 0 100 100" role="img" aria-label="MediaSpecter" width="100" height="100">
  <defs>
    <linearGradient id="msFill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#54E2A2"/>
      <stop offset="1" stop-color="#1FB9AD"/>
    </linearGradient>
    <mask id="msEyes">
      <path d="M23 46A27 27 0 0 1 77 46L77 80A9 8 0 0 1 59 80A9 8 0 0 1 41 80A9 8 0 0 1 23 80Z" fill="#fff"/>
      <circle cx="40" cy="43" r="4.6" fill="#000"/>
      <circle cx="60" cy="43" r="4.6" fill="#000"/>
    </mask>
  </defs>
  <rect width="100" height="100" fill="url(#msFill)" mask="url(#msEyes)"/>
</svg>
```

**Icon container** (when the glyph sits in a chip/tile in the UI):

```css
.ms-icon-tile {
  display: grid; place-items: center;
  width: 40px; height: 40px; border-radius: 12px;
  background: linear-gradient(150deg, #15231F, #080C0B);
  border: 1px solid var(--ms-border-mint);
}
/* glyph inside is ~60% of the tile */
```

Favicon (modern browsers accept SVG):

```html
<link rel="icon" type="image/svg+xml" href="/assets/mediaspecter-icon-tile.svg">
```

---

## 7. Do / Don't

**Do**
- Keep the icon glyph solid mint→teal — it's the legible anchor.
- Keep `Media` white and `Specter` translucent green; never swap them.
- Use `--ms-specter` for both `Specter` and the brand word "ghost".
- Stay on dark surfaces (`--ms-bg` family).

**Don't**
- Don't put the icon on a light background without the dark tile.
- Don't drop `Specter`/"ghost" opacity below ~0.5 (illegible) or raise to 1.0 (loses the effect).
- Don't recolor the glyph gradient or add drop shadows to the wordmark.
- Don't introduce a second accent hue — the brand is single-accent (mint).
