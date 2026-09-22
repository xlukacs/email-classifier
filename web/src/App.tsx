import { useEffect, useMemo, useState, type ReactNode } from "react"
import {
  ChevronDown,
  Database,
  Download,
  ExternalLink,
  Inbox,
  Loader2,
  Mail,
  Pause,
  Play,
  Shield,
  Star,
} from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { cn } from "@/lib/utils"

type Tab = {
  id: string
  name: string
  unread: number
  total: number
}

type Snapshot = {
  address: string
  tabs: Tab[]
  extras: Tab[]
  fetched_at: string
}

type Decision = "attention" | "review" | "skip"

type Classified = {
  decision: Decision
  score: number
  reason: string
  kind: string
  urgency: number
  cached?: boolean
  error?: string | null
  gmail_url?: string | null
  email: {
    id: string
    sender: string
    to?: string
    subject: string
    date: string
    body: string
    gmail_id?: string | null
    thread_id?: string | null
  }
}

type Metrics = {
  status: string
  phase?: string
  dry_run: boolean
  wrote_gmail: boolean
  total: number
  done: number
  listed?: number
  listed_estimate?: number
  listing_done?: boolean
  fetched?: number
  fetch_total?: number
  current?: string
  elapsed_ms: number
  rate: number
  concurrency: number
  fetch_workers: number
  input_tokens: number
  counts: Record<string, number>
  kinds: Record<string, number>
  would_star: number
  cached?: number
  fresh?: number
  error?: string | null
  folder?: string
  model?: string
}

type FolderProgress = {
  classified: Record<string, number>
  live: {
    folder: string
    fetched: number
    fetch_total: number
    done: number
    total: number
    phase: string
  } | null
}

const FOLDERS = [
  { id: "primary", label: "Primary", gmail: "CATEGORY_PERSONAL" },
  { id: "inbox", label: "Inbox", gmail: "INBOX" },
  { id: "social", label: "Social", gmail: "CATEGORY_SOCIAL" },
  { id: "updates", label: "Updates", gmail: "CATEGORY_UPDATES" },
  { id: "promotions", label: "Promotions", gmail: "CATEGORY_PROMOTIONS" },
  { id: "forums", label: "Forums", gmail: "CATEGORY_FORUMS" },
] as const

function fmt(n: number | undefined) {
  return (n ?? 0).toLocaleString()
}

function senderName(from: string) {
  const match = from.match(/^(.*)<.*>$/)
  return (match ? match[1] : from).replaceAll('"', "").trim() || from
}

