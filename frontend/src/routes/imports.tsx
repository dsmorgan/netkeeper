import { Link, Outlet, createFileRoute } from '@tanstack/react-router'

export const Route = createFileRoute('/imports')({
  component: ImportsLayout,
})

const TAB_CLASS =
  'rounded-md px-2 py-1 text-muted-foreground hover:bg-muted hover:text-foreground ' +
  'data-[status=active]:bg-muted data-[status=active]:font-medium data-[status=active]:text-foreground'

/** The import wizard and its history share a page (spec 14.3, `/imports`). */
function ImportsLayout() {
  return (
    <div className="flex flex-col gap-4">
      <nav aria-label="Imports" className="flex gap-1 text-sm">
        <Link to="/imports" activeOptions={{ exact: true }} className={TAB_CLASS}>
          New import
        </Link>
        <Link to="/imports/runs" className={TAB_CLASS}>
          History
        </Link>
      </nav>
      <Outlet />
    </div>
  )
}
