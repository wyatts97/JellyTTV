import { useEffect, type RefObject } from 'react'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/**
 * Modal behaviour for anything that covers the page: a dialog, the mobile nav
 * drawer.
 *
 * Escape closes it, Tab cycles inside it rather than wandering into the page
 * behind, focus lands on the first control and returns to whatever opened the
 * layer, and the page underneath does not scroll. Without this a keyboard or
 * screen-reader user tabs straight out of an open dialog into content they
 * cannot see.
 */
export function useDismissableLayer({
  open,
  onClose,
  panelRef,
}: {
  open: boolean
  onClose: () => void
  panelRef: RefObject<HTMLElement | null>
}) {
  useEffect(() => {
    if (!open) return
    const restoreTo = document.activeElement as HTMLElement | null
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const focusable = () =>
      Array.from(panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).filter(
        (element) => element.offsetParent !== null,
      )

    focusable()[0]?.focus()

    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const items = focusable()
      if (items.length === 0) return
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !panelRef.current?.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      document.body.style.overflow = previousOverflow
      restoreTo?.focus?.()
    }
  }, [open, onClose, panelRef])
}