export default function App() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [statsError, setStatsError] = useState<string | null>(null)
  const [folder, setFolder] = useState("primary")
  const [limit, setLimit] = useState(30)
  const [unread, setUnread] = useState(true)
  const [concurrency, setConcurrency] = useState(16)
  const [dryRun, setDryRun] = useState(true)
  const [force, setForce] = useState(false)
  const [running, setRunning] = useState(false)
  const [items, setItems] = useState<Classified[]>([])
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [jobError, setJobError] = useState<string | null>(null)
  const [folderProgress, setFolderProgress] = useState<FolderProgress | null>(
    null,
  )

  async function refreshStats() {
    try {
      const response = await fetch("/api/gmail/stats")
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as {
          detail?: string
        }
        throw new Error(body.detail || response.statusText)
      }
      setSnapshot((await response.json()) as Snapshot)
      setStatsError(null)
    } catch (error) {
      setStatsError(error instanceof Error ? error.message : "Gmail stats failed")
    }
  }

  async function loadSaved() {
    try {
      const response = await fetch("/api/results?limit=0")
      if (!response.ok) return
      const payload = (await response.json()) as { results: Classified[] }
      setItems(payload.results)
    } catch {
      /* empty cache is fine */
    }
  }

  async function refreshFolders() {
    try {
      const response = await fetch("/api/folders")
      if (!response.ok) return
      setFolderProgress((await response.json()) as FolderProgress)
    } catch {
      /* progress is best-effort */
    }
  }

  useEffect(() => {
    void refreshStats()
    void loadSaved()
    void refreshFolders()
    const id = window.setInterval(() => {
      void refreshStats()
      void refreshFolders()
    }, 15_000)
    return () => window.clearInterval(id)
  }, [])

  async function startJob(demo = false) {
    setRunning(true)
    setItems([])
    setJobError(null)
    setMetrics(null)
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        unread,
        folder,
        limit,
        concurrency,
        dry_run: dryRun,
        apply: !dryRun,
        demo,
        force,
      }),
    })
    if (!response.ok) {
      setRunning(false)
      setJobError(await response.text())
      return
    }
    const { id } = (await response.json()) as { id: string }
    const source = new EventSource(`/api/jobs/${id}/events`)
    source.onmessage = (event) => {
      const payload = JSON.parse(event.data) as {
        type: string
        item?: Classified
        metrics?: Metrics
        error?: string
      }
      if (payload.metrics) setMetrics(payload.metrics)
      if (payload.item) {
        setItems((current) => {
          const next = payload.item!
          return [next, ...current.filter((row) => row.email.id !== next.email.id)]
        })
      }
      if (payload.type === "done" || payload.type === "error") {
        if (payload.error) setJobError(payload.error)
        setRunning(false)
        source.close()
        void refreshStats()
        void refreshFolders()
      }
    }
    source.onerror = () => {
      setRunning(false)
      source.close()
      void refreshFolders()
    }
  }

  const tabCounts = useMemo(() => {
    const map = new Map(
      (snapshot?.tabs ?? []).map((tab) => [tab.id, tab] as const),
    )
    const classified = folderProgress?.classified ?? {}
    return FOLDERS.map((folderOption) => ({
      ...folderOption,
      unread: map.get(folderOption.gmail)?.unread ?? 0,
      total: map.get(folderOption.gmail)?.total ?? 0,
      classified: classified[folderOption.id] ?? 0,
    }))
  }, [snapshot, folderProgress])

  const grouped = {
    attention: items.filter((item) => item.decision === "attention"),
    review: items.filter((item) => item.decision === "review"),
    skip: items.filter((item) => item.decision === "skip"),
  }

  const total = metrics?.total ?? 0
  const classifyProgress =
    metrics && total > 0 ? Math.min(100, (metrics.done / total) * 100) : running ? 4 : 0
  const fetchTotal = metrics?.fetch_total ?? 0
  const fetchProgress =
    fetchTotal > 0 ? Math.min(100, ((metrics?.fetched ?? 0) / fetchTotal) * 100) : 0
  const totalLabel = metrics?.listing_done ? fmt(total) : `${fmt(total)}${total > 0 ? "+" : ""}`
  const phaseLabel = phaseText(metrics?.phase, running)

  return (
    <div className="min-h-svh bg-desk text-paper">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-white/10 px-6 py-5">
        <div>
          <p className="font-display text-4xl leading-none tracking-tight">
            Triage
          </p>
          <p className="mt-2 text-sm text-mute">
            {snapshot?.address ?? "Gmail not connected"}
            {statsError ? ` — ${statsError}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Badge variant={dryRun ? "dry" : "attention"}>
            {dryRun ? "dry-run · Gmail unchanged" : "writes stars + labels"}
          </Badge>
          {metrics?.model ? (
            <Badge variant="mute">{metrics.model}</Badge>
          ) : null}
        </div>
      </header>

      <section className="grid grid-cols-2 gap-px border-b border-white/10 bg-white/10 sm:grid-cols-3 lg:grid-cols-6">
        {tabCounts.map((tab) => {
          const pollLive = folderProgress?.live
          const live =
            running && metrics && metrics.folder === tab.id
              ? metrics
              : pollLive && pollLive.folder === tab.id
                ? pollLive
                : null
          const done = live ? live.done : tab.classified
          const denom = live ? Math.max(live.total, live.done) : tab.total
          const barPct = denom > 0 ? Math.min(100, (done / denom) * 100) : 0
          return (
            <button
              key={tab.id}
              type="button"
              onClick={() => setFolder(tab.id)}
              className={cn(
                "bg-desk px-4 py-4 text-left transition-colors hover:bg-rail",
                folder === tab.id && "bg-rail",
              )}
            >
              <div className="text-xs text-mute">{tab.label}</div>
              <div className="font-display text-3xl leading-none text-lamp">
                {fmt(tab.unread)}
              </div>
              <div className="mt-1 text-xs text-mute">
                {fmt(tab.total)} total
              </div>
              <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/10">
                <div
                  className={cn(
                    "h-full transition-[width]",
                    live ? "bg-wax" : "bg-lamp/70",
                  )}
                  style={{ width: `${barPct}%` }}
                />
              </div>
              <div className="mt-1 text-xs text-mute">
                {live ? (
                  <span className="tabular-nums">
                    ↓ {fmt(live.fetched)}/{fmt(live.fetch_total)} · ✓{" "}
                    {fmt(live.done)}/{fmt(live.total)}
                  </span>
                ) : (
                  <>{fmt(tab.classified)} classified</>
                )}
              </div>
            </button>
          )
        })}
      </section>

      <section className="flex flex-wrap items-center gap-4 border-b border-white/10 px-6 py-4">
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={unread} onCheckedChange={setUnread} />
          Unread only
        </label>
        <label className="flex items-center gap-2 text-sm">
          Limit
          <input
            type="number"
            min={0}
            value={limit}
            onChange={(event) => setLimit(Number(event.target.value))}
            className="h-8 w-16 rounded-md border border-white/10 bg-rail px-2 text-sm"
          />
          <span className="text-mute">0 = all</span>
        </label>
        <label className="flex items-center gap-2 text-sm">
          Workers
          <input
            type="number"
            min={1}
            max={64}
            value={concurrency}
            onChange={(event) => setConcurrency(Number(event.target.value))}
            className="h-8 w-16 rounded-md border border-white/10 bg-rail px-2 text-sm"
          />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={dryRun} onCheckedChange={setDryRun} />
          Dry-run
        </label>
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={force} onCheckedChange={setForce} />
          Re-run Jev
        </label>
        <Button onClick={() => void startJob(false)} disabled={running}>
          {running ? <Loader2 className="animate-spin" /> : <Play />}
          Classify {folder}
        </Button>
        <Button variant="outline" onClick={() => void startJob(true)} disabled={running}>
          Demo
        </Button>
      </section>

      <section className="grid gap-6 px-6 py-5 lg:grid-cols-[220px_1fr]">
        <aside className="space-y-4">
          <p className="flex items-center gap-2 text-sm text-lamp">
            {running ? <Loader2 className="size-4 animate-spin" /> : null}
            {phaseLabel}
          </p>
          {metrics?.current ? (
            <p className="truncate text-xs text-mute" title={metrics.current}>
              {metrics.current}
            </p>
          ) : null}
          <Metric label="Listed" value={fmt(metrics?.listed)} />
          <Metric
            label="Downloaded"
            value={`${fmt(metrics?.fetched)}/${fmt(metrics?.fetch_total)}`}
          />
          <Metric label="Classified" value={`${fmt(metrics?.done)}/${totalLabel}`} />
          <Metric label="Per second" value={(metrics?.rate ?? 0).toFixed(1)} />
          <Metric label="Attention" value={fmt(metrics?.counts.attention)} />
          <Metric label="Review" value={fmt(metrics?.counts.review)} />
          <Metric label="Skip" value={fmt(metrics?.counts.skip)} />
          <Metric label="From sqlite" value={fmt(metrics?.cached)} />
          <Metric label="New Jev calls" value={fmt(metrics?.fresh)} />
          <Metric label="Would star" value={fmt(metrics?.would_star)} />
          <Metric label="Tokens" value={fmt(metrics?.input_tokens)} />
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-mute">
              <span className="inline-flex items-center gap-1">
                <Download className="size-3" /> Gmail
              </span>
              <span>{fetchProgress.toFixed(0)}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
              <div
                className="h-full bg-lamp transition-[width]"
                style={{ width: `${fetchProgress}%` }}
              />
            </div>
            <div className="flex items-center justify-between text-xs text-mute">
              <span className="inline-flex items-center gap-1">
                <Mail className="size-3" /> Jev
              </span>
              <span>{classifyProgress.toFixed(0)}%</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-white/10">
              <div
                className="h-full bg-wax transition-[width]"
                style={{ width: `${classifyProgress}%` }}
              />
            </div>
          </div>
          {jobError ? <p className="text-sm text-wax">{jobError}</p> : null}
          {metrics?.wrote_gmail ? (
            <p className="flex items-center gap-1 text-sm text-brass">
              <Star className="size-3.5" /> Gmail labels written
            </p>
          ) : (
            <p className="flex items-center gap-1 text-sm text-mute">
              <Shield className="size-3.5" /> {dryRun ? "No Gmail writes" : "Writes armed"}
            </p>
          )}
        </aside>

        <div className="grid gap-4 md:grid-cols-3">
          <Column
            title="Needs you"
            icon={<Mail className="size-4 text-wax" />}
            count={grouped.attention.length}
            items={grouped.attention}
            empty="Nothing urgent yet."
          />
          <Column
            title="Look twice"
            icon={<Pause className="size-4 text-brass" />}
            count={grouped.review.length}
            items={grouped.review}
            empty="No uncertain messages."
          />
          <Column
            title="Can wait"
            icon={<Inbox className="size-4 text-moss" />}
            count={grouped.skip.length}
            items={grouped.skip}
            empty="Noise will land here."
          />
        </div>
      </section>
    </div>
  )
}

function phaseText(phase: string | undefined, running: boolean) {
  if (phase === "done") return "Finished"
  if (phase === "error") return "Stopped"
  if (phase === "listing") return "Listing mailbox…"
  if (phase === "running") return "Downloading + classifying…"
  if (phase === "classifying") return "Classifying…"
  if (phase === "writing") return "Writing Gmail labels…"
  if (running) return "Starting…"
  return "Idle"
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <div className="text-xs text-mute">{label}</div>
      <div className="font-display text-2xl leading-none">{value}</div>
    </div>
  )
}

function Column({
  title,
  icon,
  count,
  items,
  empty,
}: {
  title: string
  icon: ReactNode
  count: number
  items: Classified[]
  empty: string
}) {
  return (
    <section className="min-h-80 bg-paper text-ink">
      <header className="flex items-center justify-between border-b border-ink/10 px-4 py-3">
        <div className="flex items-center gap-2 text-sm font-medium">
          {icon}
          {title}
        </div>
        <span className="font-display text-xl leading-none">{count}</span>
      </header>
      <div className="max-h-[70vh] space-y-px overflow-auto">
        {items.length === 0 ? (
          <p className="px-4 py-8 text-sm text-mute">{empty}</p>
        ) : (
          items.map((item) => <EmailCard key={item.email.id} item={item} />)
        )}
      </div>
    </section>
  )
}

function EmailCard({ item }: { item: Classified }) {
  const [open, setOpen] = useState(false)
  const body = item.email.body?.trim() || ""
  const preview = body.replaceAll("\n", " ").slice(0, 160)

  return (
    <article className="border-b border-ink/8 px-4 py-3">
      <div className="flex items-start gap-2">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          className="min-w-0 flex-1 text-left"
        >
          <div className="flex items-start justify-between gap-3">
            <p className="flex items-start gap-1.5 font-medium leading-snug">
              <ChevronDown
                className={cn(
                  "mt-0.5 size-4 shrink-0 text-mute transition-transform",
                  open ? "rotate-0" : "-rotate-90",
                )}
              />
              <span>{item.email.subject}</span>
            </p>
            <span className="shrink-0 text-xs text-mute">
              {item.score.toFixed(2)}
            </span>
          </div>
          <p className="mt-1 pl-5 text-sm text-mute">
            {senderName(item.email.sender)}
          </p>
          <p className="mt-1 pl-5 text-xs text-mute">
            {item.kind.replaceAll("_", " ")} · {item.reason}
            {item.cached ? (
              <span className="ml-2 inline-flex items-center gap-1">
                <Database className="size-3" />
                sqlite
              </span>
            ) : null}
          </p>
          {!open && preview ? (
            <p className="mt-1 line-clamp-2 pl-5 text-xs text-mute/80">{preview}</p>
          ) : null}
        </button>
        {item.gmail_url ? (
          <a
            href={item.gmail_url}
            target="_blank"
            rel="noreferrer"
            title="Open in Gmail"
            className="mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-md px-2 py-1 text-xs text-wax hover:bg-wax/10"
          >
            <ExternalLink className="size-3.5" />
            Gmail
          </a>
        ) : null}
      </div>
      {open ? (
        <div className="mt-3 ml-5 space-y-2 border-t border-ink/8 pt-3">
          <p className="text-xs text-mute">
            {item.email.date}
            {item.email.to ? ` · to ${item.email.to}` : ""}
          </p>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap font-sans text-sm leading-relaxed text-ink/85">
            {body || "No body stored."}
          </pre>
        </div>
      ) : null}
    </article>
  )
}
