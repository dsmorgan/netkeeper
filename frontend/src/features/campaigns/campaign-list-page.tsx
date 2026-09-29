import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { EmptyState, ErrorNote, LoadingNote } from '@/features/crm/controls'

import { campaignsQuery } from './api'
import { CampaignStatusBadge } from './badges'
import { countsText, formatWhen } from './format'

/** Every campaign, newest first: status, enrollment counts, next send (P3-11a). */
export function CampaignListPage() {
  const campaigns = useQuery(campaignsQuery)

  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          A campaign sends a sequence of steps to an audience, after a review you walk through.
        </p>
        <Button render={<Link to="/campaigns/new" />}>New campaign</Button>
      </div>
      {campaigns.isPending ? (
        <LoadingNote label="Loading the campaigns…" />
      ) : campaigns.isError ? (
        <ErrorNote label="The campaigns are unavailable." error={campaigns.error} />
      ) : campaigns.data.length === 0 ? (
        <EmptyState title="No campaigns yet">
          Build one from your templates, enroll a list or a filter, and review it before it sends.
        </EmptyState>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle level={2}>Campaigns</CardTitle>
            <CardDescription>
              {campaigns.data.length} {campaigns.data.length === 1 ? 'campaign' : 'campaigns'}.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <table className="w-full text-left text-sm">
              <thead className="text-muted-foreground">
                <tr>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Name
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Status
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Steps
                  </th>
                  <th scope="col" className="py-1 pr-3 font-medium">
                    Enrollments
                  </th>
                  <th scope="col" className="py-1 font-medium">
                    Next send
                  </th>
                </tr>
              </thead>
              <tbody>
                {campaigns.data.map((campaign) => (
                  <tr key={campaign.id} className="border-t border-border/60">
                    <th scope="row" className="py-2 pr-3 font-normal">
                      <Link
                        to="/campaigns/$campaignId"
                        params={{ campaignId: String(campaign.id) }}
                        className="underline underline-offset-4"
                      >
                        {campaign.name}
                      </Link>
                    </th>
                    <td className="py-2 pr-3">
                      <CampaignStatusBadge status={campaign.status} />
                    </td>
                    <td className="py-2 pr-3 tabular-nums">{campaign.steps}</td>
                    <td className="py-2 pr-3 text-muted-foreground">
                      {countsText(campaign.enrollments)}
                    </td>
                    <td className="py-2 text-muted-foreground">
                      {formatWhen(campaign.next_action_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
