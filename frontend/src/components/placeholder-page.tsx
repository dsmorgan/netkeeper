import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

interface PlaceholderPageProps {
  title: string
  /** Delivery phase from spec section 19. */
  phase: 1 | 2 | 3
  /** The page's purpose, from the table in spec section 14.3. */
  purpose: string
}

/** Stands in for a page a later phase delivers. */
export function PlaceholderPage({ title, phase, purpose }: PlaceholderPageProps) {
  return (
    <Card size="sm" className="max-w-xl">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{purpose}</CardDescription>
      </CardHeader>
      <CardContent className="flex items-center gap-2">
        <Badge variant="outline">Phase {phase}</Badge>
        <span className="text-muted-foreground">This page arrives in phase {phase}.</span>
      </CardContent>
    </Card>
  )
}
