/**
 * The theme in `index.css` stays readable, and the icon stays its color (#56).
 *
 * jsdom has no layout and no CSS engine, so this reads the tokens straight out
 * of the stylesheet, converts OKLCH to sRGB, and holds every pair of a text
 * token on the surface it is drawn on to WCAG AA (4.5:1) in both schemes. A
 * tweak to a token that made muted text unreadable fails here, not in review.
 */
import { describe, expect, it } from 'vitest'

import indexHtml from '../index.html?raw'
import faviconSvg from '../public/favicon.svg?raw'

// `vite.config.ts` lets this one stylesheet through vitest's CSS stub.
import css from './index.css?raw'

type Tokens = Record<string, string>

/** The custom properties declared directly inside `block`. */
function tokensIn(block: string): Tokens {
  const tokens: Tokens = {}
  for (const match of block.matchAll(/--([a-z0-9-]+):\s*([^;]+);/g)) {
    tokens[match[1] as string] = (match[2] as string).trim()
  }
  return tokens
}

/** The body of the first `{ ... }` after `start`, matched brace for brace. */
function blockAfter(start: number): string {
  if (start < 0) throw new Error('index.css is missing a block this test reads')
  const open = css.indexOf('{', start)
  let depth = 0
  for (let index = open; index < css.length; index += 1) {
    if (css[index] === '{') depth += 1
    if (css[index] === '}') depth -= 1
    if (depth === 0) return css.slice(open + 1, index)
  }
  throw new Error('unbalanced braces in index.css')
}

const light = tokensIn(blockAfter(css.indexOf(':root {')))
const dark = {
  ...light,
  ...tokensIn(blockAfter(css.indexOf('@media (prefers-color-scheme: dark)'))),
}

/** Linear-light sRGB channels of an `oklch(L C h)` value, clamped to gamut. */
function linearRgb(value: string): [number, number, number] {
  const match = /^oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)$/.exec(value)
  if (!match) throw new Error(`not an opaque oklch() color: ${value}`)
  const [lightness, chroma, hue] = match.slice(1).map(Number) as [number, number, number]
  const a = chroma * Math.cos((hue * Math.PI) / 180)
  const b = chroma * Math.sin((hue * Math.PI) / 180)
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3
  const clamp = (x: number) => Math.min(1, Math.max(0, x))
  return [
    clamp(4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s),
    clamp(-1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s),
    clamp(-0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s),
  ]
}

function luminance(value: string): number {
  const [r, g, b] = linearRgb(value)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(foreground: string, background: string): number {
  const one = luminance(foreground)
  const other = luminance(background)
  return (Math.max(one, other) + 0.05) / (Math.min(one, other) + 0.05)
}

function hex(value: string): string {
  const encode = (x: number) => (x <= 0.0031308 ? 12.92 * x : 1.055 * x ** (1 / 2.4) - 0.055)
  const channels = linearRgb(value).map((channel) =>
    Math.round(255 * encode(channel))
      .toString(16)
      .padStart(2, '0'),
  )
  return `#${channels.join('')}`
}

/** Every text token, on each surface the kit draws it on. */
const PAIRS: [text: string, surface: string][] = [
  ['foreground', 'background'],
  ['card-foreground', 'card'],
  ['popover-foreground', 'popover'],
  ['primary-foreground', 'primary'],
  ['secondary-foreground', 'secondary'],
  ['accent-foreground', 'accent'],
  ['muted-foreground', 'background'],
  ['muted-foreground', 'card'],
  ['muted-foreground', 'muted'],
  ['destructive', 'background'],
  ['destructive', 'card'],
  ['primary', 'background'],
  ['sidebar-foreground', 'sidebar'],
  ['sidebar-accent-foreground', 'sidebar-accent'],
  ['sidebar-primary-foreground', 'sidebar-primary'],
]

describe.each([
  ['light', light],
  ['dark', dark],
] as const)('the %s theme', (_, tokens) => {
  it.each(PAIRS)('keeps %s on %s at WCAG AA', (text, surface) => {
    const foreground = tokens[text]
    const background = tokens[surface]
    if (foreground === undefined || background === undefined) {
      throw new Error(`index.css has no --${text} or --${surface}`)
    }
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(4.5)
  })
})

describe('the app icon', () => {
  it('is drawn in the theme’s primary', () => {
    const fill = /<rect[^>]*fill="(#[0-9a-f]{6})"/.exec(faviconSvg)?.[1]
    expect(fill).toBe(hex(light.primary as string))
  })

  it('is linked from index.html as an SVG', () => {
    expect(indexHtml).toContain('<link rel="icon" type="image/svg+xml" href="/favicon.svg" />')
  })
})
