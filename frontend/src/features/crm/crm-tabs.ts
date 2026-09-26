/**
 * The `/lists` page's tabs, and the `?tab=` that names one.
 *
 * Its own module so the route, the page, and the dashboard's links read one
 * list: a link that means "Tags and rules" says `?tab=tags` and lands there
 * (#134), rather than on the Lists tab one click away from what it named.
 */
export const CRM_TABS = ['lists', 'tags', 'views'] as const
export type CrmTab = (typeof CRM_TABS)[number]
export const DEFAULT_CRM_TAB: CrmTab = 'lists'

export interface CrmSearch {
  /** Left out for the default tab, so `/lists` stays `/lists`. */
  tab?: CrmTab
}

/** The search that opens `tab`: nothing at all for the default one. */
export function crmSearch(tab: CrmTab | undefined): CrmSearch {
  return tab === undefined || tab === DEFAULT_CRM_TAB ? {} : { tab }
}

/** Reads `?tab=`; anything unknown opens the default tab rather than failing the page. */
export function validateCrmSearch(input: Record<string, unknown>): CrmSearch {
  return crmSearch(CRM_TABS.find((candidate) => candidate === input.tab))
}
