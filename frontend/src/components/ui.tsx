import { forwardRef, useRef, type ReactNode } from 'react'
import { AlertTriangle, Loader2, X } from 'lucide-react'
import { ApiError } from '@/lib/api'
import { useDismissableLayer } from '@/lib/a11y'
import { cn } from '@/lib/utils'

/* -------------------------------------------------------------- PageHeader */
/** The title block every page opens with: one shape, one set of spacings. */
export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-lg font-semibold text-white">{title}</h1>
        {description && <p className="mt-1 text-sm text-ink-400">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

/* ---------------------------------------------------------------- Skeleton */
/**
 * A placeholder with the shape of the thing that is loading.
 *
 * Replaces a centred spinner, which tells you nothing about what is coming and
 * collapses the layout so the page jumps when data lands.
 */
export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton rounded-md', className)} aria-hidden />
}

export function SkeletonCards({
  count = 3,
  className,
  cardClassName = 'h-40',
}: {
  count?: number
  className?: string
  cardClassName?: string
}) {
  return (
    <div className={cn('grid gap-4 sm:grid-cols-2 xl:grid-cols-3', className)} role="status" aria-label="Loading">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className={cn('rounded-xl', cardClassName)} />
      ))}
    </div>
  )
}

export function SkeletonRows({ count = 6, className }: { count?: number; className?: string }) {
  return (
    <div className={cn('space-y-2 p-5', className)} role="status" aria-label="Loading">
      {Array.from({ length: count }, (_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ Button */
type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'outline'
type ButtonSize = 'sm' | 'md' | 'icon'

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-twitch-600 text-white hover:bg-twitch-500 disabled:bg-twitch-600/40 disabled:text-white/60',
  secondary: 'bg-ink-700 text-ink-200 hover:bg-ink-600 disabled:opacity-50',
  ghost: 'bg-transparent text-ink-300 hover:bg-ink-800 hover:text-ink-200 disabled:opacity-50',
  outline:
    'bg-transparent border border-ink-600 text-ink-200 hover:border-twitch-500 hover:text-white disabled:opacity-50',
  danger: 'bg-rose-600/90 text-white hover:bg-rose-500 disabled:opacity-50',
}

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-xs gap-1.5',
  md: 'h-10 px-4 text-sm gap-2',
  icon: 'h-9 w-9 justify-center',
}

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = 'secondary', size = 'md', loading, disabled, children, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        'inline-flex items-center rounded-lg font-medium transition-colors select-none',
        'disabled:cursor-not-allowed',
        BUTTON_VARIANTS[variant],
        BUTTON_SIZES[size],
        className,
      )}
      {...props}
    >
      {loading && <Loader2 className="size-4 animate-spin" aria-hidden />}
      {children}
    </button>
  )
})

/* -------------------------------------------------------------------- Card */
export function Card({
  className,
  children,
  id,
}: {
  className?: string
  children: ReactNode
  /** Anchor target, for pages with a section index. */
  id?: string
}) {
  return (
    <div id={id} className={cn('card-surface', id && 'scroll-mt-20', className)}>
      {children}
    </div>
  )
}

export function CardHeader({
  title,
  description,
  action,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-4 border-b border-ink-700/70 px-5 py-4',
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-white">{title}</h2>
        {description && <p className="mt-1 text-xs text-ink-400">{description}</p>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  )
}

export function CardBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cn('p-5', className)}>{children}</div>
}

/* ------------------------------------------------------------------- Input */
export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        className={cn(
          'h-10 w-full rounded-lg border border-ink-600 bg-ink-850 px-3 text-sm text-ink-200',
          'placeholder:text-ink-400 transition-colors hover:border-ink-400/60',
          'focus:border-twitch-500 disabled:opacity-60',
          className,
        )}
        {...props}
      />
    )
  },
)

export const Select = forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...props }, ref) {
    return (
      <select
        ref={ref}
        className={cn(
          'h-10 w-full appearance-none rounded-lg border border-ink-600 bg-ink-850 px-3 text-sm',
          'text-ink-200 transition-colors hover:border-ink-400/60 focus:border-twitch-500',
          className,
        )}
        {...props}
      >
        {children}
      </select>
    )
  },
)

export function Field({
  label,
  hint,
  error,
  children,
  className,
}: {
  label: string
  hint?: ReactNode
  error?: string | null
  children: ReactNode
  className?: string
}) {
  return (
    <label className={cn('block', className)}>
      <span className="mb-1.5 block text-xs font-medium text-ink-300">{label}</span>
      {children}
      {error ? (
        <span className="mt-1.5 block text-xs text-rose-400">{error}</span>
      ) : (
        hint && <span className="mt-1.5 block text-xs text-ink-400">{hint}</span>
      )}
    </label>
  )
}

/* ------------------------------------------------------------------ Toggle */
export function Toggle({
  checked,
  onChange,
  label,
  description,
  disabled,
}: {
  checked: boolean
  onChange: (value: boolean) => void
  label: string
  description?: ReactNode
  disabled?: boolean
}) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <div className="min-w-0">
        <p className="text-sm text-ink-200">{label}</p>
        {description && <p className="mt-0.5 text-xs text-ink-400">{description}</p>}
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cn(
          'relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50',
          checked ? 'bg-twitch-600' : 'bg-ink-600',
        )}
      >
        {/*
          `left-0` is load-bearing: without a horizontal anchor an absolutely
          positioned child falls back to its static position, and a <button>
          centres its content - so the knob started from the middle of the track
          and the `on` translate pushed it outside the pill entirely.
        */}
        <span
          className={cn(
            'absolute top-0.5 left-0 size-4 rounded-full bg-white transition-transform',
            checked ? 'translate-x-4.5' : 'translate-x-0.5',
          )}
        />
      </button>
    </div>
  )
}

