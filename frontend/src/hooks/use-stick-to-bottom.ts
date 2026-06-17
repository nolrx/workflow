import { useCallback, useLayoutEffect, useRef } from "react"

/** Within this many px of the bottom still counts as "parked at the bottom". */
const BOTTOM_THRESHOLD = 32

/**
 * Keep a scroll container pinned to its bottom as `dep` changes — the
 * ChatGPT/Claude streaming feel, where the viewport follows the newest tokens.
 * If the user scrolls up to read back, auto-follow pauses; it resumes once they
 * scroll back down to the bottom.
 *
 * Returns a callback ref to attach to the scroll container. It tolerates element
 * swaps (e.g. Radix tabs that mount only the active panel), so the same ref can
 * be spread onto several mutually-exclusive containers.
 */
export function useStickToBottom(dep: unknown) {
  const nodeRef = useRef<HTMLElement | null>(null)
  // Whether the viewport is parked at the bottom and should follow new content.
  const pinnedRef = useRef(true)

  const handleScroll = useCallback(() => {
    const node = nodeRef.current
    if (!node) return
    const distanceFromBottom = node.scrollHeight - node.scrollTop - node.clientHeight
    pinnedRef.current = distanceFromBottom <= BOTTOM_THRESHOLD
  }, [])

  const setRef = useCallback(
    (node: HTMLElement | null) => {
      if (nodeRef.current) nodeRef.current.removeEventListener("scroll", handleScroll)
      nodeRef.current = node
      if (node) {
        pinnedRef.current = true
        node.addEventListener("scroll", handleScroll, { passive: true })
      }
    },
    [handleScroll]
  )

  // Follow new content to the bottom before paint (no flicker) while pinned.
  useLayoutEffect(() => {
    const node = nodeRef.current
    if (node && pinnedRef.current) node.scrollTop = node.scrollHeight
  }, [dep])

  return setRef
}
