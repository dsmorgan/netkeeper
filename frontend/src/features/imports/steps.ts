/** The wizard's order, which is also the order of spec 10.5. */
export const STEPS = ['upload', 'mapping', 'preview', 'candidates', 'commit'] as const

export type Step = (typeof STEPS)[number]

export const STEP_LABELS: Record<Step, string> = {
  upload: 'Upload',
  mapping: 'Map columns',
  preview: 'Preview',
  candidates: 'Candidates',
  commit: 'Commit',
}
