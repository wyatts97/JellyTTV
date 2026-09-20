import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { PREF, readEnumPref, writePref } from './prefs'

/**
 * Chrome state the whole app shares.
 *
 * The sidebar belongs to `Layout`, but the player needs to collapse it for
 * theater mode, so both read it from here rather than passing callbacks down
 * through the page. Kept deliberately small: anything page-local stays local.
 */

export type SidebarMode = 'expanded' | 'rail'
const SIDEBAR_MODES: readonly SidebarMode[] = ['expanded', 'rail']

interface UiState {
  sidebar: SidebarMode
  toggleSidebar: () => void
  /** Player fills the window; the sidebar and page chrome step aside. */
  theater: boolean
  setTheater: (value: boolean) => void
}

const UiContext = createContext<UiState | null>(null)

export function UiStateProvider({ children }: { children: ReactNode }) {
  const [sidebar, setSidebar] = useState<SidebarMode>(() =>
    readEnumPref(PREF.sidebar, 'expanded', SIDEBAR_MODES),
  )
  const [theater, setTheater] = useState(false)

  const toggleSidebar = useCallback(() => {
    setSidebar((current) => {
      const next: SidebarMode = current === 'expanded' ? 'rail' : 'expanded'
      writePref(PREF.sidebar, next)
      return next
    })
  }, [])

  // `[` anywhere, the way editors and chat apps do it. Ignored while typing, so
  // it cannot fire from a settings field or the chat box.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== '[' || event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target as HTMLElement | null
      if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) {
        return
      }
      event.preventDefault()
      toggleSidebar()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggleSidebar])

  const value = useMemo(
    () => ({ sidebar, toggleSidebar, theater, setTheater }),
    [sidebar, toggleSidebar, theater],
  )
  return <UiContext.Provider value={value}>{children}</UiContext.Provider>
}

export function useUiState(): UiState {
  const value = useContext(UiContext)
  if (!value) throw new Error('useUiState must be used inside UiStateProvider')
  return value
}

/** Turn theater mode on while a component is mounted, and off when it leaves. */
export function useTheaterExit() {
  const { setTheater } = useUiState()
  useEffect(() => () => setTheater(false), [setTheater])
}