/* ------------------------------------------------------------------- Badge */
type BadgeTone = 'neutral' | 'live' | 'success' | 'warning' | 'danger' | 'info'

const BADGE_TONES: Record<BadgeTone, string> = {
  neutral: 'bg-ink-700 text-ink-300',
  live: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
  success: 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30',
  warning: 'bg-amber-500/15 text-amber-300 border border-amber-500/30',
  danger: 'bg-rose-500/15 text-rose-300 border border-rose-500/30',
  info: 'bg-jelly-500/15 text-jelly-400 border border-jelly-500/30',
}

export function Badge({
  tone = 'neutral',
  children,
  className,
}: {
  tone?: BadgeTone
  children: ReactNode
  className?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium',
        BADGE_TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  )
}

export function LiveBadge() {
  return (
    <Badge tone="live">
      <span className="live-dot size-1.5 rounded-full bg-rose-400" />
      LIVE
    </Badge>
  )
}

/* ---------------------------------------------------------------- Progress */
export function Progress({ value, className }: { value: number; className?: string }) {
  const clamped = Math.max(0, Math.min(100, value))
  return (
    <div className={cn('h-1.5 w-full overflow-hidden rounded-full bg-ink-700', className)}>
      <div
        className="h-full rounded-full bg-twitch-500 transition-[width] duration-500"
        style={{ width: `${clamped}%` }}
      />
    </div>
  )
}

/* ------------------------------------------------------------------- Modal */
export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  wide,
}: {
  open: boolean
  onClose: () => void
  title: string
  description?: ReactNode
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
}) {
  const panel = useRef<HTMLDivElement>(null)
  // Escape, focus trap, focus restore and scroll lock. The markup claimed
  // `aria-modal` long before any of that was true.
  useDismissableLayer({ open, onClose, panelRef: panel })

  if (!open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-4 sm:items-center"
      onMouseDown={(event) => {
        // Only a click on the backdrop itself, never one that started inside
        // the panel and drifted out (a text selection, say).
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={panel}
        className={cn(
          'card-surface my-8 w-full shadow-2xl',
          wide ? 'max-w-3xl' : 'max-w-lg',
        )}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="flex items-start justify-between gap-4 border-b border-ink-700/70 px-5 py-4">
          <div>
            <h2 className="text-sm font-semibold text-white">{title}</h2>
            {description && <p className="mt-1 text-xs text-ink-400">{description}</p>}
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close">
            <X className="size-4" />
          </Button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto px-5 py-4">{children}</div>
        {footer && (
          <div className="flex justify-end gap-2 border-t border-ink-700/70 px-5 py-4">{footer}</div>
        )}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------- Misc */
export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn('size-4 animate-spin text-ink-400', className)} aria-hidden />
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode
  title: string
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
      {icon && <div className="text-ink-400">{icon}</div>}
      <div>
        <p className="text-sm font-medium text-ink-200">{title}</p>
        {description && <p className="mx-auto mt-1 max-w-md text-xs text-ink-400">{description}</p>}
      </div>
      {action}
    </div>
  )
}

/**
 * Terminal state for a failed query. Exists because the alternative pattern -
 * rendering a Spinner whenever data is absent - is indistinguishable from
 * loading and leaves a failed page spinning forever with no way to recover.
 */
export function QueryError({
  title = 'Something went wrong',
  error,
  onRetry,
  pending,
}: {
  title?: string
  error: unknown
  onRetry?: () => void
  pending?: boolean
}) {
  const status = error instanceof ApiError ? error.status : undefined
  const message = error instanceof Error ? error.message : String(error ?? 'Unknown error')

  return (
    <Card>
      <EmptyState
        icon={<AlertTriangle className="size-6 text-amber-400" aria-hidden />}
        title={title}
        description={
          <>
            {status !== undefined && (
              <Badge tone="danger" className="mr-2 align-middle">
                {status}
              </Badge>
            )}
            {message}
            {status === 401 && ' — your session has expired. Sign in again to continue.'}
          </>
        }
        action={
          onRetry && (
            <Button variant="secondary" size="sm" onClick={onRetry} loading={pending}>
              Retry
            </Button>
          )
        }
      />
    </Card>
  )
}

export function CopyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-ink-300">{label}</span>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded-lg border border-ink-700 bg-ink-850 px-3 py-2 font-mono text-xs text-ink-200">
          {value}
        </code>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void navigator.clipboard.writeText(value)}
        >
          Copy
        </Button>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------ PlayOverlay */
/** Hover affordance for a thumbnail that opens the player. Parent needs `group relative`. */
export function PlayOverlay() {
  return (
    <div className="absolute inset-0 grid place-items-center bg-black/0 transition-colors group-hover:bg-black/35">
      <span className="grid size-12 place-items-center rounded-full bg-twitch-600/90 text-white opacity-0 shadow-lg transition-opacity group-hover:opacity-100">
        <svg viewBox="0 0 24 24" className="ml-0.5 size-5 fill-current" aria-hidden>
          <path d="M8 5v14l11-7z" />
        </svg>
      </span>
    </div>
  )
}
