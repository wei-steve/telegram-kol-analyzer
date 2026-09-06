# Mobile Page Scroll Fix Design

## Problem

On phone browsers, the dashboard can become unscrollable because the mobile layout makes the application root a fixed-height nested scrolling container. Android browser touch handling is less reliable for this pattern, especially alongside fixed controls.

## Decision

At mobile widths, let the document page be the only primary scroll container. Remove the viewport-height lock and root vertical overflow from `.trader-layout`; retain bottom padding that clears the fixed navigation and safe-area inset. The fixed mobile navigation remains unchanged.

## Scope and Validation

The change applies only below 760px and does not change desktop layout, data, routes, or navigation behavior. A static CSS regression test will assert that the mobile rule does not reintroduce fixed viewport height or nested vertical scrolling while retaining bottom safe-area padding.
