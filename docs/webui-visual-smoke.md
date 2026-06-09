# WebUI Visual Smoke Notes

Use these smoke checks after enabling the optional WebUI. The WebUI must remain local-assets-only and must not request CDN resources.

## Viewports

- Desktop: 1440 x 900. Expect left sidebar, topbar, table memory list, split-friendly detail cards, and readable dense data panels.
- Tablet: 900 x 1024. Expect sidebar to collapse into a top block, filters to become single-column, and detail sections to stack vertically.
- Mobile: 390 x 844. Expect memory table hidden, memory cards visible, single-column forms, sticky compact topbar, and no horizontal page overflow.

## Pages

- `/admin`: dashboard cards and retrieval status.
- `/admin/memories`: desktop table and mobile cards.
- `/admin/memories/new`: structured creation form and advanced JSON textarea.
- `/admin/memories/{memory_id}`: detail, edit JSON, status actions, and deleted-state Danger Zone.
- `/admin/config`: effective config, runtime config form, encrypted secret form.
- `/admin/config/nocturne`: reserved/test-only status and config/token forms.
- `/admin/import`: JSON validate/execute form and Markdown planned notes.
- `/admin/exports`: JSON/Markdown/both export form with include flags.

## Security/Asset Checks

- Browser network panel should show only `/admin/*` requests from the same origin.
- No `cdn`, `unpkg`, `jsdelivr`, `googleapis`, or external font host requests should appear.
- API keys/tokens should never appear in HTML `value` attributes, effective config JSON, audit output, or export output.
- CSP response header should include `default-src 'self'`, `img-src 'self' data:`, `connect-src 'self'`, and `frame-ancestors 'none'`.

## Accessibility/Readability

- Body text remains readable with glass surfaces in light and dark themes.
- Danger Zone uses high-contrast error styling.
- `prefers-reduced-transparency: reduce` removes backdrop blur and uses solid surfaces.
