import { describe, expect, it } from 'vitest';
// @ts-expect-error The desktop package intentionally omits ambient Node globals.
import { readFileSync } from 'node:fs';

const css = readFileSync('src/app.css', 'utf8');

function readHexVariable(name: string): string {
  const match = css.match(new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match) throw new Error(`missing_css_variable:${name}`);
  return match[1];
}

function relativeLuminance(hex: string): number {
  const channels = hex
    .slice(1)
    .match(/.{2}/g)
    ?.map((value) => Number.parseInt(value, 16) / 255);
  if (!channels || channels.length !== 3) throw new Error(`invalid_hex_color:${hex}`);

  const [red, green, blue] = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return red * 0.2126 + green * 0.7152 + blue * 0.0722;
}

function contrastRatio(foreground: string, background: string): number {
  const lighter = Math.max(relativeLuminance(foreground), relativeLuminance(background));
  const darker = Math.min(relativeLuminance(foreground), relativeLuminance(background));
  return (lighter + 0.05) / (darker + 0.05);
}

describe('startup center CSS contracts', () => {
  it('keeps WebKit and Mozilla error progress selectors in separate rules', () => {
    const selectorLists = [...css.matchAll(/([^{}]+)\{/g)].map((match) => match[1]);
    const mixedVendorSelector = selectorLists.find(
      (selector) =>
        selector.includes('::-webkit-progress-value') && selector.includes('::-moz-progress-bar'),
    );

    expect(mixedVendorSelector).toBeUndefined();
  });

  it('keeps muted ink readable on the cold paper background', () => {
    const mutedInk = readHexVariable('ink-muted');
    const coldPaper = readHexVariable('paper-cool');

    expect(contrastRatio(mutedInk, coldPaper)).toBeGreaterThanOrEqual(4.5);
  });
});
